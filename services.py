"""Service layer: all writes and reads used by the UI.

Every mutating function returns a dict:
    {"success": bool, "message": str, "reasons": [str], "ids": {...}, "error_type": str|None}
error_type is one of None, "INPUT", "PERMISSION", "STATE", "RULES", "DB".

Transaction strategy for every write:
  1. BEGIN IMMEDIATE, validate and change state.
  2. Success -> the success audit row is inserted in the SAME transaction, then COMMIT.
  3. Refusal / error -> ROLLBACK, then the failure audit row is written in its own
     short transaction. A success is never logged unless it actually committed.
"""
import sqlite3
from contextlib import contextmanager

import eligibility as elig
from database import get_connection, now_str, safe_rollback

NO_LONGER_AVAILABLE = "no longer available"
ACTIVE = ("OPEN", "CLAIMED")


# ------------------------------------------------------------------ results
def _ok(message, **ids):
    return {"success": True, "message": message, "reasons": [], "ids": ids, "error_type": None}


def _fail(message, reasons=None, error_type="RULES", **ids):
    return {"success": False, "message": message, "reasons": list(reasons or []),
            "ids": ids, "error_type": error_type}


# ------------------------------------------------------------------ helpers
def _clean_id(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def _clean_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _staff(conn, staff_id):
    return conn.execute("SELECT id, name, role FROM staff WHERE id = ?", (staff_id,)).fetchone()


def _label(row):
    return f"{row['name']} ({row['id']})"


def _listing(conn, listing_id):
    return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


def _insert_history(conn, actor_id, action, shift_id, outcome, details):
    conn.execute(
        "INSERT INTO action_history(actor_id, action, shift_id, outcome, details, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (str(actor_id), action, shift_id, outcome, details, now_str()))


def _details(res):
    text = res["message"]
    if res["reasons"]:
        text += " | " + " ; ".join(res["reasons"])
    return text


def _execute(action, actor_id, shift_hint, db_path, body):
    conn = get_connection(db_path)
    try:
        res = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            res = body(conn)
            if res["success"]:
                _insert_history(conn, actor_id, action, res["ids"].get("shift_id") or shift_hint,
                                "SUCCESS", _details(res))
                conn.execute("COMMIT")
                return res
            conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            safe_rollback(conn)
            res = _fail("Database error - no changes were saved.", [str(exc)], "DB",
                        **(res["ids"] if res else {}))
        # refused / failed: log the failure in its own short transaction
        try:
            conn.execute("BEGIN IMMEDIATE")
            _insert_history(conn, actor_id, action, res["ids"].get("shift_id") or shift_hint,
                            "FAILURE", _details(res))
            conn.execute("COMMIT")
        except sqlite3.Error:
            safe_rollback(conn)
        return res
    finally:
        conn.close()


@contextmanager
def _reader(db_path):
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _need_actor(conn, actor_id, role, what):
    """Returns (staff_row, None) or (None, failure_result)."""
    aid = _clean_id(actor_id)
    if not aid:
        return None, _fail("Invalid input: a person ID is required.", error_type="INPUT")
    row = _staff(conn, aid)
    if row is None:
        return None, _fail(f"Invalid input: unknown person {aid!r}.", error_type="INPUT")
    if row["role"] != role:
        who = "Managers" if row["role"] == "MANAGER" else "Staff members"
        return None, _fail(f"Permission denied: {who} cannot {what}.", error_type="PERMISSION")
    return row, None


def _need_listing(conn, listing_id):
    lid = _clean_int(listing_id)
    if lid is None:
        return None, _fail("Invalid input: listing ID must be a whole number.", error_type="INPUT")
    row = _listing(conn, lid)
    if row is None:
        return None, _fail(f"Invalid input: listing {lid} does not exist.", error_type="INPUT")
    return row, None


# ------------------------------------------------------------------ writes
def post_listing(actor_id, shift_id, db_path=None):
    def body(conn):
        actor, err = _need_actor(conn, actor_id, "STAFF", "post shifts")
        if err:
            return err
        sid = _clean_id(shift_id)
        if not sid:
            return _fail("Invalid input: a shift ID is required.", error_type="INPUT")
        shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (sid,)).fetchone()
        if shift is None:
            return _fail(f"Invalid input: unknown shift {sid!r}.", error_type="INPUT")
        if shift["assigned_to"] != actor["id"]:
            return _fail(f"Permission denied: {sid} is not assigned to {_label(actor)}; "
                         "you can only post your own shifts.", error_type="PERMISSION", shift_id=sid)
        active = conn.execute(
            "SELECT id, status FROM listings WHERE shift_id = ? AND status IN ('OPEN','CLAIMED')",
            (sid,)).fetchone()
        if active:
            return _fail(f"{sid} already has an active listing (#{active['id']}, {active['status']}).",
                         error_type="STATE", shift_id=sid, listing_id=active["id"])
        ts = now_str()
        try:
            cur = conn.execute(
                "INSERT INTO listings(shift_id, poster_id, claimer_id, status, created_at, updated_at) "
                "VALUES (?,?,NULL,'OPEN',?,?)", (sid, actor["id"], ts, ts))
        except sqlite3.IntegrityError:
            return _fail(f"{sid} already has an active listing.", error_type="STATE", shift_id=sid)
        return _ok(f"{_label(actor)} posted {sid} (listing #{cur.lastrowid}).",
                   shift_id=sid, listing_id=cur.lastrowid)
    return _execute("POST", actor_id, _clean_id(shift_id), db_path, body)


def withdraw_listing(actor_id, listing_id, db_path=None):
    def body(conn):
        actor, err = _need_actor(conn, actor_id, "STAFF", "withdraw listings")
        if err:
            return err
        lst, err = _need_listing(conn, listing_id)
        if err:
            return err
        ids = {"shift_id": lst["shift_id"], "listing_id": lst["id"]}
        if lst["poster_id"] != actor["id"]:
            return _fail("Permission denied: only the original poster can withdraw this listing.",
                         error_type="PERMISSION", **ids)
        if lst["status"] != "OPEN":
            return _fail(f"Only OPEN listings can be withdrawn (this one is {lst['status']}).",
                         error_type="STATE", **ids)
        n = conn.execute("UPDATE listings SET status='WITHDRAWN', updated_at=? "
                         "WHERE id=? AND status='OPEN'", (now_str(), lst["id"])).rowcount
        if n != 1:
            return _fail("Listing changed while withdrawing; nothing was saved.", error_type="STATE", **ids)
        return _ok(f"{_label(actor)} withdrew listing #{lst['id']} for {lst['shift_id']}.", **ids)
    return _execute("WITHDRAW", actor_id, None, db_path, body)


def claim_listing(actor_id, listing_id, db_path=None):
    def body(conn):
        # (a) malformed input, (b) role
        actor, err = _need_actor(conn, actor_id, "STAFF", "claim shifts")
        if err:
            return err
        lst, err = _need_listing(conn, listing_id)
        if err:
            return err
        ids = {"shift_id": lst["shift_id"], "listing_id": lst["id"]}
        # (c) not OPEN -> exactly "no longer available", nothing else evaluated
        if lst["status"] != "OPEN":
            return _fail(NO_LONGER_AVAILABLE, error_type="STATE", **ids)
        # (d)+(e) Rule 1 alone, otherwise Rules 2-5 together - re-evaluated inside the tx
        violations = elig.check_claim_eligibility_detailed(conn, actor["id"], lst["shift_id"])
        if violations:
            return _fail(f"Claim refused: {len(violations)} rule violation(s).",
                         [v.message for v in violations], "RULES", **ids)
        n = conn.execute(
            "UPDATE listings SET status='CLAIMED', claimer_id=?, updated_at=? "
            "WHERE id=? AND status='OPEN'", (actor["id"], now_str(), lst["id"])).rowcount
        if n != 1:
            return _fail(NO_LONGER_AVAILABLE, error_type="STATE", **ids)
        return _ok(f"{_label(actor)} claimed {lst['shift_id']}; awaiting manager approval.",
                   claimer_id=actor["id"], **ids)
    return _execute("CLAIM", actor_id, None, db_path, body)


def unclaim_listing(actor_id, listing_id, db_path=None):
    def body(conn):
        actor, err = _need_actor(conn, actor_id, "STAFF", "unclaim shifts")
        if err:
            return err
        lst, err = _need_listing(conn, listing_id)
        if err:
            return err
        ids = {"shift_id": lst["shift_id"], "listing_id": lst["id"]}
        if lst["status"] != "CLAIMED":
            return _fail(f"Only CLAIMED listings can be unclaimed (this one is {lst['status']}).",
                         error_type="STATE", **ids)
        if lst["claimer_id"] != actor["id"]:
            return _fail("Permission denied: only the claimer can unclaim this listing.",
                         error_type="PERMISSION", **ids)
        n = conn.execute(
            "UPDATE listings SET status='OPEN', claimer_id=NULL, updated_at=? "
            "WHERE id=? AND status='CLAIMED' AND claimer_id=?",
            (now_str(), lst["id"], actor["id"])).rowcount
        if n != 1:
            return _fail("Listing changed while unclaiming; nothing was saved.", error_type="STATE", **ids)
        return _ok(f"{_label(actor)} unclaimed {lst['shift_id']}; listing is OPEN again.", **ids)
    return _execute("UNCLAIM", actor_id, None, db_path, body)


def approve_listing(actor_id, listing_id, db_path=None):
    def body(conn):
        mgr, err = _need_actor(conn, actor_id, "MANAGER", "approve claims")
        if err:
            return err
        lst, err = _need_listing(conn, listing_id)
        if err:
            return err
        ids = {"shift_id": lst["shift_id"], "listing_id": lst["id"]}
        if lst["status"] != "CLAIMED" or not lst["claimer_id"]:
            return _fail(f"Only CLAIMED listings can be approved (this one is {lst['status']}).",
                         error_type="STATE", **ids)
        claimer = _staff(conn, lst["claimer_id"])
        shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (lst["shift_id"],)).fetchone()
        if claimer is None or claimer["role"] != "STAFF":
            return _fail("The recorded claimer is not a valid staff member.", error_type="STATE", **ids)
        if shift["assigned_to"] != lst["poster_id"]:
            return _fail(f"{shift['id']} is no longer assigned to the poster "
                         f"({shift['assigned_to']}); approval refused.", error_type="STATE", **ids)
        # Re-check Rules 2-5 against the claimer's CURRENT assignments
        violations = elig.check_claim_eligibility_detailed(
            conn, claimer["id"], shift["id"], include_rule1=False)
        if violations:
            return _fail(f"Approval refused: the claim by {_label(claimer)} no longer passes the rules. "
                         "The listing stays CLAIMED.", [v.message for v in violations], "RULES", **ids)
        ts = now_str()
        conn.execute("UPDATE shifts SET assigned_to=? WHERE id=?", (claimer["id"], shift["id"]))
        n = conn.execute("UPDATE listings SET status='APPROVED', updated_at=? "
                         "WHERE id=? AND status='CLAIMED'", (ts, lst["id"])).rowcount
        if n != 1:
            raise sqlite3.OperationalError("listing state changed during approval")
        poster = _staff(conn, lst["poster_id"])
        return _ok(f"{_label(mgr)} approved: {shift['id']} reassigned from {_label(poster)} "
                   f"to {_label(claimer)}.", claimer_id=claimer["id"], **ids)
    return _execute("APPROVE", actor_id, None, db_path, body)


