"""Deterministic claim-eligibility engine (Rules 1-5). No UI / no AI / no wall clock.

Times are naive local clinic times (YYYY-MM-DDTHH:MM). Nothing here reads the
system date or time, so every shift is effectively "upcoming".
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional

DT_FORMAT = "%Y-%m-%dT%H:%M"
MIN_REST_HOURS = 11
MAX_WEEKLY_HOURS = 40

RULE_TITLES = {
    1: "Not your own shift",
    2: "Required certification",
    3: "No overlapping shift",
    4: "Minimum 11-hour rest",
    5: "Maximum 40 hours per week",
}


class EligibilityInputError(ValueError):
    """Malformed / unknown input (distinct from a business-rule violation)."""


@dataclass(frozen=True)
class Violation:
    rule: int
    code: str
    message: str
    conflicting_shift_id: Optional[str] = None

    def to_dict(self):
        return asdict(self)


# ----------------------------------------------------------------- helpers
def parse_dt(value):
    try:
        return datetime.strptime(value, DT_FORMAT)
    except (TypeError, ValueError):
        raise EligibilityInputError(
            f"Malformed timestamp {value!r}; expected YYYY-MM-DDTHH:MM")


def shift_hours(start, end):
    """Duration in hours for string or datetime inputs (handles midnight crossing)."""
    s = parse_dt(start) if isinstance(start, str) else start
    e = parse_dt(end) if isinstance(end, str) else end
    return (e - s).total_seconds() / 3600


def week_start(dt):
    """Monday 00:00 of the week containing dt."""
    d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end  # strict: touching endpoints don't overlap


def fmt_hours(h):
    h = round(h, 2)
    return str(int(h)) if h == int(h) else f"{h:g}"


def fmt_dt(dt):
    return dt.strftime("%a %d %b %H:%M")


def fmt_range(start_dt, end_dt):
    """e.g. 'Mon 02 Mar 15:00 - Mon 02 Mar 23:00'."""
    return f"{fmt_dt(start_dt)} - {fmt_dt(end_dt)}"


def _load_staff(conn, staff_id):
    row = conn.execute("SELECT id, name, role FROM staff WHERE id = ?", (staff_id,)).fetchone()
    if row is None:
        raise EligibilityInputError(f"Unknown staff member {staff_id!r}")
    return row


def _load_shift(conn, shift_id):
    row = conn.execute(
        'SELECT id, start, "end" AS end_, required_certification, assigned_to '
        "FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    if row is None:
        raise EligibilityInputError(f"Unknown shift {shift_id!r}")
    d = dict(row)
    d["start_dt"], d["end_dt"] = parse_dt(d["start"]), parse_dt(d["end_"])
    if d["end_dt"] <= d["start_dt"]:
        raise EligibilityInputError(f"Shift {shift_id} ends before it starts")
    d["hours"] = shift_hours(d["start_dt"], d["end_dt"])
    return d


def _assigned(conn, staff_id, exclude_shift_id=None):
    out = []
    for r in conn.execute(
            'SELECT id, start, "end" AS end_ FROM shifts WHERE assigned_to = ? ORDER BY start, id',
            (staff_id,)):
        if r["id"] == exclude_shift_id:
            continue
        s, e = parse_dt(r["start"]), parse_dt(r["end_"])
        out.append({"id": r["id"], "start_dt": s, "end_dt": e, "hours": shift_hours(s, e)})
    return out


def weekly_hours(conn, staff_id, week_start_dt, exclude_shift_id=None):
    """Hours of assigned shifts that START in [week_start, week_start + 7 days)."""
    week_end = week_start_dt + timedelta(days=7)
    return sum(x["hours"] for x in _assigned(conn, staff_id, exclude_shift_id)
               if week_start_dt <= x["start_dt"] < week_end)


# ------------------------------------------------------------ main engine
def check_claim_eligibility_detailed(conn, claimer_id, shift_id, include_rule1=True):
    """Return a list of Violation objects (empty list == eligible).

    * Rule 1 failure returns ONLY the Rule 1 violation.
    * Otherwise Rules 2-5 are all evaluated and every violation is reported.
    * include_rule1=False is used for approval-time re-validation (Rules 2-5).
    """
    claimer = _load_staff(conn, claimer_id)
    shift = _load_shift(conn, shift_id)

    if include_rule1 and shift["assigned_to"] == claimer_id:
        return [Violation(
            1, "OWN_SHIFT",
            f"Rule 1: {claimer['name']} already owns {shift['id']} and cannot claim their own shift.")]

    violations = []

    # Rule 2 - certification
    req = shift["required_certification"]
    if req:
        has = conn.execute(
            "SELECT 1 FROM staff_certifications WHERE staff_id = ? AND certification = ?",
            (claimer_id, req)).fetchone()
        if not has:
            violations.append(Violation(
                2, "MISSING_CERTIFICATION",
                f"Rule 2: {req} certification is required, but {claimer['name']} does not have it."))

    mine = _assigned(conn, claimer_id, exclude_shift_id=shift["id"])
    s_start, s_end = shift["start_dt"], shift["end_dt"]

    # Rule 3 - overlap (strict)
    overlapping_ids = set()
    for x in mine:
        if overlaps(s_start, s_end, x["start_dt"], x["end_dt"]):
            overlapping_ids.add(x["id"])
            violations.append(Violation(
                3, "OVERLAP",
                f"Rule 3: {x['id']} ({fmt_range(x['start_dt'], x['end_dt'])}) overlaps the "
                f"proposed shift {shift['id']} ({fmt_range(s_start, s_end)}).",
                x["id"]))

    # Rule 4 - rest (skip overlapping shifts; those are Rule 3's job)
    required = timedelta(hours=MIN_REST_HOURS)
    for x in mine:
        if x["id"] in overlapping_ids:
            continue
        if x["end_dt"] <= s_start:       # X is earlier
            gap = s_start - x["end_dt"]
            earlier_txt = f"{x['id']} ends {fmt_dt(x['end_dt'])}"
            later_txt = f"{shift['id']} starts {fmt_dt(s_start)}"
        else:                             # X is later (non-overlap guarantees s_end <= x.start)
            gap = x["start_dt"] - s_end
            earlier_txt = f"{shift['id']} ends {fmt_dt(s_end)}"
            later_txt = f"{x['id']} starts {fmt_dt(x['start_dt'])}"
        if gap < required:
            gh = gap.total_seconds() / 3600
            violations.append(Violation(
                4, "INSUFFICIENT_REST",
                f"Rule 4: Only {fmt_hours(gh)} hours of rest separate the shifts "
                f"({earlier_txt}; {later_txt}); {MIN_REST_HOURS} hours are required.",
                x["id"]))

    # Rule 5 - weekly hours (week in which the candidate STARTS)
    wk = week_start(s_start)
    current = weekly_hours(conn, claimer_id, wk, exclude_shift_id=shift["id"])
    projected = current + shift["hours"]
    if projected > MAX_WEEKLY_HOURS:
        violations.append(Violation(
            5, "WEEKLY_HOURS_EXCEEDED",
            f"Rule 5: Projected weekly hours would be {fmt_hours(projected)} "
            f"(current {fmt_hours(current)} + this shift {fmt_hours(shift['hours'])}) for the week of "
            f"{wk:%d %b %Y}, exceeding the {MAX_WEEKLY_HOURS}-hour limit."))

    violations.sort(key=lambda v: v.rule)
    return violations


def check_claim_eligibility(conn, claimer_id, shift_id):
    """Public contract: [] when eligible, otherwise all applicable reason strings."""
    return [v.message for v in check_claim_eligibility_detailed(conn, claimer_id, shift_id)]


def workload_preview(conn, staff_id, shift_id):
    """Read-only weekly workload impact of adding shift_id to staff_id's assignments."""
    _load_staff(conn, staff_id)
    shift = _load_shift(conn, shift_id)
    wk = week_start(shift["start_dt"])
    current = weekly_hours(conn, staff_id, wk, exclude_shift_id=shift["id"])
    projected = current + shift["hours"]
    return {
        "week_start": wk.strftime("%Y-%m-%d"),
        "week_end": (wk + timedelta(days=7)).strftime("%Y-%m-%d"),
        "current_hours": current,
        "candidate_hours": shift["hours"],
        "projected_hours": projected,
        "limit": MAX_WEEKLY_HOURS,
        "remaining_hours": MAX_WEEKLY_HOURS - projected,
        "over_limit": projected > MAX_WEEKLY_HOURS,
        "summary": f"{fmt_hours(current)} hours currently -> {fmt_hours(projected)} hours after this shift",
    }


