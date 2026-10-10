import sqlite3

import pytest

import seed_loader
import services as svc
from database import get_connection
from helpers import REAL_SEED, build_db, seeded_db


def q(db, sql, *params):
    conn = get_connection(db)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def owner(db, shift):
    return q(db, "SELECT assigned_to FROM shifts WHERE id=?", shift)[0]["assigned_to"]


def status(db, lid):
    return q(db, "SELECT status, claimer_id FROM listings WHERE id=?", lid)[0]


def post(db, who="S1", shift="SH004"):
    r = svc.post_listing(who, shift, db_path=db)
    assert r["success"], r
    return r["ids"]["listing_id"]


def posted_and_claimed(db, poster="S1", shift="SH004", claimer="S6"):
    lid = post(db, poster, shift)
    r = svc.claim_listing(claimer, lid, db_path=db)
    assert r["success"], r
    return lid


# ------------------------------------------------------------------ seeding
def test_seed_counts_and_assignments_match_json(db):
    import json
    data = json.load(open(REAL_SEED))
    staff = q(db, "SELECT * FROM staff")
    assert len(staff) == 10 and sum(s["role"] == "MANAGER" for s in staff) == 2
    shifts = q(db, 'SELECT id, start, "end", required_certification, assigned_to FROM shifts')
    assert len(shifts) == 61
    by_id = {s["id"]: s for s in shifts}
    for sh in data["shifts"]:
        row = by_id[sh["id"]]
        assert (row["start"], row["end"], row["required_certification"], row["assigned_to"]) == \
               (sh["start"], sh["end"], sh["required_certification"], sh["assigned_to"])
    certs = {(r["staff_id"], r["certification"]) for r in q(db, "SELECT * FROM staff_certifications")}
    assert certs == {(s["id"], c) for s in data["staff"] for c in s["certifications"]}
    assert q(db, "SELECT COUNT(*) n FROM listings")[0]["n"] == 0
    assert q(db, "SELECT COUNT(*) n FROM action_history")[0]["n"] == 0


def test_seed_not_reloaded_or_duplicated_on_restart(db):
    lid = post(db)
    svc.claim_listing("S6", lid, db_path=db)
    svc.approve_listing("M1", lid, db_path=db)
    assert seed_loader.initialize(db, REAL_SEED) is False
    assert q(db, "SELECT COUNT(*) n FROM shifts")[0]["n"] == 61
    assert q(db, "SELECT COUNT(*) n FROM staff")[0]["n"] == 10
    assert owner(db, "SH004") == "S6"           # approved change NOT overwritten by seed
    assert status(db, lid)["status"] == "APPROVED"


def test_empty_listings_does_not_trigger_reseed(db):
    conn = get_connection(db)
    conn.execute("UPDATE shifts SET assigned_to='S2' WHERE id='SH004'")
    conn.close()
    assert q(db, "SELECT COUNT(*) n FROM listings")[0]["n"] == 0
    assert seed_loader.initialize(db, REAL_SEED) is False
    assert owner(db, "SH004") == "S2"


def test_missing_seed_file_reports_expected_path(tmp_path):
    missing = str(tmp_path / "nope" / "seed.json")
    with pytest.raises(seed_loader.SeedFileNotFoundError) as exc:
        seed_loader.initialize(str(tmp_path / "x.db"), missing)
    assert missing in str(exc.value)


# ------------------------------------------------------------------- posting
def test_staff_can_post_own_shift(db):
    r = svc.post_listing("S1", "SH004", db_path=db)
    assert r["success"] and r["ids"]["shift_id"] == "SH004"
    assert status(db, r["ids"]["listing_id"])["status"] == "OPEN"
    assert owner(db, "SH004") == "S1"


def test_manager_cannot_post(db):
    r = svc.post_listing("M1", "SH004", db_path=db)
    assert not r["success"] and r["error_type"] == "PERMISSION"
    assert q(db, "SELECT COUNT(*) n FROM listings")[0]["n"] == 0


def test_cannot_post_someone_elses_shift(db):
    r = svc.post_listing("S2", "SH004", db_path=db)
    assert not r["success"] and r["error_type"] == "PERMISSION"