def reject_listing(actor_id, listing_id, db_path=None):
    def body(conn):
        mgr, err = _need_actor(conn, actor_id, "MANAGER", "reject claims")
        if err:
            return err
        lst, err = _need_listing(conn, listing_id)
        if err:
            return err
        ids = {"shift_id": lst["shift_id"], "listing_id": lst["id"]}
        if lst["status"] != "CLAIMED":
            return _fail(f"Only CLAIMED listings can be rejected (this one is {lst['status']}).",
                         error_type="STATE", **ids)
        prev = lst["claimer_id"]
        n = conn.execute("UPDATE listings SET status='OPEN', claimer_id=NULL, updated_at=? "
                         "WHERE id=? AND status='CLAIMED'", (now_str(), lst["id"])).rowcount
        if n != 1:
            return _fail("Listing changed while rejecting; nothing was saved.", error_type="STATE", **ids)
        return _ok(f"{_label(mgr)} rejected the claim by {prev} on {lst['shift_id']}; "
                   "listing is OPEN again.", **ids)
    return _execute("REJECT", actor_id, None, db_path, body)


# ------------------------------------------------------------------- reads
def get_people(db_path=None):
    with _reader(db_path) as conn:
        certs = {}
        for r in conn.execute("SELECT staff_id, certification FROM staff_certifications "
                              "ORDER BY certification"):
            certs.setdefault(r["staff_id"], []).append(r["certification"])
        return [{"id": r["id"], "name": r["name"], "role": r["role"],
                 "certifications": certs.get(r["id"], [])}
                for r in conn.execute("SELECT * FROM staff ORDER BY role DESC, id")]


