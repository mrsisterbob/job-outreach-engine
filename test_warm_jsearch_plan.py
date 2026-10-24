from datetime import datetime, timedelta, timezone

from warm_jsearch_plan import plan_warm_jsearch

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


CANDS = [("a", "A", 5), ("b", "B", 9), ("c", "C", 3), ("d", "D", 1)]


def test_spends_nothing_when_week_at_cap():
    picks, left = plan_warm_jsearch(CANDS, [("x", _ago(1), 10)], NOW)
    assert picks == [] and left == 0


def test_spends_only_what_is_left():
    picks, left = plan_warm_jsearch(CANDS, [("x", _ago(2), 7)], NOW)
    assert [k for k, _ in picks] == ["b", "a", "c"]
    assert left == 0


def test_biggest_company_first():
    picks, left = plan_warm_jsearch(CANDS, [], NOW)
    assert picks[0] == ("b", "B")
    assert left == 6


def test_company_inside_retry_gap_skipped():
    picks, _ = plan_warm_jsearch(CANDS, [("b", _ago(10), 1)], NOW)
    assert picks[0][0] == "a" and "b" not in [k for k, _ in picks]


def test_lookup_older_than_gap_eligible_again():
    picks, _ = plan_warm_jsearch(CANDS, [("b", _ago(40), 1)], NOW)
    assert picks[0][0] == "b"


def test_old_ledger_rows_stop_counting():
    picks, left = plan_warm_jsearch(CANDS, [("x", _ago(8), 10)], NOW)
    assert len(picks) == 4 and left == 6


def test_deterministic():
    led = [("a", _ago(3), 1)]
    assert plan_warm_jsearch(CANDS, led, NOW) == plan_warm_jsearch(list(reversed(CANDS)), led, NOW)


def test_ties_break_by_company_key():
    picks, _ = plan_warm_jsearch([("z", "Z", 4), ("m", "M", 4)], [], NOW)
    assert [k for k, _ in picks] == ["m", "z"]


def test_empty_and_zero_contact_yield_empty_plan():
    assert plan_warm_jsearch([], [], NOW) == ([], 10)
    assert plan_warm_jsearch([("a", "A", 0)], [], NOW) == ([], 10)


def test_boundary_exactly_seven_days_counts():
    picks, left = plan_warm_jsearch(CANDS, [("x", _ago(7), 10)], NOW)
    assert picks == [] and left == 0


def test_boundary_exactly_retry_gap_is_eligible():
    picks, _ = plan_warm_jsearch(CANDS, [("b", _ago(28), 1)], NOW)
    assert picks[0][0] == "b"
    picks, _ = plan_warm_jsearch(CANDS, [("b", _ago(27.99), 1)], NOW)
    assert picks[0][0] == "a"


def test_malformed_timestamp_counts_as_spent_now():
    picks, left = plan_warm_jsearch(CANDS, [("b", "not-a-date", 3)], NOW)
    assert left == 10 - 3 - len(picks)
    assert "b" not in [k for k, _ in picks]
    picks, left = plan_warm_jsearch(CANDS, [("x", None, 10)], NOW)
    assert picks == [] and left == 0


def test_duplicate_candidates_picked_once():
    picks, left = plan_warm_jsearch([("a", "A", 2), ("a", "A", 7), ("b", "B", 5)], [], NOW)
    assert [k for k, _ in picks] == ["a", "b"]
    assert left == 8


def test_negative_requests_spent_grants_no_budget():
    picks, left = plan_warm_jsearch(CANDS, [("x", _ago(1), -5), ("y", _ago(1), 4)], NOW)
    assert left == 6 - len(picks)


def test_garbage_requests_spent_counts_as_full_cap():
    for bad in ("abc", None):
        picks, left = plan_warm_jsearch(CANDS, [("x", _ago(1), bad)], NOW)
        assert picks == [] and left == 0
