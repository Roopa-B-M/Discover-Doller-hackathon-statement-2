import inspect
from datetime import datetime
from unittest import mock

import pytest

import eligibility as el
from database import get_connection
from helpers import build_db

D = "2026-03-"  # March 2026: Mon 2 ... Sun 8, Mon 9


def check(db, claimer, shift):
    conn = get_connection(db)
    try:
        return el.check_claim_eligibility(conn, claimer, shift)
    finally:
        conn.close()


def detailed(db, claimer, shift):
    conn = get_connection(db)
    try:
        return el.check_claim_eligibility_detailed(conn, claimer, shift)
    finally:
        conn.close()


def rules(db, claimer, shift):
    return sorted({v.rule for v in detailed(db, claimer, shift)})


# ---------------------------------------------------------------- Rule 1
def test_same_owner_returns_only_rule1(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T07:00", D + "02T15:00", "ICU", "P"),
                             ("X", D + "02T10:00", D + "02T12:00", None, "P")])
    out = check(db, "P", "S")  # lacks ICU, would overlap X - none of that may be reported
    assert len(out) == 1 and out[0].startswith("Rule 1")


# ---------------------------------------------------------------- Rule 2
def test_missing_certification_fails_rule2(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T07:00", D + "02T15:00", "ICU", "P")])
    out = check(db, "C", "S")
    assert len(out) == 1 and out[0].startswith("Rule 2") and "ICU" in out[0] and "Cal" in out[0]


def test_matching_certification_passes_rule2(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T07:00", D + "02T15:00", "ICU", "P")], {"C": ["ICU"]})
    assert check(db, "C", "S") == []


def test_certification_must_be_exact(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T07:00", D + "02T15:00", "ICU", "P")], {"C": ["PEDS"]})
    assert rules(db, "C", "S") == [2]


def test_null_certification_passes(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T07:00", D + "02T15:00", None, "P")])
    assert check(db, "C", "S") == []


# ---------------------------------------------------------------- Rule 3
def test_overlap_fails_rule3_and_names_conflict(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T12:00", D + "02T20:00", None, "P"),
                             ("X", D + "02T08:00", D + "02T16:00", None, "C")])
    vs = detailed(db, "C", "S")
    assert [v.rule for v in vs] == [3]            # no Rule 4 against an overlapping shift
    assert vs[0].conflicting_shift_id == "X" and "X" in vs[0].message


def test_endpoint_touching_is_not_overlap(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T15:00", D + "02T23:00", None, "P"),
                             ("X", D + "02T07:00", D + "02T15:00", None, "C")])
    assert 3 not in rules(db, "C", "S")
    assert 4 in rules(db, "C", "S")               # 0h rest instead


def test_overlap_helper_is_strict():
    a, b, c = (datetime(2026, 3, 2, h) for h in (7, 15, 23))
    assert not el.overlaps(a, b, b, c) and not el.overlaps(b, c, a, b)
    assert el.overlaps(a, c, b, c)


# ---------------------------------------------------------------- Rule 4
def test_exactly_11h_rest_passes_earlier_shift(tmp_path):
    db = build_db(tmp_path, [("X", D + "02T00:00", D + "02T08:00", None, "C"),
                             ("S", D + "02T19:00", D + "03T03:00", None, "P")])
    assert check(db, "C", "S") == []


def test_10h59_rest_fails(tmp_path):
    db = build_db(tmp_path, [("X", D + "02T00:00", D + "02T08:00", None, "C"),
                             ("S", D + "02T18:59", D + "03T02:59", None, "P")])
    out = check(db, "C", "S")
    assert len(out) == 1 and out[0].startswith("Rule 4") and "10.98 hours" in out[0]


def test_exactly_11h_rest_passes_later_shift(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T07:00", D + "02T15:00", None, "P"),
                             ("X", D + "03T02:00", D + "03T10:00", None, "C")])
    assert check(db, "C", "S") == []


def test_later_shift_with_short_rest_fails(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T07:00", D + "02T15:00", None, "P"),
                             ("X", D + "03T01:00", D + "03T09:00", None, "C")])  # 10h
    vs = detailed(db, "C", "S")
    assert [v.rule for v in vs] == [4] and vs[0].conflicting_shift_id == "X"


def test_both_sides_reported(tmp_path):
    db = build_db(tmp_path, [("A", D + "02T00:00", D + "02T08:00", None, "C"),
                             ("S", D + "02T15:00", D + "02T23:00", None, "P"),
                             ("B", D + "03T07:00", D + "03T15:00", None, "C")])
    vs = detailed(db, "C", "S")
    assert [(v.rule, v.conflicting_shift_id) for v in vs] == [(4, "A"), (4, "B")]


# ---------------------------------------------------------------- Rule 5
def _week_shifts(n, owner="C", hours=8):
    return [(f"W{i}", f"{D}{2 + i:02d}T07:00", f"{D}{2 + i:02d}T{7 + hours:02d}:00", None, owner)
            for i in range(n)]


def test_exactly_40_hours_passes(tmp_path):
    shifts = _week_shifts(4) + [("S", D + "06T07:00", D + "06T15:00", None, "P")]
    db = build_db(tmp_path, shifts)
    assert check(db, "C", "S") == []
    conn = get_connection(db)
    wl = el.workload_preview(conn, "C", "S")
    assert (wl["current_hours"], wl["projected_hours"], wl["remaining_hours"]) == (32, 40, 0)