def test_two_active_listings_for_one_shift_not_allowed(db):
    post(db)
    r = svc.post_listing("S1", "SH004", db_path=db)
    assert not r["success"] and "active listing" in r["message"]
    conn = get_connection(db)
    with pytest.raises(sqlite3.IntegrityError):   # DB-level guard, bypassing the service
        conn.execute("INSERT INTO listings(shift_id,poster_id,status,created_at,updated_at) "
                     "VALUES ('SH004','S1','OPEN','t','t')")
    conn.close()


def test_can_repost_after_withdraw(db):
    lid = post(db)
    assert svc.withdraw_listing("S1", lid, db_path=db)["success"]
    assert svc.post_listing("S1", "SH004", db_path=db)["success"]


# ----------------------------------------------------------------- withdraw
def test_only_poster_can_withdraw_open_listing(db):
    lid = post(db)
    assert not svc.withdraw_listing("S2", lid, db_path=db)["success"]
    assert not svc.withdraw_listing("M1", lid, db_path=db)["success"]
    assert status(db, lid)["status"] == "OPEN"
    assert svc.withdraw_listing("S1", lid, db_path=db)["success"]
    assert status(db, lid)["status"] == "WITHDRAWN"


def test_cannot_withdraw_claimed_listing(db):
    lid = posted_and_claimed(db)
    r = svc.withdraw_listing("S1", lid, db_path=db)
    assert not r["success"] and status(db, lid)["status"] == "CLAIMED"


# -------------------------------------------------------------------- claim
def test_eligible_staff_can_claim(db):
    lid = post(db)
    r = svc.claim_listing("S6", lid, db_path=db)
    assert r["success"]
    assert status(db, lid) == {"status": "CLAIMED", "claimer_id": "S6"}
    assert owner(db, "SH004") == "S1"            # pending claim is not an assignment


def test_owner_cannot_claim_own_shift_only_rule1(db):
    lid = post(db)
    r = svc.claim_listing("S1", lid, db_path=db)
    assert not r["success"] and len(r["reasons"]) == 1 and r["reasons"][0].startswith("Rule 1")
    assert status(db, lid)["status"] == "OPEN"


def test_manager_cannot_claim(db):
    lid = post(db)
    r = svc.claim_listing("M1", lid, db_path=db)
    assert not r["success"] and r["error_type"] == "PERMISSION"
    assert status(db, lid)["status"] == "OPEN"


def test_ineligible_claim_blocked_with_all_reasons_and_logged(db):
    lid = post(db)
    r = svc.claim_listing("S2", lid, db_path=db)   # Scenario B
    assert not r["success"]
    nums = [x.split(":")[0] for x in r["reasons"]]
    assert nums == ["Rule 2", "Rule 4", "Rule 4"]
    assert not any(x.startswith(("Rule 3", "Rule 5")) for x in r["reasons"])
    assert status(db, lid) == {"status": "OPEN", "claimer_id": None}
    h = svc.get_action_history(db_path=db)[-1]
    assert (h["actor_id"], h["action"], h["shift_id"], h["outcome"]) == ("S2", "CLAIM", "SH004", "FAILURE")
    assert "Rule 2" in h["details"] and "Rule 4" in h["details"]


def test_scenario_c_missing_cert_and_overlap(db):
    lid = post(db, "S8", "SH013")
    r = svc.claim_listing("S2", lid, db_path=db)
    assert not r["success"]
    assert [x[:6] for x in r["reasons"]] == ["Rule 2", "Rule 3"] and "SH012" in r["reasons"][1]


@pytest.mark.parametrize("final", ["CLAIMED", "WITHDRAWN", "APPROVED"])
def test_non_open_listing_returns_exactly_no_longer_available(db, final):
    lid = post(db)
    if final == "WITHDRAWN":
        svc.withdraw_listing("S1", lid, db_path=db)
    else:
        svc.claim_listing("S6", lid, db_path=db)
        if final == "APPROVED":
            svc.approve_listing("M1", lid, db_path=db)
    # S1 (owner of nothing now / own) and S2 (ineligible) - neither triggers rule checks
    for who in ("S2", "S3"):
        r = svc.claim_listing(who, lid, db_path=db)
        assert not r["success"] and r["message"] == "no longer available" and r["reasons"] == []


def test_malformed_inputs_are_input_errors(db):
    for args in [(None, 1), ("S2", "abc"), ("S2", None), ("NOPE", 1), ("S2", 9999), ("", ""), (5, True)]:
        r = svc.claim_listing(*args, db_path=db)
        assert not r["success"] and r["error_type"] == "INPUT"
    assert svc.post_listing("S1", "NOPE", db_path=db)["error_type"] == "INPUT"
    assert svc.post_listing(None, "SH004", db_path=db)["error_type"] == "INPUT"


