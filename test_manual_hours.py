import importlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

import manual_hours as mh


@pytest.fixture(autouse=True)
def logdir(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_LOG_DIR", str(tmp_path))
    return tmp_path


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


NOW = utc(2026, 11, 4, 15, 0)  # Wednesday


def disk(d):
    return json.loads((d / "manual_hours.json").read_text())


@pytest.mark.parametrize("text,cat,hours", [
    ("side work 2h", "side work", 2.0),
    ("2h side work", "side work", 2.0),
    ("gym 1.5 hrs", "gym", 1.5),
    ("reading 45m", "reading", 0.75),
    ("study 1h30m", "study", 1.5),
    ("Side Work 2 hours", "Side Work", 2.0),
    ("for gym 1 hour", "gym", 1.0),
    ("on   side    work   2h", "side work", 2.0),
    ("side work 2h 30m", "side work", 2.5),
    ("gym 90 minutes", "gym", 1.5),
])
def test_accepted_phrasings(text, cat, hours):
    c, h, r = mh.parse_log_text(text)
    assert (c, h) == (cat, hours)
    assert r is False


def test_rounding_and_limits():
    assert mh.parse_log_text("x 20m") == ("x", 0.25, True)
    assert mh.parse_log_text("x 50m") == ("x", 0.75, True)
    for bad in ("x 7m", "x 25h", "x 0h"):
        with pytest.raises(mh.LogInvalid):
            mh.parse_log_text(bad)
    assert mh.parse_log_text("x 24h")[1] == 24.0


@pytest.mark.parametrize("bad", [
    "side work 2", "side work 2h 3h", "-2h side work", "gym 1,5h", "", "   ",
    "2h", "side work", True, None, 5, "x 2h 30m 10m",
])
def test_invalid_inputs_write_nothing(bad, logdir):
    with pytest.raises(mh.LogInvalid) as ei:
        mh.log_entry(bad, now=NOW)
    assert str(ei.value)
    assert not (logdir / "manual_hours.json").exists()


@pytest.mark.parametrize("bad", ["<b>x</b> 2h", "a" * 61 + " 2h", "123 2h", "a>b 2h"])
def test_bad_categories(bad):
    with pytest.raises(mh.LogInvalid):
        mh.parse_log_text(bad)


def test_log_and_list(logdir):
    e1 = mh.log_entry("side work 2h", now=NOW)
    e2 = mh.log_entry("gym 1h", now=NOW)
    assert (e1["id"], e2["id"]) == (1, 2)
    assert e1["week_start"] == "2026-11-02"
    assert datetime.fromisoformat(e1["logged_at"]).utcoffset() == timedelta(0)
    wk = mh.list_week(mh.current_week_start(NOW))
    assert [e["id"] for e in wk] == [1, 2]
    assert disk(logdir)["entries"][0]["category"] == "side work"
    assert mh.list_week("2026-10-26") == []


def test_week_boundary_detroit():
    # Sunday 23:30 Detroit (EST, UTC-5) = Monday 04:30 UTC
    assert mh.current_week_start(utc(2026, 11, 9, 4, 30)) == "2026-11-02"
    assert mh.current_week_start(utc(2026, 11, 9, 5, 30)) == "2026-11-09"
    e = mh.log_entry("gym 1h", now=utc(2026, 11, 9, 4, 30))
    assert e["week_start"] == "2026-11-02"


def test_week_boundary_dst_change():
    # DST ends Sun 2026-11-01 06:00 UTC; Monday 00:30 EST = 05:30 UTC
    assert mh.current_week_start(utc(2026, 11, 2, 4, 30)) == "2026-10-26"
    assert mh.current_week_start(utc(2026, 11, 2, 5, 30)) == "2026-11-02"
    # DST starts Sun 2026-03-08 07:00 UTC; Sunday 23:30 EDT = Monday 03:30 UTC
    assert mh.current_week_start(utc(2026, 3, 9, 3, 30)) == "2026-03-02"
    assert mh.current_week_start(utc(2026, 3, 9, 4, 30)) == "2026-03-09"


def test_canonical_category():
    assert mh.canonical_category("sleep", ["Sleep"]) == "Sleep"
    assert mh.canonical_category("  SLEEP ", ["Sleep"]) == "Sleep"
    assert mh.canonical_category("deep  work", ["Deep Work"]) == "Deep Work"
    assert mh.canonical_category("gym", ["Sleep"]) == "gym"
    assert mh.log_entry("sleep 8h", known_categories=["Sleep"], now=NOW)["category"] == "Sleep"


def test_remove_last(logdir):
    assert mh.remove_last() is None
    assert not (logdir / "manual_hours.json").exists()
    mh.log_entry("a 1h", now=NOW)
    e2 = mh.log_entry("b 1h", now=NOW)
    assert mh.remove_last() == e2
    assert [e["category"] for e in disk(logdir)["entries"]] == ["a"]
    assert mh.remove_last()["category"] == "a"
    assert mh.remove_last() is None
    assert mh.remove_last("2026-11-02") is None


def test_remove_last_by_week():
    mh.log_entry("a 1h", now=NOW)
    mh.log_entry("b 1h", now=NOW + timedelta(days=7))
    assert mh.remove_last("2026-11-02")["category"] == "a"
    assert [e["category"] for e in mh.list_all()] == ["b"]


def test_cap_500_and_ids_never_reused(logdir):
    for i in range(505):
        mh.log_entry("a 0.25h", now=NOW + timedelta(minutes=i))
    # week cap would hit at 168h only after 672 entries of 0.25h; ok
    es = mh.list_all()
    assert len(es) == 500
    assert es[0]["id"] == 6 and es[-1]["id"] == 505
    assert mh.remove_last()["id"] == 505
    assert mh.log_entry("a 1h", now=NOW)["id"] == 506


def test_week_168_cap(logdir):
    for _ in range(6):
        mh.log_entry("a 24h", now=NOW)
    mh.log_entry("a 23h", now=NOW)
    before = (logdir / "manual_hours.json").read_text()
    with pytest.raises(mh.LogInvalid) as ei:
        mh.log_entry("a 2h", now=NOW)
    assert "167" in str(ei.value)
    assert (logdir / "manual_hours.json").read_text() == before


def test_week_168_exact_ok():
    for _ in range(7):
        mh.log_entry("a 24h", now=NOW)
    # 168 exactly is allowed
    assert sum(e["hours"] for e in mh.list_all()) == 168
    with pytest.raises(mh.LogInvalid):
        mh.log_entry("a 15m", now=NOW)


def test_corrupt_file(logdir):
    (logdir / "manual_hours.json").write_text("{not json")
    assert mh.list_all() == []
    e = mh.log_entry("a 1h", now=NOW)
    assert e["id"] == 1
    assert disk(logdir)["entries"][0]["id"] == 1


def test_persistence_across_reload():
    mh.log_entry("a 1h", now=NOW)
    before = mh.list_all()
    importlib.reload(mh)
    assert mh.list_all() == before
    assert mh.log_entry("b 1h", now=NOW)["id"] == 2


def test_concurrent_logging():
    def work():
        for _ in range(20):
            mh.log_entry("a 0.25h", now=NOW)

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    es = mh.list_all()
    assert len(es) == 40
    assert len({e["id"] for e in es}) == 40


def test_default_dir_next_to_module(monkeypatch):
    monkeypatch.delenv("PERSONAL_LOG_DIR", raising=False)
    d = mh._log_dir()
    assert os.path.dirname(d) == os.path.dirname(os.path.abspath(mh.__file__))
    assert os.path.basename(d) == "personal_log"


def test_source_has_no_forbidden_imports():
    src = open(mh.__file__, encoding="utf-8").read()
    for bad in ("sqlite3", "flask", "personal_snapshots", "hours_plan", "finance_model", "import main"):
        assert bad not in src


@pytest.mark.parametrize("bad", ["gym 1h 30", "2h 30 gym", "for 2h", "on 2h", "gym -2h", "-2h",
                                 "gym " + "9" * 400 + "h", "9" * 400 + "m gym", "gym 1" + "0" * 30 + "h"])
def test_bare_number_connector_only_negative_overflow_invalid(bad):
    with pytest.raises(mh.LogInvalid):
        mh.parse_log_text(bad)


@pytest.mark.parametrize("text", ["side work for 2h", "side work, 2h", "side work 2h.", "side work - 2h",
                                  "side work -- 2h", "2h - side work", "for side work 2h", "side work on 2h",
                                  "gym -- 1h"])
def test_connectors_and_separators_stripped(text):
    cat, hours, _ = mh.parse_log_text(text)
    assert cat in ("side work", "gym") and hours in (2.0, 1.0)


def test_negative_message_and_dash_separator():
    with pytest.raises(mh.LogInvalid) as ei:
        mh.parse_log_text("-2h")
    assert "negative" in str(ei.value)


def test_read_drops_nonfinite_and_string_hours(logdir):
    (logdir / "manual_hours.json").write_text(
        '{"next_id": 5, "entries": [{"id": 1, "hours": NaN, "week_start": "2026-11-02"},'
        '{"id": 2, "hours": Infinity, "week_start": "2026-11-02"},'
        '{"id": 3, "hours": "2", "week_start": "2026-11-02"},'
        '{"id": 4, "hours": 1.0, "week_start": "2026-11-02"}]}')
    assert [e["id"] for e in mh.list_all()] == [4]
    assert mh.log_entry("a 1h", now=NOW)["id"] == 5
