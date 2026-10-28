from datetime import datetime, timedelta, timezone

import resolve_plan as rp

T0 = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
SRC = "hirebase"


def _cands(n=40):
    return [(f"co{i:02d}", f"Co {i}", 100 - i) for i in range(n)]


def _write(res, key, outcome, now, source=SRC):
    res[(key, source)] = (outcome, now.isoformat(), rp.next_eligible(outcome, now))


def test_resolve_plan_every_company_gets_one_turn():
    res, seen = {}, []
    for run in range(6):
        now = T0 + timedelta(hours=run)
        picks = rp.plan_resolution(_cands(), res, SRC, set(), 8, now)
        if run < 5:
            assert len(picks) == 8
        else:
            assert picks == []
        for key, _d, _p in picks:
            seen.append(key)
            _write(res, key, "empty", now)
    assert len(seen) == 40 and len(set(seen)) == 40


def test_resolve_plan_error_returns_next_day_not_before():
    res = {}
    _write(res, "co00", "error", T0)
    c = _cands(1)
    assert rp.plan_resolution(c, res, SRC, set(), 8, T0 + timedelta(hours=23)) == []
    assert len(rp.plan_resolution(c, res, SRC, set(), 8, T0 + timedelta(days=1, minutes=1))) == 1


def test_resolve_plan_empty_cooldown_44_vs_46_days():
    res = {}
    _write(res, "co00", "empty", T0)
    c = _cands(1)
    assert rp.plan_resolution(c, res, SRC, set(), 8, T0 + timedelta(days=44)) == []
    assert len(rp.plan_resolution(c, res, SRC, set(), 8, T0 + timedelta(days=46))) == 1


def test_resolve_plan_resolved_never_reoffered():
    res = {}
    _write(res, "co00", "resolved", T0)
    assert rp.next_eligible("resolved", T0) == ""
    assert rp.plan_resolution(_cands(1), res, SRC, set(), 8, T0 + timedelta(days=900)) == []


def test_resolve_plan_covered_key_never_offered():
    out = rp.plan_resolution(_cands(3), {}, SRC, {"co00"}, 8, T0)
    assert [k for k, _d, _p in out] == ["co01", "co02"]


def test_resolve_plan_malformed_empty_date_stays_in_cooldown_error_does_not():
    res = {("co00", SRC): ("empty", "x", "not-a-date"), ("co01", SRC): ("error", "x", "garbage")}
    out = rp.plan_resolution(_cands(2), res, SRC, set(), 8, T0)
    assert [k for k, _d, _p in out] == ["co01"]


def test_resolve_plan_unknown_outcome_is_error_and_other_source_ignored():
    assert rp.next_eligible("weird", T0) == rp.next_eligible("error", T0)
    res = {}
    _write(res, "co00", "empty", T0, source="apify")
    out = rp.plan_resolution(_cands(1), res, SRC, set(), 8, T0)
    assert len(out) == 1


def test_resolve_plan_per_run_zero_or_negative():
    assert rp.plan_resolution(_cands(), {}, SRC, set(), 0, T0) == []
    assert rp.plan_resolution(_cands(), {}, SRC, set(), -3, T0) == []


def test_resolve_plan_reserve_order_keeps_all_resolves():
    scan = [(f"s{i}", f"S{i}", 90 - i) for i in range(8)]
    res = [(f"r{i}", f"R{i}", 1) for i in range(3)]
    out = rp.reserve_order(scan, res, 8)
    assert len(out) == 8
    assert {"r0", "r1", "r2"} <= {k for k, _d, _p in out}


def test_resolve_plan_reserve_order_backfills():
    scan = [(f"s{i}", f"S{i}", 90 - i) for i in range(10)]
    assert [k for k, _d, _p in rp.reserve_order(scan, [], 8)] == [f"s{i}" for i in range(8)]
    res = [(f"r{i}", f"R{i}", 1) for i in range(10)]
    assert len(rp.reserve_order([], res, 8)) == 8
    assert rp.reserve_order(scan, res, 0) == []


def test_resolve_plan_tie_order_deterministic_never_attempted_first():
    cands = [("b", "B", 5), ("a", "A", 5), ("c", "C", 5)]
    res = {("a", SRC): ("empty", "x", (T0 - timedelta(days=1)).isoformat())}
    out = rp.plan_resolution(cands, res, SRC, set(), 8, T0)
    assert [k for k, _d, _p in out] == ["b", "c", "a"]
    assert out == rp.plan_resolution(list(reversed(cands)), res, SRC, set(), 8, T0)


def test_resolve_plan_reserve_order_sorts_unsorted_scan_heaviest_first():
    scan = [("a", "A", 1), ("b", "B", 50), ("c", "C", 20), ("d", "D", 50)]
    out = rp.reserve_order(scan, [], 3)
    assert [k for k, _d, _p in out] == ["b", "d", "c"]


def test_resolve_plan_string_and_none_people_do_not_raise():
    cands = [("a", "A", "12"), ("b", "B", None), ("c", "C", "junk"), ("d", "D", 3)]
    out = rp.plan_resolution(cands, {}, SRC, set(), 8, T0)
    assert [k for k, _d, _p in out] == ["a", "d", "b", "c"]
    out2 = rp.reserve_order(cands, [("r", "R", "7")], 4)
    assert len(out2) == 4 and out2[0][0] == "r"
