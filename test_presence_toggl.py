"""Tests for presence_toggl. The fake http is always injected; nothing reaches Toggl."""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import presence_toggl as pt

TOKEN = "tok-ZZ-4471"
T0 = datetime(2026, 11, 10, 15, 0, 0, tzinfo=timezone.utc)
PLACES = {"library": "Job search", "home": "Home life"}


class Fake:
    def __init__(self, projects=None):
        self.running = None
        self.next_id = 7001
        self.projects = projects if projects is not None else [
            {"id": 501, "name": "Job search"}, {"id": 502, "name": "Home life"}]
        self.requests = []
        self.fail = {}          # kind -> (status, headers)
        self.headers = {}       # added to every 200 reply
        self.gate = None

    def kinds(self):
        return [r for r in self.requests]

    def __call__(self, method, url, headers=None, json=None, timeout=20):
        if self.gate is not None:
            self.gate.wait(5)
        if "/tracking/current" in url:
            kind = "current"
        elif "/tracking/start" in url:
            kind = "start"
        elif "/tracking/stop" in url:
            kind = "stop"
        else:
            kind = "projects"
        self.requests.append(kind)
        if kind in self.fail:
            st, hd = self.fail[kind]
            return st, hd, b"{}"
        h = dict(self.headers)
        if kind == "current":
            if self.running is None:
                return 204, h, b""
            return 200, h, _j(self.running)
        if kind == "projects":
            return 200, h, _j({"data": self.projects})
        if kind == "start":
            self.running = {"id": self.next_id, "project_id": json["project_id"],
                            "start": json["start"]}
            self.next_id += 1
            return 200, h, _j(self.running)
        e = dict(self.running)
        e["duration"] = 5
        self.running = None
        return 200, h, _j(e)