# ------------------------------------------------------------------ unclaim
def test_only_claimer_can_unclaim(db):
    lid = posted_and_claimed(db)
    for who in ("S1", "S2", "M1"):
        assert not svc.unclaim_listing(who, lid, db_path=db)["success"]
    assert status(db, lid)["status"] == "CLAIMED"
    assert svc.unclaim_listing("S6", lid, db_path=db)["success"]
    assert status(db, lid) == {"status": "OPEN", "claimer_id": None}


def test_cannot_unclaim_open_listing(db):
    lid = post(db)
    assert not svc.unclaim_listing("S6", lid, db_path=db)["success"]


# ------------------------------------------------------------ reject/approve
def test_reject_returns_listing_to_open(db):
    lid = posted_and_claimed(db)
    assert not svc.reject_listing("S1", lid, db_path=db)["success"]    # staff can't reject
    assert svc.reject_listing("M1", lid, db_path=db)["success"]
    assert status(db, lid) == {"status": "OPEN", "claimer_id": None}
    assert owner(db, "SH004") == "S1"
    assert not svc.reject_listing("M1", lid, db_path=db)["success"]    # no longer CLAIMED
    h = [x for x in svc.get_action_history(db_path=db) if x["action"] == "REJECT"]
    assert [x["outcome"] for x in h] == ["FAILURE", "SUCCESS", "FAILURE"]


def test_staff_cannot_approve(db):
    lid = posted_and_claimed(db)
    r = svc.approve_listing("S1", lid, db_path=db)
    assert not r["success"] and r["error_type"] == "PERMISSION"


def test_scenario_a_full_flow(db):
    lid = post(db, "S1", "SH004")
    a = svc.get_claim_eligibility("S6", lid, db_path=db)
    assert a["eligible"] and a["workload"]["summary"] == "24 hours currently -> 32 hours after this shift"
    assert svc.claim_listing("S6", lid, db_path=db)["success"]
    r = svc.approve_listing("M1", lid, db_path=db)
    assert r["success"], r
    assert owner(db, "SH004") == "S6" and status(db, lid)["status"] == "APPROVED"
    acts = [(h["actor_id"], h["action"], h["outcome"]) for h in svc.get_action_history(db_path=db)]
    assert acts == [("S1", "POST", "SUCCESS"), ("S6", "CLAIM", "SUCCESS"), ("M1", "APPROVE", "SUCCESS")]


def test_approval_revalidates_with_fixture_mutation(db):          # Scenario E (i)
    lid = posted_and_claimed(db)
    conn = get_connection(db)
    conn.execute("DELETE FROM staff_certifications WHERE staff_id='S6'")   # Rule 2 now fails
    conn.close()
    r = svc.approve_listing("M1", lid, db_path=db)
    assert not r["success"] and r["reasons"][0].startswith("Rule 2")
    assert status(db, lid) == {"status": "CLAIMED", "claimer_id": "S6"}
    assert owner(db, "SH004") == "S1"
    h = svc.get_action_history(db_path=db)[-1]
    assert (h["action"], h["outcome"]) == ("APPROVE", "FAILURE") and "Rule 2" in h["details"]


def test_approval_revalidates_naturally_two_claims(db):           # Scenario E (ii)
    l1 = post(db, "S1", "SH004")      # Mar 2 15-23, ICU
    l2 = post(db, "S3", "SH005")      # Mar 2 15-23, no cert -> overlaps SH004 once S6 owns it
    assert svc.claim_listing("S6", l1, db_path=db)["success"]
    assert svc.claim_listing("S6", l2, db_path=db)["success"]    # fine alone: pending != assigned
    assert svc.approve_listing("M1", l1, db_path=db)["success"]
    r = svc.approve_listing("M1", l2, db_path=db)
    assert not r["success"] and r["reasons"][0].startswith("Rule 3") and "SH004" in r["reasons"][0]
    assert status(db, l2) == {"status": "CLAIMED", "claimer_id": "S6"}
    assert owner(db, "SH005") == "S3" and owner(db, "SH004") == "S6"


def test_approve_non_claimed_listing_refused(db):
    lid = post(db)
    r = svc.approve_listing("M1", lid, db_path=db)
    assert not r["success"] and r["error_type"] == "STATE"


