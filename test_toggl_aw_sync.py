import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import toggl_aw_sync as S
from toggl_snapshot import TogglReplyError

FIX = json.loads((Path(__file__).parent / "fixtures" / "toggl_focus.json").read_text(encoding="utf-8"))
ENTRIES = FIX["time_entries_taskless"]
PROJECTS = FIX["projects"]
U = timezone.utc
WS = datetime(2026, 11, 1, tzinfo=U)
WE = datetime(2026, 12, 1, tzinfo=U)
NOW = datetime(2026, 11, 10, 16, 35, 2, 140000, tzinfo=U)
_counter = [1000]


def build(entries=ENTRIES, projects=PROJECTS, ws=WS, we=WE, now=NOW):
    return S.build_events(entries, projects, ws, we, now)


def apply(existing, plan):
    gone = set(plan["delete"])
    out = [e for e in existing if e["id"] not in gone]
    for ev in plan["insert"]:
        e = copy.deepcopy(ev)
        _counter[0] += 1
        e["id"] = _counter[0]
        out.append(e)
    return out


def by_id(events):
    return {e["data"]["toggl_id"]: e for e in events}


def test_fixture_builds_eight_events():
    ev, rep = build()
    assert len(ev) == 8 and rep["events"] == 8 and rep["running"] == 1
    m = by_id(ev)
    assert m["1028"]["duration"] == 60.0 and m["1028"]["timestamp"] == "2026-11-10T16:30:03.624Z"
    assert m["1002"]["data"]["running"] is True and m["1002"]["duration"] == 180.0
    assert m["1002"]["timestamp"] == "2026-11-10T16:32:02.140Z"
    assert sum(1 for e in ev if not e["data"]["running"]) == 7
    assert m["1022"]["duration"] == 24.0


def test_labels_and_projects():
    ev, rep = build()
    m = by_id(ev)
    assert m["1028"]["data"]["label"] == "Project 14"
    assert m["1023"]["data"]["label"] == "Project 7"
    assert m["1022"]["data"]["label"] == S.NO_PROJECT
    assert rep["no_project"] == 4 and rep["unknown_project"] == 0
    ent = {"data": [{"id": 1, "project_id": 99999, "start": "2026-11-10T10:00:00Z", "duration": 5}]}
    ev2, rep2 = build(ent)
    assert ev2[0]["data"]["label"] == S.NO_PROJECT and rep2["unknown_project"] == 1


def test_window_boundary():
    ev, _ = build(ws=datetime(2026, 11, 8, 5, tzinfo=U))
    assert "1027" in by_id(ev) and by_id(ev)["1027"]["timestamp"] == "2026-11-09T04:06:01.192Z"
    ev2, rep = build(ws=datetime(2026, 11, 9, 5, tzinfo=U))
    assert "1027" not in by_id(ev2) and rep["outside_window"] == 6


@pytest.mark.parametrize("bad", [{"error": "x"}, [], "s", None, {"data": "x"}])
def test_bad_replies_raise(bad):
    with pytest.raises(TogglReplyError):
        build(entries=bad)
    with pytest.raises(TogglReplyError):
        build(projects=bad)


def test_empty_is_fine():
    ev, rep = build({"data": []})
    assert ev == [] and rep["entries"] == 0


def test_unreadable_skipped():
    ok = "2026-11-10T10:00:00Z"
    rows = [{"id": 1, "start": "nope", "duration": 1},
            {"id": 2, "start": ok, "duration": "5"},
            {"id": 3, "start": ok, "duration": True},
            {"id": 4, "start": ok, "duration": float("nan")},
            {"id": 5, "start": 12345, "duration": 1}]
    ev, rep = build({"data": rows})
    assert ev == [] and rep["unreadable"] == 5 and rep["entries"] == 5


def test_running_before_start_is_zero():
    ev, _ = build(now=datetime(2026, 11, 10, 1, tzinfo=U))
    assert by_id(ev)["1002"]["duration"] == 0.0


