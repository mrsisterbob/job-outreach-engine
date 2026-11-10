import json
import os
from datetime import datetime, timezone

import pytest

import personal_snapshots
import toggl_snapshot as ts
from activity_snapshot import week_bounds

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = json.load(open(os.path.join(HERE, "fixtures", "toggl_focus.json"), encoding="utf-8"))
ENTRIES = FIX["time_entries_taskless"]
PROJECTS = FIX["projects"]
CANARY = "ZZ-CANARY-DESC-4471"


def utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def window(day):
    wb = week_bounds(utc(day + "T12:00:00Z"))
    return utc(wb["start"]), utc(wb["end"])


NOW = utc("2026-11-10T18:32:02Z")  # running entry started 16:32:02.14 -> 7199.86 s
W2 = window("2026-11-04")
W9 = window("2026-11-10")


def build(entries, projects=PROJECTS, w=W9, now=NOW, **kw):
    return ts.build_toggl_section(entries, projects, w[0], w[1], now, **kw)


_IDS = iter(range(100000, 10**9))


def ent(start, duration=None, pid=None, **extra):
    e = {"id": next(_IDS), "start": start, "project_id": pid, "description": CANARY}
    if duration is not None:
        e["duration"] = duration
    e.update(extra)
    return e


def projs(*pairs):
    return {"data": [{"id": i, "name": n, "total_tracked_secs": 999} for i, n in pairs]}


def test_fixture_week_nov2():
    sec = build(ENTRIES, w=W2)
    assert sec["week_of"] == "2026-11-02"
    assert sec["as_of"] == "2026-11-10"
    assert sec["actual"] == [{"category": "Other", "hours": 0.01}]  # 27 s; 2 s rows omitted


def test_fixture_week_nov9_with_running():
    sec = build(ENTRIES, w=W9)
    # running entry: 16:32:02.14 -> 18:32:02 = 7199.86 s = 2.0 h; 60 s entry = 0.02 h
    assert sec["actual"] == [{"category": "Project 2", "hours": 2.0},
                             {"category": "Project 14", "hours": 0.02}]
    d = ts.describe_week(ENTRIES, PROJECTS, W9[0], W9[1], NOW)
    assert d["running_included"] is True
    assert d["unreadable"] == 0


def test_every_finished_second_once():
    fin = {"data": [e for e in ENTRIES["data"] if "duration" in e]}
    assert sum(e["duration"] for e in fin["data"]) == 91
    secs2, _, _ = ts._tally(fin, PROJECTS, W2[0], W2[1], NOW, None)
    secs9, _, _ = ts._tally(fin, PROJECTS, W9[0], W9[1], NOW, None)
    assert sum(secs2.values()) == 31 and sum(secs9.values()) == 60
    assert secs2["Other"] == 27  # includes the 04:06:01Z Nov 9 entry (Nov 8 23:06 Detroit)


def test_running_before_start_and_clipped():
    run = {"data": [ent("2026-11-10T16:32:02Z")]}
    assert build(run, now=utc("2026-11-10T10:00:00Z"))["actual"] == []
    clip = {"data": [ent("2026-11-15T21:00:00Z")]}  # Sunday 16:00 Detroit
    d = ts.describe_week(clip, projs(), W9[0], W9[1], utc("2026-11-20T00:00:00Z"))
    assert d["tracked_hours"] == 8.0  # to Monday 00:00 Detroit = 05:00Z -> 8 h


def test_no_project_and_unknown_project():
    e = {"data": [ent("2026-11-10T10:00:00Z", 3600, None), ent("2026-11-10T11:00:00Z", 3600, 777),
                  {"start": "2026-11-10T12:00:00Z", "duration": 3600}]}
    sec = build(e, projs((1, "Dog")))
    assert sec["actual"] == [{"category": "Other", "hours": 3.0}]
    d = ts.describe_week(e, projs((1, "Dog")), W9[0], W9[1], NOW)
    assert d["no_project_entries"] == 2 and d["unknown_project_entries"] == 1
    assert d["other_hours"] == 3.0