def get_staff_roster(staff_id, db_path=None):
    """Shifts currently assigned to staff_id with their active listing status (if any)."""
    with _reader(db_path) as conn:
        rows = conn.execute(
            'SELECT s.id AS shift_id, s.start, s."end" AS end_, s.required_certification, '
            "       l.id AS listing_id, l.status AS listing_status, l.claimer_id "
            "FROM shifts s LEFT JOIN listings l "
            "  ON l.shift_id = s.id AND l.status IN ('OPEN','CLAIMED') "
            "WHERE s.assigned_to = ? ORDER BY s.start, s.id", (staff_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["end"] = d.pop("end_")
            d["hours"] = elig.shift_hours(d["start"], d["end"])
            out.append(d)
        return out


_LISTING_SQL = (
    'SELECT l.id AS listing_id, l.shift_id, l.poster_id, p.name AS poster_name, '
    '       l.claimer_id, c.name AS claimer_name, l.status, l.created_at, l.updated_at, '
    '       s.start, s."end" AS end_, s.required_certification, s.assigned_to '
    "FROM listings l JOIN shifts s ON s.id = l.shift_id "
    "JOIN staff p ON p.id = l.poster_id LEFT JOIN staff c ON c.id = l.claimer_id ")


def _listing_dicts(rows):
    out = []
    for r in rows:
        d = dict(r)
        d["end"] = d.pop("end_")
        d["hours"] = elig.shift_hours(d["start"], d["end"])
        out.append(d)
    return out


def get_open_listings(exclude_poster_id=None, db_path=None):
    with _reader(db_path) as conn:
        sql = _LISTING_SQL + "WHERE l.status = 'OPEN' "
        params = []
        if exclude_poster_id:
            sql += "AND l.poster_id <> ? "
            params.append(exclude_poster_id)
        return _listing_dicts(conn.execute(sql + "ORDER BY s.start, l.id", params))


def get_claimed_listings(claimer_id=None, db_path=None):
    with _reader(db_path) as conn:
        sql = _LISTING_SQL + "WHERE l.status = 'CLAIMED' "
        params = []
        if claimer_id:
            sql += "AND l.claimer_id = ? "
            params.append(claimer_id)
        return _listing_dicts(conn.execute(sql + "ORDER BY s.start, l.id", params))


def get_my_claims(claimer_id, db_path=None):
    """Claims by this person that are pending (CLAIMED) or completed (APPROVED)."""
    with _reader(db_path) as conn:
        return _listing_dicts(conn.execute(
            _LISTING_SQL + "WHERE l.claimer_id = ? AND l.status IN ('CLAIMED','APPROVED') "
                           "ORDER BY l.id DESC", (claimer_id,)))


def get_my_listings(poster_id, db_path=None):
    with _reader(db_path) as conn:
        return _listing_dicts(conn.execute(
            _LISTING_SQL + "WHERE l.poster_id = ? ORDER BY l.id DESC", (poster_id,)))


def get_claim_eligibility(claimer_id, listing_id, mode="claim", db_path=None):
    """Live, read-only assessment.

    mode="claim"    - listing must be OPEN; Rules 1-5 (claimer = the viewer).
    mode="approval" - listing must be CLAIMED; Rules 2-5 for the recorded claimer.
    """
    with _reader(db_path) as conn:
        cid = _clean_id(claimer_id)
        lid = _clean_int(listing_id)
        if not cid or lid is None:
            return {"success": False, "message": "Invalid input.", "error_type": "INPUT"}
        person = _staff(conn, cid)
        lst = _listing(conn, lid)
        if person is None or lst is None:
            return {"success": False, "message": "Unknown person or listing.", "error_type": "INPUT"}
        if person["role"] != "STAFF":
            return {"success": False, "message": "Managers cannot claim shifts.",
                    "error_type": "PERMISSION"}
        wanted = "OPEN" if mode == "claim" else "CLAIMED"
        if lst["status"] != wanted:
            return {"success": True, "available": False, "eligible": False,
                    "message": NO_LONGER_AVAILABLE if mode == "claim"
                    else f"Listing is {lst['status']}, not CLAIMED.",
                    "checklist": [], "reasons": [], "workload": None, "error_type": "STATE",
                    "listing_status": lst["status"]}
        include_rule1 = mode == "claim"
        checklist = elig.rule_checklist(conn, cid, lst["shift_id"], include_rule1)
        violations = elig.check_claim_eligibility_detailed(conn, cid, lst["shift_id"], include_rule1)
        wl = elig.workload_preview(conn, cid, lst["shift_id"])
        return {"success": True, "available": True, "eligible": not violations,
                "message": "Eligible to claim." if not violations else
                f"Not eligible: {len(violations)} rule violation(s).",
                "checklist": checklist, "reasons": [v.message for v in violations],
                "violations": [v.to_dict() for v in violations],
                "workload": wl, "error_type": None, "listing_status": lst["status"]}


def get_eligible_alternatives(claimer_id, exclude_listing_id=None, db_path=None):
    """Other OPEN listings that pass every rule right now, each with an explanation."""
    out = []
    for lst in get_open_listings(exclude_poster_id=claimer_id, db_path=db_path):
        if exclude_listing_id is not None and lst["listing_id"] == exclude_listing_id:
            continue
        a = get_claim_eligibility(claimer_id, lst["listing_id"], "claim", db_path)
        if a.get("success") and a.get("eligible"):
            lst = dict(lst)
            wl = a["workload"]
            lst["explanation"] = " ".join(
                [m for item in a["checklist"] for m in item["messages"]]) + \
                f" Workload: {wl['summary']} (limit {wl['limit']})."
            lst["workload"] = wl
            out.append(lst)
    return out


def get_action_history(actor_id=None, limit=None, newest_first=False, db_path=None):
    """Chronological (by autoincrement id, not by timestamp) action history."""
    with _reader(db_path) as conn:
        sql = ("SELECT h.id, h.actor_id, COALESCE(s.name, '(unknown)') AS actor_name, h.action, "
               "h.shift_id, h.outcome, h.created_at, h.details "
               "FROM action_history h LEFT JOIN staff s ON s.id = h.actor_id ")
        params = []
        if actor_id:
            sql += "WHERE h.actor_id = ? "
            params.append(actor_id)
        sql += "ORDER BY h.id " + ("DESC" if newest_first else "ASC")
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in conn.execute(sql, params)]


def get_weekly_workload(staff_id, db_path=None):
    """Assigned hours per Monday-Monday week (a shift counts in the week it starts)."""
    from collections import defaultdict
    weeks = defaultdict(lambda: {"hours": 0.0, "shifts": []})
    for sh in get_staff_roster(staff_id, db_path):
        wk = elig.week_start(elig.parse_dt(sh["start"]))
        weeks[wk]["hours"] += sh["hours"]
        weeks[wk]["shifts"].append(sh["shift_id"])
    return [{"week_start": wk.strftime("%Y-%m-%d"),
             "week_end": (wk + elig.timedelta(days=7)).strftime("%Y-%m-%d"),
             "hours": d["hours"], "limit": elig.MAX_WEEKLY_HOURS,
             "remaining_hours": elig.MAX_WEEKLY_HOURS - d["hours"], "shifts": d["shifts"]}
            for wk, d in sorted(weeks.items())]


def get_dashboard_counts(db_path=None):
    with _reader(db_path) as conn:
        one = lambda q: conn.execute(q).fetchone()[0]
        counts = {
            "staff": one("SELECT COUNT(*) FROM staff WHERE role='STAFF'"),
            "managers": one("SELECT COUNT(*) FROM staff WHERE role='MANAGER'"),
            "shifts": one("SELECT COUNT(*) FROM shifts"),
            "actions": one("SELECT COUNT(*) FROM action_history"),
        }
        for st in ("OPEN", "CLAIMED", "APPROVED", "WITHDRAWN"):
            counts[st.lower()] = conn.execute(
                "SELECT COUNT(*) FROM listings WHERE status=?", (st,)).fetchone()[0]
        return counts