def test_truncation_and_keys():
    rows = {"data": [{"id": 1, "project_id": 7, "start": "2026-11-10T10:00:00Z", "duration": 3,
                      "description": "  ZZ-CANARY-DESC-4471  " + "x" * 300, "type": "activity"}]}
    projs = {"data": [{"id": 7, "name": "  A   " + "b" * 100 + " "}]}
    ev, _ = build(rows, projs)
    d = ev[0]["data"]
    assert len(d["label"]) <= 60 and d["label"].startswith("A b")
    assert len(d["description"]) == 200 and d["description"].startswith("ZZ-CANARY-DESC-4471 x")
    assert set(d) == {"label", "running", "source", "toggl_id", "description", "type"}
    assert d["type"] == "activity" and d["toggl_id"] == "1"


def test_idempotent():
    wanted, _ = build()
    p1 = S.plan_sync(wanted, [], WS)
    assert len(p1["insert"]) == 8
    existing = apply([], p1)
    assert S.plan_sync(wanted, existing, WS) == {"insert": [], "delete": [], "keep": 8, "replace": 0}


@pytest.mark.parametrize("field", ["label", "description", "duration"])
def test_changed_entry_replaced(field):
    wanted, _ = build()
    existing = apply([], S.plan_sync(wanted, [], WS))
    new = copy.deepcopy(wanted)
    tgt = next(e for e in new if e["data"]["toggl_id"] == "1028")
    if field == "duration":
        tgt["duration"] = 75.0
    else:
        tgt["data"][field] = "changed"
    old_id = next(e["id"] for e in existing if e["data"]["toggl_id"] == "1028")
    plan = S.plan_sync(new, existing, WS)
    assert plan["delete"] == [old_id] and len(plan["insert"]) == 1 and plan["replace"] == 1
    after = apply(existing, plan)
    assert len(after) == len(existing)
    assert len({e["data"]["toggl_id"] for e in after}) == len(after)


def test_running_then_finished():
    wanted, _ = build()
    existing = apply([], S.plan_sync(wanted, [], WS))
    later, _ = build(now=NOW + timedelta(minutes=5))
    p = S.plan_sync(later, existing, WS)
    assert len(p["insert"]) == 1 and len(p["delete"]) == 1 and p["insert"][0]["duration"] == 480.0
    existing = apply(existing, p)
    fin = copy.deepcopy(later)
    t = next(e for e in fin if e["data"]["toggl_id"] == "1002")
    t["data"]["running"] = False
    t["duration"] = 500.0
    p = S.plan_sync(fin, existing, WS)
    assert len(p["insert"]) == 1 and p["replace"] == 1
    existing = apply(existing, p)
    assert S.plan_sync(fin, existing, WS) == {"insert": [], "delete": [], "keep": 8, "replace": 0}
    assert len(existing) == 8


def test_deleted_entry_window():
    wanted, _ = build()
    existing = apply([], S.plan_sync(wanted, [], WS))
    fewer = [e for e in wanted if e["data"]["toggl_id"] != "1028"]
    gone = next(e["id"] for e in existing if e["data"]["toggl_id"] == "1028")
    assert S.plan_sync(fewer, existing, WS)["delete"] == [gone]
    assert S.plan_sync(fewer, existing, datetime(2026, 11, 11, tzinfo=U))["delete"] == []


def test_foreign_invisible():
    wanted, _ = build()
    w = wanted[0]
    ts = w["timestamp"]
    foreign = [
        {"id": "a", "timestamp": ts, "duration": 1, "data": {"label": w["data"]["label"]}},
        {"id": "b", "timestamp": ts, "duration": 1,
         "data": {"source": "other", "toggl_id": w["data"]["toggl_id"]}},
        {"id": "c", "timestamp": ts, "duration": 1, "data": {"source": S.SOURCE}},
        {"id": "d", "timestamp": ts, "duration": 1, "data": "str"},
        {"id": "e", "timestamp": ts, "duration": 1, "data": None},
        {"id": "f", "timestamp": ts, "duration": 1, "data": {"source": S.SOURCE, "toggl_id": 5}},
    ]
    p = S.plan_sync(wanted, foreign, WS)
    assert p["delete"] == [] and len(p["insert"]) == 8