def test_41_hours_fails_rule5(tmp_path):
    shifts = _week_shifts(4) + [("S", D + "06T07:00", D + "06T16:00", None, "P")]  # 9h
    db = build_db(tmp_path, shifts)
    out = check(db, "C", "S")
    assert len(out) == 1 and out[0].startswith("Rule 5") and "41" in out[0] and "32" in out[0]


def test_overnight_duration():
    assert el.shift_hours("2026-03-02T23:00", "2026-03-03T07:00") == 8
    assert el.shift_hours("2026-03-04T18:00", "2026-03-04T23:00") == 5


def test_monday_midnight_starts_new_week(tmp_path):
    assert el.week_start(datetime(2026, 3, 9, 0, 0)) == datetime(2026, 3, 9)
    assert el.week_start(datetime(2026, 3, 8, 23, 59)) == datetime(2026, 3, 2)
    base = _week_shifts(5)  # Mon-Fri 8h = 40h in week of Mar 2
    db = build_db(tmp_path, base + [("S", D + "09T00:00", D + "09T08:00", None, "P")])
    assert check(db, "C", "S") == []              # starts Monday 00:00 -> new week
    db2 = build_db(tmp_path, base + [("S", D + "08T23:00", D + "09T07:00", None, "P")], name="b.db")
    assert rules(db2, "C", "S") == [5]            # starts Sunday -> old (full) week


def test_overnight_shift_counted_fully_in_start_week(tmp_path):
    # C has 32h Mon-Thu; candidate Sun 23:00 -> Mon 07:00 (8h) counts wholly in the OLD week => 40.
    shifts = _week_shifts(4) + [("S", D + "08T23:00", D + "09T07:00", None, "P")]
    db = build_db(tmp_path, shifts)
    assert check(db, "C", "S") == []
    conn = get_connection(db)
    wl = el.workload_preview(conn, "C", "S")
    assert wl["week_start"] == "2026-03-02" and wl["projected_hours"] == 40


def test_existing_sunday_overnight_not_counted_in_next_week(tmp_path):
    shifts = [("N", D + "08T23:00", D + "09T07:00", None, "C"),
              ("S", D + "09T18:00", D + "10T02:00", None, "P")]
    db = build_db(tmp_path, shifts)
    conn = get_connection(db)
    assert el.workload_preview(conn, "C", "S")["current_hours"] == 0


# ---------------------------------------------------- multiple / seed data
def test_multiple_violations_reported(tmp_path):
    db = build_db(tmp_path, [("S", D + "02T12:00", D + "02T20:00", "ICU", "P"),
                             ("X", D + "02T08:00", D + "02T16:00", None, "C")])
    assert rules(db, "C", "S") == [2, 3]


def test_seed_scenario_b_ben_vs_sh004(db):
    vs = detailed(db, "S2", "SH004")
    assert [(v.rule, v.conflicting_shift_id) for v in vs] == [(2, None), (4, "SH001"), (4, "SH007")]
    assert "0 hours" in vs[1].message and "8 hours" in vs[2].message and "11 hours" in vs[2].message
    conn = get_connection(db)
    wl = el.workload_preview(conn, "S2", "SH004")
    assert wl["projected_hours"] == 40 and not wl["over_limit"]  # Rule 5 passes at exactly 40


def test_seed_scenario_c_ben_vs_sh013(db):
    vs = detailed(db, "S2", "SH013")
    assert [(v.rule, v.conflicting_shift_id) for v in vs] == [(2, None), (3, "SH012")]


def test_seed_scenario_a_tarun_eligible(db):
    assert check(db, "S6", "SH004") == []
    conn = get_connection(db)
    wl = el.workload_preview(conn, "S6", "SH004")
    assert (wl["current_hours"], wl["projected_hours"]) == (24, 32)


def test_seed_scenario_d_owner_only_rule1(db):
    out = check(db, "S1", "SH004")
    assert len(out) == 1 and out[0].startswith("Rule 1")


def test_malformed_inputs_raise_input_error(db):
    conn = get_connection(db)
    with pytest.raises(el.EligibilityInputError):
        el.check_claim_eligibility(conn, "NOPE", "SH004")
    with pytest.raises(el.EligibilityInputError):
        el.check_claim_eligibility(conn, "S2", "NOPE")
    with pytest.raises(el.EligibilityInputError):
        el.parse_dt("2026-13-45")


# ------------------------------------------------------- clock independence
def test_real_clock_does_not_affect_eligibility(db, tmp_path):
    before = check(db, "S2", "SH004")
    with mock.patch("time.time", return_value=0.0), mock.patch("time.time", return_value=4102444800.0):
        assert check(db, "S2", "SH004") == before
    src = inspect.getsource(el)
    assert "now(" not in src and "today(" not in src and "time.time" not in src
    # Shifts located far in the past behave identically (everything is "upcoming").
    old = build_db(tmp_path, [("S", "2001-03-05T07:00", "2001-03-05T15:00", "ICU", "P")], name="old.db")
    assert rules(old, "C", "S") == [2]
