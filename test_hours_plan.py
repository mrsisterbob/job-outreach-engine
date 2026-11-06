import importlib
import json
import math
import os
import threading
from datetime import datetime, timezone

import pytest

import hours_plan as hp

CATS = ["Sleep", "Work", "Gym", "Other"]


def plan(sleep=56, work=60, gym=7, other=45):
    return [{"category": "Sleep", "hours": sleep}, {"category": "Work", "hours": work},
            {"category": "Gym", "hours": gym}, {"category": "Other", "hours": other}]


@pytest.fixture(autouse=True)
def d(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_PLAN_DIR", str(tmp_path / "pd"))
    return tmp_path / "pd"


def test_save_and_load(d):
    s = hp.save_plan(plan(), CATS, None, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    got = hp.load_plan()
    assert got == s and got["version"] == 1
    assert got["hours"] == plan()
    assert datetime.fromisoformat(got["updated_at"]).utcoffset().total_seconds() == 0
    assert (d / "hours_plan.json").exists()


def test_second_save_keeps_prev(d):
    hp.save_plan(plan(), CATS, None)
    s = hp.save_plan(plan(sleep=55, other=46), CATS, 1)
    assert s["version"] == 2
    assert json.loads((d / "hours_plan.prev.json").read_text())["version"] == 1


@pytest.mark.parametrize("sleep,total", [(55.75, "167.75"), (56.25, "168.25")])
def test_bad_total(d, sleep, total):
    with pytest.raises(hp.PlanInvalid) as e:
        hp.save_plan(plan(sleep=sleep), CATS, None)
    assert total in str(e.value) and "0.25" in str(e.value)
    assert not d.exists()
    hp.save_plan(plan(), CATS, None)
    before = (d / "hours_plan.json").read_bytes()
    with pytest.raises(hp.PlanInvalid):
        hp.save_plan(plan(sleep=sleep), CATS, 1)
    assert (d / "hours_plan.json").read_bytes() == before


def test_float_traps():
    cats = [f"c{i}" for i in range(9)]
    with pytest.raises(hp.PlanInvalid):
        hp.validate_plan([{"category": c, "hours": 168 / 9} for c in cats], cats)
    cats = [f"c{i}" for i in range(10)]
    with pytest.raises(hp.PlanInvalid):
        hp.validate_plan([{"category": c, "hours": 0.1} for c in cats], cats)
    cats = [f"c{i}" for i in range(30)]
    ents = [{"category": c, "hours": 5.5} for c in cats[:29]] + [{"category": cats[29], "hours": 8.5}]
    assert sum(e["hours"] for e in hp.validate_plan(ents, cats)) == 168


@pytest.mark.parametrize("bad", [True, "5", math.nan, math.inf, -1, 169, 5.1, None])
def test_bad_hours(bad):
    with pytest.raises(hp.PlanInvalid):
        hp.validate_plan(plan(sleep=bad), CATS)


@pytest.mark.parametrize("ents", [
    [{"category": "Nope", "hours": 56}, *plan()[1:]],
    plan()[:3],
    plan() + [{"category": "Sleep", "hours": 0}],
    [{"category": "  ", "hours": 56}, *plan()[1:]],
    [{"category": "Sleep", "hours": 56, "x": 1}, *plan()[1:]],
    [{"category": "Sleep"}, *plan()[1:]],
    [], "nope", None, {"Sleep": 56},
])
def test_bad_structure(ents):
    with pytest.raises(hp.PlanInvalid):
        hp.validate_plan(ents, CATS)


def test_trim_and_order():
    ents = [{"category": " Other ", "hours": 45}] + plan()[:3][::-1]
    assert [e["category"] for e in hp.validate_plan(ents, CATS)] == CATS


def test_conflicts(d):
    with pytest.raises(hp.PlanConflict):  # base 1 but nothing saved
        hp.save_plan(plan(), CATS, 1)
    assert not d.exists()
    hp.save_plan(plan(), CATS, None)
    hp.save_plan(plan(), CATS, 1)
    for base in (1, None):
        with pytest.raises(hp.PlanConflict) as e:
            hp.save_plan(plan(sleep=55, other=46), CATS, base)
        assert e.value.current_version == 2
    assert hp.load_plan()["hours"] == plan()
    with pytest.raises(hp.PlanInvalid):
        hp.save_plan(plan(), CATS, "2")


def test_corrupt_fallback(d):
    hp.save_plan(plan(), CATS, None)
    hp.save_plan(plan(sleep=55, other=46), CATS, 1)
    (d / "hours_plan.json").write_text("{not json")
    assert hp.load_plan()["version"] == 1
    (d / "hours_plan.prev.json").write_text("garbage")
    assert hp.load_plan() is None


def test_save_over_corrupt_main_keeps_good_prev_and_never_goes_backwards(d):
    hp.save_plan(plan(), CATS, None)
    hp.save_plan(plan(sleep=55, other=46), CATS, 1)
    hp.save_plan(plan(), CATS, 2)  # main v3, prev v2
    (d / "hours_plan.json").write_text("oops")
    assert hp.load_plan()["version"] == 2
    with pytest.raises(hp.PlanConflict) as e:
        hp.save_plan(plan(), CATS, 1)
    assert e.value.current_version == 2
    s = hp.save_plan(plan(sleep=55, other=46), CATS, 2)
    assert s["version"] == 3  # v3 existed only in the corrupt file; highest readable is 2
    assert json.loads((d / "hours_plan.prev.json").read_text())["version"] == 2


def test_huge_hours_no_overflow():
    for bad in (1e308, 10 ** 400, -1e308):
        with pytest.raises(hp.PlanInvalid):
            hp.validate_plan(plan(sleep=bad), CATS)


@pytest.mark.parametrize("allowed", ["Sleep", [], ["A", "A"], ["A", " "], ["A", 3], None])
def test_bad_allowed_categories(allowed):
    with pytest.raises(hp.PlanInvalid):
        hp.validate_plan(plan(), allowed)


def test_semantically_bad_stored_file_ignored(d):
    hp.save_plan(plan(), CATS, None)
    hp.save_plan(plan(sleep=55, other=46), CATS, 1)
    bad = json.loads((d / "hours_plan.json").read_text())
    bad["hours"][0]["hours"] = 100  # total no longer 168
    (d / "hours_plan.json").write_text(json.dumps(bad))
    assert hp.load_plan()["version"] == 1
    bad["hours"][0]["hours"] = 55
    bad["version"] = 0
    (d / "hours_plan.json").write_text(json.dumps(bad))
    assert hp.load_plan()["version"] == 1


def test_storage_error_cleans_tmp(d, monkeypatch):
    hp.save_plan(plan(), CATS, None)
    before = (d / "hours_plan.json").read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(hp.os, "replace", boom)
    with pytest.raises(hp.PlanStorageError):
        hp.save_plan(plan(sleep=55, other=46), CATS, 1)
    monkeypatch.undo()
    assert sorted(p.name for p in d.iterdir()) == ["hours_plan.json"]
    assert (d / "hours_plan.json").read_bytes() == before


def test_dir_is_absolute_and_unset_warns(monkeypatch, caplog):
    monkeypatch.setenv("PERSONAL_PLAN_DIR", "rel_dir")
    assert os.path.isabs(hp._plan_dir())
    monkeypatch.delenv("PERSONAL_PLAN_DIR")
    with caplog.at_level("WARNING"):
        hp._plan_dir()
    assert "ephemeral" in caplog.text


def test_restart_persistence():
    s = hp.save_plan(plan(), CATS, None)
    importlib.reload(hp)
    assert hp.load_plan() == s


def test_default_dir_next_to_module(monkeypatch):
    monkeypatch.delenv("PERSONAL_PLAN_DIR")
    assert hp._plan_dir() == os.path.join(os.path.dirname(os.path.abspath(hp.__file__)), "personal_plan")


def test_source_clean():
    src = open(hp.__file__, encoding="utf-8").read().lower()
    code = src.split('"""', 2)[2]
    for word in ("sqlite3", "personal_snapshots", "flask"):
        assert word not in code


def test_concurrent_saves():
    hp.save_plan(plan(), CATS, None)
    res = []

    def go(i):
        try:
            hp.save_plan(plan(sleep=56 - i, other=45 + i), CATS, 1)
            res.append("ok")
        except hp.PlanConflict:
            res.append("conflict")
    ts = [threading.Thread(target=go, args=(i,)) for i in (1, 2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(res) == ["conflict", "ok"]