# ------------------------------------------------------ audit / integrity
def test_refused_operations_are_recorded(db):
    svc.post_listing("M1", "SH004", db_path=db)
    lid = post(db)
    svc.withdraw_listing("S2", lid, db_path=db)
    svc.claim_listing("S1", lid, db_path=db)
    svc.unclaim_listing("S6", lid, db_path=db)
    svc.approve_listing("M1", lid, db_path=db)
    svc.reject_listing("M1", lid, db_path=db)
    hist = svc.get_action_history(db_path=db)
    assert [(h["action"], h["outcome"]) for h in hist] == [
        ("POST", "FAILURE"), ("POST", "SUCCESS"), ("WITHDRAW", "FAILURE"), ("CLAIM", "FAILURE"),
        ("UNCLAIM", "FAILURE"), ("APPROVE", "FAILURE"), ("REJECT", "FAILURE")]
    assert [h["id"] for h in hist] == sorted(h["id"] for h in hist)
    assert hist[0]["actor_name"] == "Meera"


def test_database_error_rolls_back_and_logs_no_success(db, monkeypatch):
    lid = posted_and_claimed(db)
    real = svc._insert_history

    def boom(conn, actor, action, shift, outcome, details):
        if outcome == "SUCCESS":
            raise sqlite3.OperationalError("disk I/O error (simulated)")
        return real(conn, actor, action, shift, outcome, details)

    monkeypatch.setattr(svc, "_insert_history", boom)
    r = svc.approve_listing("M1", lid, db_path=db)
    assert not r["success"] and r["error_type"] == "DB"
    assert owner(db, "SH004") == "S1"                                  # shift update rolled back
    assert status(db, lid) == {"status": "CLAIMED", "claimer_id": "S6"}
    hist = svc.get_action_history(db_path=db)
    assert not any(h["action"] == "APPROVE" and h["outcome"] == "SUCCESS" for h in hist)
    assert hist[-1]["action"] == "APPROVE" and hist[-1]["outcome"] == "FAILURE"


def test_state_survives_restart(db):
    lid = posted_and_claimed(db)
    svc.approve_listing("M1", lid, db_path=db)
    seed_loader.initialize(db, REAL_SEED)       # simulated process restart
    fresh = get_connection(db)
    assert fresh.execute("SELECT assigned_to FROM shifts WHERE id='SH004'").fetchone()[0] == "S6"
    assert fresh.execute("SELECT COUNT(*) FROM action_history").fetchone()[0] == 3
    fresh.close()
    assert len(svc.get_staff_roster("S6", db_path=db)) == 7


def test_foreign_keys_enforced(db):
    conn = get_connection(db)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE shifts SET assigned_to='NOBODY' WHERE id='SH001'")
    conn.close()


# --------------------------------------------------------- read services
def test_alternatives_only_include_eligible_listings(db):
    l_icu = post(db, "S1", "SH004")      # Ben cannot take it
    l_ok = post(db, "S5", "SH056")       # Mar 13 15-23, no cert - Ben can
    alts = svc.get_eligible_alternatives("S2", exclude_listing_id=l_icu, db_path=db)
    assert [a["listing_id"] for a in alts] == [l_ok]
    assert "Workload" in alts[0]["explanation"]
    # nothing eligible -> empty list (the UI shows a friendly message)
    assert svc.get_eligible_alternatives("S2", exclude_listing_id=l_ok, db_path=db) == []


def test_get_eligibility_unavailable_and_manager(db):
    lid = post(db)
    assert svc.get_claim_eligibility("M1", lid, db_path=db)["error_type"] == "PERMISSION"
    svc.withdraw_listing("S1", lid, db_path=db)
    a = svc.get_claim_eligibility("S2", lid, db_path=db)
    assert a["available"] is False and a["message"] == "no longer available"


def test_weekly_workload_and_roster(db):
    wl = {w["week_start"]: w for w in svc.get_weekly_workload("S2", db_path=db)}
    assert wl["2026-03-02"]["hours"] == 32 and wl["2026-03-02"]["remaining_hours"] == 8
    lid = post(db, "S2", "SH001")
    row = [r for r in svc.get_staff_roster("S2", db_path=db) if r["shift_id"] == "SH001"][0]
    assert row["listing_status"] == "OPEN" and row["listing_id"] == lid
    assert svc.get_dashboard_counts(db)["open"] == 1
