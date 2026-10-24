import copy
import random
from datetime import date

import warm_screen_plan as w

TODAY = date(2026, 10, 24)


def _role(h, rank, title="T"):
    return {"job_hash": h, "rank": rank, "title": title}


def _s(co, h, pre, final, title="T"):
    return {"company_key": co, "job_hash": h, "title": title, "company": co,
            "pre_boost": pre, "final": final}


def test_pick_takes_three_per_company_in_rank_order():
    roles = {"a": [_role("a4", 4), _role("a2", 2), _role("a1", 1), _role("a3", 3)]}
    got = w.pick_to_screen(roles, {}, TODAY)
    assert [r["job_hash"] for r in got] == ["a1", "a2", "a3"]


def test_pick_skips_recent_memo_not_old_one():
    roles = {"a": [_role("y", 1), _role("o", 2)]}
    memo = {"y": "2026-10-23", "o": "2026-10-09"}
    got = w.pick_to_screen(roles, memo, TODAY)
    assert [r["job_hash"] for r in got] == ["o"]


def test_pick_bad_memo_date_does_not_skip():
    roles = {"a": [_role("x", 1)]}
    assert len(w.pick_to_screen(roles, {"x": "garbage"}, TODAY)) == 1


def test_choose_rejects_below_70():
    out = w.choose_outputs([_s("a", "1", 69, 99), _s("b", "2", 70, 100)])
    assert [s["job_hash"] for s in out["rejected"]] == ["1"]
    assert [s["job_hash"] for s in out["cards"]] == ["2"]


def test_choose_keeps_two_per_company():
    out = w.choose_outputs([_s("a", "1", 80, 100), _s("a", "2", 75, 99), _s("a", "3", 71, 98)])
    assert [s["job_hash"] for s in out["cards"]] == ["1", "2"]
    assert [s["job_hash"] for s in out["held"]] == ["3"]


def test_seven_companies_five_cards_two_digest():
    items = [_s(f"c{i}", f"h{i}", 80, 100 - i) for i in range(7)]
    out = w.choose_outputs(items)
    assert [s["job_hash"] for s in out["cards"]] == [f"h{i}" for i in range(5)]
    assert [s["job_hash"] for s in out["digest"]] == ["h5", "h6"]


def test_twelve_companies_five_five_two_held():
    items = [_s(f"c{i:02d}", f"h{i}", 80, 100 - i) for i in range(12)]
    out = w.choose_outputs(items)
    assert (len(out["cards"]), len(out["digest"]), len(out["held"])) == (5, 5, 2)


def test_ties_stable_when_shuffled():
    items = [_s(f"c{i}", f"h{i}", 80, 100) for i in range(9)]
    base = w.choose_outputs(items)
    for seed in (1, 2):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        out = w.choose_outputs(shuffled)
        assert out == base


def test_input_not_mutated():
    items = [_s("a", "1", 80, 100), _s("a", "2", 60, 90), _s("a", "3", 75, 95)]
    before = copy.deepcopy(items)
    w.choose_outputs(items)
    assert items == before
    roles = {"a": [_role("x", 2), _role("y", 1)]}
    rb = copy.deepcopy(roles)
    w.pick_to_screen(roles, {}, TODAY)
    assert roles == rb


def test_applied_recently():
    assert w.applied_recently({"a": "2026-10-11"}, "a", TODAY) is True
    assert w.applied_recently({"a": "2026-10-09"}, "a", TODAY) is False
    assert w.applied_recently({}, "a", TODAY) is False
    assert w.applied_recently({"a": "soon"}, "a", TODAY) is False
    assert w.applied_recently({"a": "2026-11-01"}, "a", TODAY) is True


def test_digest_lines_shape():
    line = w.digest_lines([{"final": 7, "company": "C" * 40, "title": "T" * 60}])[0]
    assert line == "  7  " + "C" * 28 + " - " + "T" * 40
