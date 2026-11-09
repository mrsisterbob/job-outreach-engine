import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import presence_events as pe

T0 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
PLACES = {"library": "Job search", "home": "Home", "desk": "Work", "couch": "Rest"}


def s(n):
    return T0 + timedelta(seconds=n)


@pytest.fixture(autouse=True)
def logdir(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_LOG_DIR", str(tmp_path))
    return tmp_path


def lines(path, name="presence_2026-11.jsonl"):
    return (path / name).read_text().splitlines()


def test_parse_places_valid_and_blank():
    assert pe.parse_places('{"library": " Job   search ", "home": "Home"}') == {
        "library": "Job search", "home": "Home"}
    for blank in (None, "", "   \n"):
        assert pe.parse_places(blank) == {}


@pytest.mark.parametrize("bad", [
    "ZZ-CANARY-4471 not json", '["ZZ-CANARY-4471"]',
    json.dumps({"k%d" % i: "v" for i in range(11)}),
    '{"ZZ-CANARY-4471": "v"}', json.dumps({"a" * 33: "v"}),
    '{"library": "  "}', json.dumps({"library": "x" * 41}), '{"library": 5}',
])
def test_parse_places_invalid(bad):
    with pytest.raises(pe.PresenceInvalid) as ei:
        pe.parse_places(bad)
    assert "CANARY" not in str(ei.value) and "library" not in str(ei.value)


def test_parse_event_ok_and_failures():
    assert pe.parse_event({"place": "library", "event": " JOIN "}, PLACES) == ("library", "join")
    assert pe.parse_event({"place": "desk", "event": " TAP ", "x": 1}, PLACES) == ("desk", "tap")
    cases = [{"place": "ZZ-CANARY-4471", "event": "join"}, {}, {"place": "library"},
             {"event": "join"}, "ZZ-CANARY-4471", None, ["library"],
             {"place": "library", "event": "arrive"}, {"place": ["library"], "event": "join"},
             {"place": "library", "event": 5}]
    for c in cases:
        with pytest.raises(pe.PresenceInvalid) as ei:
            pe.parse_event(c, PLACES)
        assert str(ei.value) in ("unknown place", "bad event", "bad payload")
        assert "CANARY" not in str(ei.value)


def test_write_path(logdir):
    r = pe.record("library", "join", now=T0)
    assert r["status"] == "ok"
    assert pe.recent(now=T0) == [r["entry"]]
    assert r["entry"]["received_at"] == T0.isoformat()
    ls = lines(logdir)
    assert len(ls) == 1 and json.loads(ls[0]) == r["entry"]


def test_dedup_boundary(logdir):
    pe.record("library", "join", now=T0)
    assert pe.record("library", "join", now=s(89)) == {"status": "duplicate"}
    assert len(lines(logdir)) == 1
    assert pe.record("library", "join", now=s(91))["status"] == "ok"
    assert len(lines(logdir)) == 2
    assert pe.record("library", "leave", now=s(92))["status"] == "ok"
    assert pe.record("home", "join", now=s(93))["status"] == "ok"


def test_leave_first_recorded():
    assert pe.record("library", "leave", now=T0)["status"] == "ok"
    assert pe.place_state("library", now=T0) == "out"


def test_rate_cap():
    for i in range(60):
        r = pe.record("library", "join" if i % 2 == 0 else "leave", now=s(i * 60))
        assert r["status"] == "ok", i
    assert pe.record("home", "join", now=s(3599))["status"] == "rate_limited"
    assert len(pe.recent(500, now=s(3599))) == 60
    assert pe.record("home", "join", now=s(3700))["status"] == "ok"


def test_place_state_follows_log():
    assert pe.place_state("library", now=T0) == "unknown"
    pe.record("library", "join", now=T0)
    assert pe.place_state("library", now=s(1)) == "in"
    pe.record("library", "leave", now=s(100))
    assert pe.place_state("library", now=s(101)) == "out"
    pe.record("library", "leave", now=s(110))
    assert pe.place_state("library", now=s(111)) == "out"
    assert pe.place_state("home", now=s(111)) == "unknown"


def test_month_rollover(logdir):
    a = datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc)
    b = datetime(2026, 12, 1, 0, 1, tzinfo=timezone.utc)
    pe.record("library", "join", now=a)
    pe.record("home", "join", now=b)
    assert len(lines(logdir, "presence_2026-11.jsonl")) == 1
    assert len(lines(logdir, "presence_2026-12.jsonl")) == 1
    assert [e["place"] for e in pe.recent(now=b)] == ["home", "library"]
    assert pe.place_state("library", now=b) == "in"
    c = datetime(2026, 12, 1, 0, 0, 20, tzinfo=timezone.utc)
    assert pe.record("library", "join", now=c)["status"] == "duplicate"


def test_corrupt_and_blank_lines(logdir):
    good = json.dumps({"received_at": T0.isoformat(), "place": "library", "event": "join"})
    (logdir / "presence_2026-11.jsonl").write_text(
        "{broken\n\n" + good + "\n[1]\n"
        '{"received_at": "nope", "place": "library", "event": "join"}\n')
    assert len(pe.recent(now=s(1))) == 1
    assert pe.place_state("library", now=s(1)) == "in"
    assert pe.record("library", "join", now=s(10)) == {"status": "duplicate"}
    assert pe.record("library", "leave", now=s(11))["status"] == "ok"