def test_straddle_dst_week():
    w = window("2026-10-27")
    assert (w[1] - w[0]).total_seconds() == 169 * 3600
    e = {"data": [ent("2026-11-02T04:00:00Z", 7200, 1)]}  # window ends 05:00Z
    assert w[1] == utc("2026-11-02T05:00:00Z")
    sec = ts.build_toggl_section(e, projs((1, "Dog")), w[0], w[1], w[1])
    assert sec["actual"] == [{"category": "Dog", "hours": 1.0}]
    assert sec["week_of"] == "2026-10-26"


def test_category_map():
    e = {"data": [ent("2026-11-10T10:00:00Z", 3600, 1), ent("2026-11-10T12:00:00Z", 1800, 2)]}
    p = projs((1, "Walks"), (2, "dog park"))
    sec = build(e, p, category_map={"WALKS": "Dog", "Dog Park": "Dog"})
    assert sec["actual"] == [{"category": "Dog", "hours": 1.5}]


def test_check_reply():
    assert ts.check_reply(200, "{}") is None
    for st in (401, 403):
        with pytest.raises(ts.TogglTokenRejected) as ei:
            ts.check_reply(st, "x" * 500)
        assert "TOGGL_API_TOKEN" in str(ei.value) and "x" * 101 not in str(ei.value)
    with pytest.raises(ts.TogglReplyError) as ei:
        ts.check_reply(402, "", {"X-Toggl-Quota-Resets-In": "1234"})
    assert not isinstance(ei.value, ts.TogglTokenRejected) and "1234" in str(ei.value)
    for st in (204, 429, 500):
        with pytest.raises(ts.TogglReplyError):
            ts.check_reply(st, "y" * 500)
    with pytest.raises(ts.TogglReplyError) as ei:
        ts.check_reply(500, "y" * 500)
    assert "y" * 101 not in str(ei.value)


BAD = [{"error": "bad"}, {"message": "x"}, [], "Incorrect username", None, {"data": "x"}]


@pytest.mark.parametrize("bad", BAD)
def test_loud_failure(bad):
    with pytest.raises(ts.TogglReplyError):
        build(bad)
    with pytest.raises(ts.TogglReplyError):
        build(ENTRIES, bad)
    with pytest.raises(ts.TogglReplyError):
        ts.describe_week(bad, PROJECTS, W9[0], W9[1], NOW)


def test_empty_week_is_legit():
    assert build({"data": []})["actual"] == []


def test_unreadable_skipped():
    e = {"data": [ent("not a date", 3600, 1), ent("2026-11-10T10:00:00Z", "3600", 1),
                  ent("2026-11-10T10:00:00Z", True, 1), ent("2026-11-10T10:00:00Z", float("nan"), 1),
                  "junk"]}
    p = projs((1, "Dog"))
    assert build(e, p)["actual"] == []
    assert ts.describe_week(e, p, W9[0], W9[1], NOW)["unreadable"] == 5


def test_name_cut_and_collapsed():
    e = {"data": [ent("2026-11-10T10:00:00Z", 3600, 1), ent("2026-11-10T12:00:00Z", 3600, 2)]}
    p = projs((1, "A" * 60), (2, "  Walk   the\tdog "))
    cats = {r["category"] for r in build(e, p)["actual"]}
    assert cats == {"A" * 40, "Walk the dog"}
    assert ts.describe_week(e, p, W9[0], W9[1], NOW)["categories_cut"] == 1


def test_section_contract_clean():
    e = {"data": [ent("2026-11-10T10:00:00Z", 3600, 1)]}
    sec = build(e, projs((1, "Dog")))
    assert set(sec) == {"as_of", "week_of", "actual"}
    assert all(set(r) == {"category", "hours"} for r in sec["actual"])
    assert CANARY not in json.dumps(sec) and "999" not in json.dumps(sec)
    real = build(ENTRIES)
    blob = json.dumps(real)
    for ident in ("1002", "1003", "1017", "1001", "1004"):
        assert ident not in blob
    json.dumps(real, allow_nan=False)