def test_duplicates_self_heal():
    wanted, _ = build()
    existing = apply([], S.plan_sync(wanted, [], WS))
    orig = next(e for e in existing if e["data"]["toggl_id"] == "1028")
    dup = copy.deepcopy(orig)
    dup["id"] = 9001
    p = S.plan_sync(wanted, existing + [dup], WS)
    assert p["insert"] == [] and len(p["delete"]) == 1 and p["keep"] == 8
    d1 = copy.deepcopy(orig)
    d1["id"], d1["duration"] = 9002, 5.0
    d2 = copy.deepcopy(orig)
    d2["id"], d2["duration"] = 9003, 6.0
    rest = [e for e in existing if e is not orig]
    p = S.plan_sync(wanted, rest + [d1, d2], WS)
    assert p["delete"] == [9002, 9003] and len(p["insert"]) == 1 and p["replace"] == 1


def test_deterministic_no_mutation():
    e0, p0 = copy.deepcopy(ENTRIES), copy.deepcopy(PROJECTS)
    a = build()
    assert a == build() and ENTRIES == e0 and PROJECTS == p0
    for ev in a[0]:
        json.dumps(ev, allow_nan=False)
    existing = apply([], S.plan_sync(a[0], [], WS))
    snap = copy.deepcopy((a[0], existing))
    assert S.plan_sync(a[0], existing, WS) == S.plan_sync(a[0], existing, WS)
    assert (a[0], existing) == snap


def test_module_is_pure():
    import re
    src = (Path(__file__).parent / "toggl_aw_sync.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:import|from)\s+([\w.]+)", src, re.M)
    for bad in ("os", "requests", "urllib", "socket", "sqlite3", "main", "flask", "time"):
        assert bad not in imports, bad
    assert ".now(" not in src and "utcnow" not in src


def test_null_or_negative_duration_is_running():
    rows = {"data": [{"id": 1, "start": "2026-11-10T16:30:00Z", "duration": None},
                     {"id": 2, "start": "2026-11-10T16:30:00Z", "duration": -1}]}
    ev, rep = build(rows)
    assert rep["running"] == 2 and all(e["data"]["running"] and e["duration"] == 302.0 for e in ev)


def test_unreadable_row_does_not_delete_existing_event():
    wanted, _ = build()
    existing = apply([], S.plan_sync(wanted, [], WS))
    rows = copy.deepcopy(ENTRIES)
    next(r for r in rows["data"] if r["id"] == 1028)["duration"] = "bad"
    w2, rep = build(rows)
    assert rep["unreadable_ids"] == ["1028"]
    p = S.plan_sync(w2, existing, WS, rep["unreadable_ids"])
    assert p["delete"] == [] and p["insert"] == []


def test_repeated_entry_id_settles():
    wanted, _ = build()
    dup = copy.deepcopy(next(e for e in wanted if e["data"]["toggl_id"] == "1028"))
    dup["duration"] = 999.0
    w2 = wanted + [dup]
    existing = apply([], S.plan_sync(w2, [], WS))
    assert len(existing) == 8
    assert S.plan_sync(w2, existing, WS) == {"insert": [], "delete": [], "keep": 8, "replace": 0}


def test_project_id_int_vs_str():
    ent = {"data": [{"id": 1, "project_id": "7", "start": "2026-11-10T10:00:00Z", "duration": 5},
                    {"id": 2, "project_id": 8, "start": "2026-11-10T11:00:00Z", "duration": 5}]}
    projs = {"data": [{"id": 7, "name": "Seven"}, {"id": "8", "name": "Eight"}]}
    ev, rep = build(ent, projs)
    assert [e["data"]["label"] for e in ev] == ["Seven", "Eight"] and rep["unknown_project"] == 0


def test_naive_datetime_raises():
    naive = datetime(2026, 11, 10)
    with pytest.raises(ValueError):
        build(ws=naive)
    with pytest.raises(ValueError):
        build(we=naive)
    with pytest.raises(ValueError):
        build(now=naive)