def test_unwritable_directory(tmp_path, monkeypatch):
    f = tmp_path / "afile"
    f.write_text("x")
    monkeypatch.setenv("PERSONAL_LOG_DIR", str(f))
    with pytest.raises(OSError):
        pe.record("library", "join", now=T0)


def test_limit_clamp():
    for i in range(3):
        pe.record("library", "join" if i % 2 == 0 else "leave", now=s(i * 100))
    assert len(pe.recent(0, now=s(400))) == 1
    assert len(pe.recent(10**6, now=s(400))) == 3
    assert len(pe.recent("x", now=s(400))) == 3
    assert pe.recent(2, now=s(400))[0]["received_at"] == s(200).isoformat()


def test_nothing_outside_dir_and_source_clean(tmp_path):
    before = sorted(os.listdir(tmp_path.parent))
    pe.record("library", "join", now=T0)
    assert sorted(os.listdir(tmp_path.parent)) == before
    src = open(pe.__file__, encoding="utf-8").read()
    assert not re.search(r"^\s*(import|from)\s+(requests|urllib|flask|sqlite3|main)\b", src, re.M)
    assert "toggl" not in src.lower()


def test_tap():
    assert pe.place_state("desk", now=T0) == "unknown"
    r = pe.record("desk", "tap", now=T0)
    assert r["status"] == "ok" and r["entry"]["event"] == "tap"
    assert pe.recent(now=T0)[0]["event"] == "tap"
    assert pe.place_state("desk", now=s(1)) == "in"


def test_tap_dedup_and_independence(logdir):
    pe.record("desk", "tap", now=T0)
    assert pe.record("desk", "tap", now=s(89)) == {"status": "duplicate"}
    assert len(lines(logdir)) == 1
    assert pe.record("desk", "tap", now=s(91))["status"] == "ok"
    assert pe.record("couch", "tap", now=s(96))["status"] == "ok"
    assert pe.record("desk", "join", now=s(97))["status"] == "ok"
    for i in range(57):
        pe.record("library", "join" if i % 2 == 0 else "leave", now=s(100 + i))
    assert pe.record("home", "tap", now=s(200))["status"] == "rate_limited"


def test_torn_tail_gets_newline(logdir):
    good = json.dumps({"received_at": T0.isoformat(), "place": "home", "event": "join"})
    (logdir / "presence_2026-11.jsonl").write_text(good + '\n{"received_at": "2026-11')
    assert pe.record("library", "join", now=s(10))["status"] == "ok"
    got = pe.recent(now=s(11))
    assert [e["place"] for e in got] == ["library", "home"]


def test_short_write_loops(monkeypatch, logdir):
    real = os.write
    calls = []

    def slow(fd, data):
        calls.append(1)
        return real(fd, bytes(data)[:5])
    monkeypatch.setattr(pe.os, "write", slow)
    assert pe.record("library", "join", now=T0)["status"] == "ok"
    assert len(calls) > 1
    assert pe.recent(now=T0)[0]["place"] == "library"
    assert len(lines(logdir)) == 1


def test_short_write_zero_raises(monkeypatch, logdir):
    monkeypatch.setattr(pe.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        pe.record("library", "join", now=T0)


def test_record_rejects_invalid(logdir):
    for p, e in (("anything goes", "x"), ("library", "x"), ("Bad", "join"), (5, "join"),
                 ("library", None), ("ZZ-CANARY-4471" * 5, "join")):
        with pytest.raises(pe.PresenceInvalid) as ei:
            pe.record(p, e, now=T0)
        assert "CANARY" not in str(ei.value)
    assert list(logdir.iterdir()) == []


def test_recent_limit_inf():
    pe.record("library", "join", now=T0)
    assert len(pe.recent(float("inf"), now=T0)) == 1
    assert len(pe.recent(float("nan"), now=T0)) == 1


def test_naive_timestamp_is_utc(logdir):
    (logdir / "presence_2026-11.jsonl").write_text(
        json.dumps({"received_at": "2026-11-10T12:00:00", "place": "library",
                    "event": "join"}) + "\n")
    assert pe.record("library", "join", now=s(30)) == {"status": "duplicate"}
    assert pe.record("library", "join", now=s(100))["status"] == "ok"


def test_future_line_ignored_for_dedup_and_rate(logdir):
    fut = (T0 + timedelta(hours=5)).isoformat()
    rows = [json.dumps({"received_at": fut, "place": "library", "event": "join"})]
    rows += [json.dumps({"received_at": fut, "place": "p%d" % i, "event": "join"})
             for i in range(70)]
    (logdir / "presence_2026-11.jsonl").write_text("\n".join(rows) + "\n")
    assert pe.record("library", "join", now=T0)["status"] == "ok"


def test_recent_projects_fields(logdir):
    (logdir / "presence_2026-11.jsonl").write_text(
        json.dumps({"received_at": T0.isoformat(), "place": "home", "event": "join",
                    "secret": "ZZ-CANARY-4471"}) + "\n")
    assert pe.recent(now=T0) == [{"received_at": T0.isoformat(), "place": "home",
                                  "event": "join"}]