def test_write_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_SNAPSHOT_DIR", str(tmp_path))
    e = {"data": [ent("2026-11-10T10:00:00Z", 3600, 1)]}
    sec = build(e, projs((1, "Vendor meetings")))
    personal_snapshots.validate_snapshot({"toggl": sec})
    personal_snapshots.save_snapshot({"toggl": sec}, now=NOW)
    loaded = personal_snapshots.load_latest()
    assert loaded["toggl"] == sec
    assert sec["actual"][0]["category"] == "Vendor meetings"


def test_module_is_pure_and_uses_now():
    src = open(os.path.join(HERE, "toggl_snapshot.py"), encoding="utf-8").read()
    for mod in ("requests", "urllib", "socket", "sqlite3", "main", "flask", "os"):
        assert f"import {mod}" not in src and f"from {mod}" not in src
    sec = build({"data": []}, now=utc("2026-11-10T03:00:00Z"))  # 22:00 Nov 9 Detroit
    assert sec["as_of"] == "2026-11-09"


def test_running_when_duration_null_or_negative():
    for d in (None, -1):
        x = ent("2026-11-10T16:32:02Z", None, 1)
        x["duration"] = d
        e = {"data": [x]}
        sec = build(e, projs((1, "Dog")))
        assert sec["actual"] == [{"category": "Dog", "hours": 2.0}]
        assert ts.describe_week(e, projs((1, "Dog")), W9[0], W9[1], NOW)["running_included"]


def test_headers_mapping_case_insensitive():
    from collections import UserDict

    class CI(UserDict):
        def __getitem__(self, k):
            return super().__getitem__(k.lower())

    h = CI()
    h.data["x-toggl-quota-resets-in"] = "77"
    with pytest.raises(ts.TogglReplyError) as ei:
        ts.check_reply(402, "", h)
    assert "77" in str(ei.value)
    with pytest.raises(ts.TogglReplyError) as ei:
        ts.check_reply(402, "", {"x-toggl-quota-resets-in": "88"})
    assert "88" in str(ei.value)


def test_huge_duration_unreadable():
    e = {"data": [ent("2026-11-10T10:00:00Z", 1e18, 1), ent("2026-11-10T10:00:00Z", 10**30, 1)]}
    assert build(e, projs((1, "Dog")))["actual"] == []
    assert ts.describe_week(e, projs((1, "Dog")), W9[0], W9[1], NOW)["unreadable"] == 2


def test_unhashable_ids_unreadable():
    e = {"data": [ent("2026-11-10T10:00:00Z", 3600, [1]), ent("2026-11-10T10:00:00Z", 3600, {"a": 1}),
                  ent("2026-11-10T11:00:00Z", 3600, 1)]}
    p = {"data": [{"id": [1], "name": "Bad"}, {"id": 1, "name": "Dog"}]}
    assert build(e, p)["actual"] == [{"category": "Dog", "hours": 1.0}]
    assert ts.describe_week(e, p, W9[0], W9[1], NOW)["unreadable"] == 2


def test_category_map_values_cleaned():
    e = {"data": [ent("2026-11-10T10:00:00Z", 3600, 1), ent("2026-11-10T12:00:00Z", 3600, 2)]}
    p = projs((1, "Walks"), (2, "Cats"))
    sec = build(e, p, category_map={"walks": "  " + "B" * 60 + "  ", "cats": 5})
    assert {r["category"] for r in sec["actual"]} == {"B" * 40, "Cats"}
    assert ts.describe_week(e, p, W9[0], W9[1], NOW, category_map={"walks": "B" * 60})["categories_cut"] == 1


def test_dedupe_by_entry_id():
    a = ent("2026-11-10T10:00:00Z", 3600, 1)
    b = ent("2026-11-10T14:00:00Z", 1800, 1)
    b["id"] = None
    c = dict(b)
    e = {"data": [a, dict(a), b, c]}
    assert build(e, projs((1, "Dog")))["actual"] == [{"category": "Dog", "hours": 2.0}]


def test_error_message_has_no_body():
    for st in (500, 418, 402, 429, 401):
        with pytest.raises(ts.TogglReplyError) as ei:
            ts.check_reply(st, "oops " + CANARY)
        assert CANARY not in str(ei.value)