def _j(o):
    return json.dumps(o).encode()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("PERSONAL_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("PRESENCE_PLACES", json.dumps(PLACES))
    monkeypatch.setenv("TOGGL_API_TOKEN", TOKEN)
    monkeypatch.setenv("TOGGL_ORG_ID", "4001")
    monkeypatch.setenv("TOGGL_WORKSPACE_ID", "4002")
    return tmp_path


def _state(d):
    with open(os.path.join(str(d), pt.STATE_FILE)) as f:
        return json.load(f)


def test_presence_toggl_join_starts_one_entry_on_mapped_project(env):
    f = Fake()
    assert pt.apply("library", "join", now=T0, http=f) == "started"
    assert f.running["project_id"] == 501
    st = _state(env)
    assert st["entry"] == {"entry_id": f.running["id"], "place": "library",
                           "started_at": "2026-11-10T15:00:00Z"}


def test_presence_toggl_write_path_fresh_leave_stops_same_entry(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    eid = f.running["id"]
    assert _state(env)["entry"]["entry_id"] == eid
    f.requests.clear()
    assert pt.apply("library", "leave", now=T0 + timedelta(minutes=5), http=f) == "stopped"
    assert f.running is None and f.requests == ["current", "stop"]
    assert _state(env)["entry"] is None


@pytest.mark.parametrize("event", ["join", "tap"])
def test_presence_toggl_foreign_timer_is_never_touched(env, event):
    f = Fake()
    f.running = {"id": 9, "project_id": None, "start": "2026-11-10T14:00:00Z"}
    assert pt.apply("library", event, now=T0, http=f) == "skipped_foreign_timer"
    assert f.requests == ["current"] and f.running["id"] == 9
    assert not os.path.exists(os.path.join(str(env), pt.STATE_FILE))


def test_presence_toggl_leave_after_hand_stop_sends_no_stop(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    f.running = None
    f.requests.clear()
    assert pt.apply("library", "leave", now=T0, http=f) == "already_stopped"
    assert f.requests == ["current"] and _state(env)["entry"] is None


def test_presence_toggl_leave_when_kevin_started_his_own_sends_no_stop(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    f.running = {"id": 9, "project_id": None, "start": "2026-11-10T15:10:00Z"}
    f.requests.clear()
    assert pt.apply("library", "leave", now=T0, http=f) == "skipped_foreign_timer"
    assert "stop" not in f.requests and f.running["id"] == 9
    assert _state(env)["entry"] is None


def test_presence_toggl_leave_with_nothing_to_stop_makes_no_request(env):
    f = Fake()
    assert pt.apply("library", "leave", now=T0, http=f) == "nothing_to_stop"
    pt.apply("library", "join", now=T0, http=f)
    f.requests.clear()
    assert pt.apply("home", "leave", now=T0, http=f) == "nothing_to_stop"
    assert f.requests == []


def test_presence_toggl_switch_place_stops_old_then_starts_new(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    f.requests.clear()
    assert pt.apply("home", "join", now=T0 + timedelta(minutes=1), http=f) == "started"
    assert f.requests == ["current", "stop", "projects", "start"]
    assert f.running["project_id"] == 502
    assert _state(env)["entry"]["place"] == "home"


def test_presence_toggl_unknown_project_skips_and_is_not_cached(env):
    f = Fake(projects=[])
    assert pt.apply("library", "join", now=T0, http=f) == "skipped_no_project"
    assert "start" not in f.requests and f.running is None
    assert not os.path.exists(os.path.join(str(env), pt.CACHE_FILE))


@pytest.mark.parametrize("key,val", [
    ("TOGGL_API_TOKEN", ""), ("TOGGL_API_TOKEN", None), ("TOGGL_ORG_ID", "abc"),
    ("TOGGL_ORG_ID", None), ("TOGGL_WORKSPACE_ID", "12x"), ("TOGGL_WORKSPACE_ID", " ")])
def test_presence_toggl_bad_config_never_calls_http(env, monkeypatch, key, val):
    if val is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, val)
    f = Fake()
    assert pt.apply("library", "join", now=T0, http=f) == "skipped_no_config"
    assert f.requests == []


def test_presence_toggl_401_and_stop_500_leave_state_alone(env):
    f = Fake()
    f.fail["current"] = (401, {})
    assert pt.apply("library", "join", now=T0, http=f) == "error:TogglTokenRejected"
    assert not os.path.exists(os.path.join(str(env), pt.STATE_FILE))
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    before = _state(env)
    f.fail["stop"] = (500, {})
    assert pt.apply("library", "leave", now=T0, http=f) == "error:TogglError"
    assert _state(env) == before
    del f.fail["stop"]
    assert pt.apply("library", "leave", now=T0, http=f) == "stopped"


def test_presence_toggl_quota_block_window(env):
    f = Fake()
    f.fail["current"] = (402, {"X-Toggl-Quota-Resets-In": "600"})
    assert pt.apply("library", "join", now=T0, http=f) == "error:TogglQuotaExceeded"
    f.requests.clear()
    assert pt.apply("library", "join", now=T0 + timedelta(minutes=5), http=f) == "skipped_over_quota"
    assert f.requests == []
    del f.fail["current"]
    assert pt.apply("library", "join", now=T0 + timedelta(minutes=11), http=f) == "started"


def test_presence_toggl_quota_missing_header_blocks_fifteen_minutes(env):
    f = Fake()
    f.fail["current"] = (402, {})
    pt.apply("library", "join", now=T0, http=f)
    assert _state(env)["quota"]["blocked_until"] == "2026-11-10T15:15:00Z"


def test_presence_toggl_low_remaining_gates_next_event_only_with_header(env):
    f = Fake()
    f.headers = {"X-Toggl-Quota-Remaining": "2"}
    pt.apply("library", "join", now=T0, http=f)
    f.requests.clear()
    assert pt.apply("home", "tap", now=T0 + timedelta(minutes=1), http=f) == "skipped_low_quota"
    assert f.requests == []
    # no header at all: nothing is gated (fresh state directory)
    os.remove(os.path.join(str(env), pt.STATE_FILE))
    g = Fake()
    pt.apply("library", "join", now=T0, http=g)
    g.requests.clear()
    assert pt.apply("library", "leave", now=T0, http=g) == "stopped"


def test_presence_toggl_request_budget(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    assert f.requests == ["current", "projects", "start"]          # cold = 3
    pt.apply("library", "leave", now=T0, http=f)
    f.requests.clear()
    pt.apply("library", "tap", now=T0 + timedelta(minutes=1), http=f)
    assert f.requests == ["current", "start"]                      # warm = 2
    f.requests.clear()
    pt.apply("library", "leave", now=T0 + timedelta(minutes=2), http=f)
    assert f.requests == ["current", "stop"]                       # leave = 2
    f.requests.clear()
    pt.apply("library", "tap", now=T0 + timedelta(minutes=3), http=f)
    f.requests.clear()
    assert pt.apply("library", "tap", now=T0 + timedelta(minutes=4), http=f) == "already_tracking"
    assert f.requests == ["current"]


def test_presence_toggl_project_cache_ttl(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    pt.apply("library", "leave", now=T0, http=f)
    f.requests.clear()
    pt.apply("library", "join", now=T0 + timedelta(minutes=30), http=f)
    assert "projects" not in f.requests
    pt.apply("library", "leave", now=T0 + timedelta(minutes=31), http=f)
    f.requests.clear()
    pt.apply("library", "join", now=T0 + timedelta(minutes=61), http=f)
    assert f.requests.count("projects") == 1


def test_presence_toggl_outcome_log_and_files_hold_no_secrets(env):
    f = Fake()
    outs = [pt.apply("library", "join", now=T0, http=f),
            pt.apply("library", "leave", now=T0, http=f)]
    log = [n for n in os.listdir(str(env)) if n.startswith("presence_actions_")]
    assert log == ["presence_actions_2026-11.jsonl"]
    with open(os.path.join(str(env), log[0])) as fh:
        lines = [json.loads(x) for x in fh.read().splitlines()]
    assert len(lines) == 2 and all(set(x) == {"at", "place", "event", "outcome"} for x in lines)
    blob = json.dumps(outs)
    for n in os.listdir(str(env)):
        if n not in (pt.STATE_FILE, pt.CACHE_FILE):
            with open(os.path.join(str(env), n)) as fh:
                blob += fh.read()
    for bad in (TOKEN, "Job search", "4001", "4002", "7001"):
        assert bad not in blob
    with open(os.path.join(str(env), pt.STATE_FILE)) as fh:
        raw = fh.read()
    assert set(json.loads(raw)) == {"entry", "quota"}
    assert TOKEN not in raw and "Job search" not in raw


def test_presence_toggl_dispatch_returns_at_once_and_swallows_errors(env, monkeypatch):
    f = Fake()
    f.gate = threading.Event()
    monkeypatch.setattr(pt.toggl_timer, "_default_http", f)
    t0 = time.time()
    done = pt.dispatch("library", "join")
    assert time.time() - t0 < 1 and not done.is_set()
    f.gate.set()
    assert done.wait(5)
    assert f.running is not None

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(pt, "apply", boom)
    assert pt.dispatch("library", "join").wait(5)


def test_presence_toggl_dispatch_applies_in_arrival_order(env, monkeypatch):
    f = Fake()
    monkeypatch.setattr(pt.toggl_timer, "_default_http", f)
    real = pt.apply
    first = []

    def slow(place, event, **k):
        if not first:
            first.append(1)
            time.sleep(0.3)  # the join is slow; an overtaking leave would see empty state
        return real(place, event, **k)
    monkeypatch.setattr(pt, "apply", slow)
    j = pt.dispatch("library", "join")
    l = pt.dispatch("library", "leave")
    assert j.wait(10) and l.wait(10)
    assert f.running is None and f.requests.count("start") == 1 and f.requests.count("stop") == 1
    assert _state(env)["entry"] is None


def test_presence_toggl_low_quota_never_skips_a_leave(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    f.headers = {"X-Toggl-Quota-Remaining": "1"}
    pt.apply("library", "tap", now=T0 + timedelta(minutes=1), http=f)  # records Remaining 1
    assert pt.apply("home", "join", now=T0 + timedelta(minutes=2), http=f) == "skipped_low_quota"
    assert pt.apply("library", "leave", now=T0 + timedelta(minutes=3), http=f) == "stopped"
    assert f.running is None


def test_presence_toggl_leave_still_respects_a_real_block(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    f.fail["current"] = (402, {"X-Toggl-Quota-Resets-In": "600"})
    pt.apply("library", "leave", now=T0, http=f)
    f.fail.clear()
    f.requests.clear()
    assert pt.apply("library", "leave", now=T0 + timedelta(minutes=1), http=f) == "skipped_over_quota"
    assert f.requests == []


def test_presence_toggl_switch_with_stop_404_still_starts_new_place(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    f.fail["stop"] = (404, {})
    assert pt.apply("home", "join", now=T0 + timedelta(minutes=1), http=f) == "started"
    del f.fail["stop"]
    assert _state(env)["entry"]["place"] == "home"


def test_presence_toggl_failed_call_persists_no_quota_change(env):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    before = _state(env)
    f.fail["current"] = (403, {"X-Toggl-Quota-Remaining": "0"})
    assert pt.apply("library", "leave", now=T0 + timedelta(minutes=1), http=f) == "error:TogglTokenRejected"
    assert _state(env) == before


def test_presence_toggl_cache_label_mismatch_or_removed_place_is_a_miss(env, monkeypatch):
    f = Fake()
    pt.apply("library", "join", now=T0, http=f)
    pt.apply("library", "leave", now=T0, http=f)
    monkeypatch.setenv("PRESENCE_PLACES", json.dumps({"library": "Home life"}))
    f.requests.clear()
    pt.apply("library", "join", now=T0 + timedelta(minutes=1), http=f)
    assert f.requests.count("projects") == 1 and f.running["project_id"] == 502
    pt.apply("library", "leave", now=T0 + timedelta(minutes=2), http=f)
    monkeypatch.setenv("PRESENCE_PLACES", json.dumps({"home": "Home life"}))
    f.requests.clear()
    assert pt.apply("library", "join", now=T0 + timedelta(minutes=3), http=f) == "skipped_no_project"
    assert "start" not in f.requests


def test_presence_toggl_concurrent_joins_make_one_entry(env):
    f = Fake()
    ts = [threading.Thread(target=pt.apply, args=("library", "join"),
                           kwargs={"now": T0, "http": f}) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert f.requests.count("start") == 1


def test_presence_toggl_corrupt_files_read_empty_and_rewrite_atomically(env):
    for n in (pt.STATE_FILE, pt.CACHE_FILE):
        with open(os.path.join(str(env), n), "w") as fh:
            fh.write("{not json")
    f = Fake()
    assert pt.apply("library", "join", now=T0, http=f) == "started"
    assert _state(env)["entry"]["place"] == "library"
    assert not [n for n in os.listdir(str(env)) if n.endswith(".tmp")]


def test_presence_toggl_stop_any_on_tap_stays_false():
    assert pt.STOP_ANY_ON_TAP is False