def rule_checklist(conn, claimer_id, shift_id, include_rule1=True):
    """Per-rule pass/fail checklist built from the deterministic engine."""
    violations = check_claim_eligibility_detailed(conn, claimer_id, shift_id, include_rule1)
    by_rule = {}
    for v in violations:
        by_rule.setdefault(v.rule, []).append(v)
    shift = _load_shift(conn, shift_id)
    claimer = _load_staff(conn, claimer_id)
    wl = workload_preview(conn, claimer_id, shift_id)
    rule1_failed = 1 in by_rule

    pass_msg = {
        1: f"{claimer['name']} is not the owner of {shift['id']}.",
        2: (f"{claimer['name']} holds the required {shift['required_certification']} certification."
            if shift["required_certification"] else "No certification required."),
        3: "No overlap with any assigned shift.",
        4: f"At least {MIN_REST_HOURS} hours of rest around every neighbouring shift.",
        5: f"Projected weekly hours {fmt_hours(wl['projected_hours'])} are within the "
           f"{MAX_WEEKLY_HOURS}-hour limit.",
    }
    items = []
    for rule in range(1, 6):
        if rule == 1 and not include_rule1:
            continue
        if rule1_failed and rule != 1:
            items.append({"rule": rule, "title": RULE_TITLES[rule], "passed": None,
                          "messages": ["Not evaluated (Rule 1 failed)."]})
        elif rule in by_rule:
            items.append({"rule": rule, "title": RULE_TITLES[rule], "passed": False,
                          "messages": [v.message for v in by_rule[rule]]})
        else:
            items.append({"rule": rule, "title": RULE_TITLES[rule], "passed": True,
                          "messages": [f"Rule {rule}: {pass_msg[rule]}"]})
    return items
