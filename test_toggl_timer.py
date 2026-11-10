"""Tests for toggl_timer.py. A fake http is always injected; no network, no real token."""
import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import toggl_timer as tt

HERE = Path(__file__).parent
TOKEN = "tok-ZZ-4471"
ORG, WS = 4001, 4002
BASE = "https://focus.toggl.com/api/organizations/4001/workspaces/4002"
NOW = datetime(2026, 11, 10, 19, 36, 56, tzinfo=timezone.utc)

TIMER = json.loads((HERE / "fixtures" / "toggl_focus_timer.json").read_text(encoding="utf-8"))
FOCUS = json.loads((HERE / "fixtures" / "toggl_focus.json").read_text(encoding="utf-8"))


def step(name, nth=0):
    return [s for s in TIMER if s["name"] == name][nth]


def body_of(obj):
    return b"" if obj is None else json.dumps(obj).encode()


class Fake:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers=None, json=None, timeout=20):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        status, hdrs, body = self.replies.pop(0)
        return status, hdrs, body


def one(status, obj=None, headers=None, raw=None):
    return Fake([(status, headers or {}, raw if raw is not None else body_of(obj))])


def fixture_reply(name, nth=0):
    s = step(name, nth)
    return (s["status"], s["headers"], body_of(s["reply"]))


class Stateful:
    """Fake that follows the measured behaviour: start replaces, stop stops whatever runs."""

    def __init__(self):
        self.running = None
        self.next_id = 7000
        self.calls = []

    def __call__(self, method, url, headers=None, json=None, timeout=20):
        self.calls.append((method, url))
        if url.endswith("/tracking/current"):
            if self.running is None:
                return 204, {}, b""
            return 200, {}, body_of(self.running)
        if url.endswith("/tracking/start"):
            self.next_id += 1
            self.running = {"id": self.next_id, "project_id": json.get("project_id"),
                            "start": json["start"], "type": json["type"]}
            return 200, {}, body_of(self.running)
        if url.endswith("/tracking/stop"):
            if self.running is None:
                return 404, {}, b"{}"
            done = dict(self.running, duration=60)
            self.running = None
            return 200, {}, body_of(done)
        return 500, {}, b""


def call_current(http, **kw):
    return tt.current(TOKEN, ORG, WS, http, **kw)


def test_current_idle_and_running_from_fixture():
    assert call_current(Fake([fixture_reply("current", 0)])) is None
    got = call_current(Fake([fixture_reply("current", 1)]))
    assert got == {"id": 1001, "project_id": None, "start": "2026-11-10T19:36:56Z"}


@pytest.mark.parametrize("body", [
    b'{"error": "nope"}', b'[1, 2]', b'"text"', b'{"id": "x"}', b'not json at all',
    b'{"id": 5}',
])
def test_current_odd_200_is_shape_error_never_none(body):
    with pytest.raises(tt.TogglShapeError):
        call_current(one(200, raw=body))


@pytest.mark.parametrize("status", [401, 403])
def test_token_rejected_message_clean(status):
    echoed = ("Bearer " + TOKEN + " " + "x" * 300).encode()
    with pytest.raises(tt.TogglTokenRejected) as ei:
        call_current(one(status, raw=echoed))
    msg = str(ei.value)
    assert "TOGGL_API_TOKEN" in msg and TOKEN not in msg and "xxxx" not in msg


def test_quota_and_server_errors():
    hdrs = {"X-Toggl-Quota-Resets-In": "1234", "X-Toggl-Quota-Remaining": "0"}
    for status in (402, 429):
        with pytest.raises(tt.TogglQuotaExceeded) as ei:
            call_current(one(status, headers=hdrs, raw=TOKEN.encode()))
        assert ei.value.resets_in == "1234" and not isinstance(ei.value, tt.TogglTokenRejected)
        assert TOKEN not in str(ei.value)
        with pytest.raises(tt.TogglQuotaExceeded) as ei:
            call_current(one(status))
        assert ei.value.resets_in is None
    with pytest.raises(tt.TogglError) as ei:
        call_current(one(500, raw=TOKEN.encode()))
    assert not isinstance(ei.value, (tt.TogglTokenRejected, tt.TogglQuotaExceeded))
    assert TOKEN not in str(ei.value)


def test_start_request_shape_and_required_type():
    http = Fake([fixture_reply("start A")])
    got = tt.start(TOKEN, ORG, WS, 4101, NOW, http, description="work")
    assert len(http.calls) == 1
    c = http.calls[0]
    assert c["method"] == "POST" and c["url"] == BASE + "/tracking/start"
    assert c["headers"]["Authorization"] == "Bearer " + TOKEN
    assert c["json"] == {"type": "activity", "project_id": 4101,
                         "start": "2026-11-10T19:36:56Z", "description": "work"}
    assert got["id"] == 1001

    def strict(method, url, headers=None, json=None, timeout=20):
        if "type" not in json:
            err = {"error_description": "Key: 'StartTrackingPayload.Type' failed on the 'required' tag" + "y" * 200}
            return 400, {}, body_of(err)
        return 200, {}, body_of(step("start A")["reply"])

    assert tt.start(TOKEN, ORG, WS, 4101, NOW, strict)["id"] == 1001
    with pytest.raises(tt.TogglError) as ei:
        tt.start(TOKEN, ORG, WS, 4101, NOW, lambda *a, **k: strict(a[0], a[1], json={}))
    assert "StartTrackingPayload.Type" in str(ei.value) and len(str(ei.value)) < 200


def test_start_without_project_omits_key():
    http = Fake([fixture_reply("start A")])
    tt.start(TOKEN, ORG, WS, None, NOW, http)
    assert "project_id" not in http.calls[0]["json"]


def test_start_bad_replies():
    with pytest.raises(tt.TogglShapeError):
        tt.start(TOKEN, ORG, WS, 4101, NOW, one(200, {"id": "x", "start": "s"}))
    with pytest.raises(tt.TogglShapeError):
        tt.start(TOKEN, ORG, WS, 4101, NOW, one(200, {"ok": True}))
    with pytest.raises(tt.TogglTokenRejected):
        tt.start(TOKEN, ORG, WS, 4101, NOW, one(401))
    with pytest.raises(tt.TogglQuotaExceeded):
        tt.start(TOKEN, ORG, WS, 4101, NOW, one(402))
    with pytest.raises(tt.TogglError):
        tt.start(TOKEN, ORG, WS, 4101, NOW, one(500))


def test_stop_request_and_result_and_404():
    http = Fake([fixture_reply("stop")])
    got = tt.stop(TOKEN, ORG, WS, NOW, http)
    c = http.calls[0]
    assert c["method"] == "POST" and c["url"] == BASE + "/tracking/stop"
    assert c["json"] == {"end": "2026-11-10T19:36:56Z"}
    assert got == {"id": 1004, "duration_seconds": 0}
    with pytest.raises(tt.TogglEntryGone):
        tt.stop(TOKEN, ORG, WS, NOW, one(404))


def test_stop_computes_duration_when_reply_has_none():
    reply = {"id": 9, "start": "2026-11-10T19:30:00Z"}
    assert tt.stop(TOKEN, ORG, WS, NOW, one(200, reply)) == {"id": 9, "duration_seconds": 416}


def projects_reply(data):
    return one(200, {"data": data})


def test_project_ids_matching():
    data = [
        {"id": 1, "name": "Job  Search", "archived_at": None, "active": False},
        {"id": 2, "name": "Old", "archived_at": "2026-01-01T00:00:00Z", "active": False},
        {"id": 3, "name": "Job Search Extra", "archived_at": None},
    ]
    http = projects_reply(data)
    got = tt.project_ids(TOKEN, ORG, WS, ["job search", " JOB   SEARCH ", "Old", "Nope", "Job"], http)
    assert len(http.calls) == 1
    assert http.calls[0]["url"] == BASE + "/projects?per_page=100"
    assert got == {"job search": 1, " JOB   SEARCH ": 1, "Old": None, "Nope": None, "Job": None}


def test_project_ids_bad_shape():
    for raw in (b'[]', b'{"items": []}', b'{"data": "x"}', b'junk'):
        with pytest.raises(tt.TogglShapeError):
            tt.project_ids(TOKEN, ORG, WS, ["a"], one(200, raw=raw))


def test_project_ids_fixture_active_false_matched():
    projects = FOCUS["projects"]["data"]
    p = next(p for p in projects if p.get("archived_at") is None and p.get("active") is False)
    got = tt.project_ids(TOKEN, ORG, WS, [p["name"]], projects_reply(projects))
    assert got[p["name"]] == p["id"]


def test_project_ids_pages_only_when_full():
    full = [{"id": i, "name": "p%d" % i, "archived_at": None} for i in range(100)]
    http = Fake([(200, {}, body_of({"data": full})),
                 (200, {}, body_of({"data": [{"id": 500, "name": "late", "archived_at": None}]}))])
    got = tt.project_ids(TOKEN, ORG, WS, ["late"], http)
    assert got["late"] == 500 and len(http.calls) == 2
    assert http.calls[1]["url"].endswith("&page=2")


def test_ids_must_be_ints_and_urls_are_focus():
    http = Fake([])
    for bad in ("4001", True, 4001.0, None):
        with pytest.raises(tt.TogglError):
            tt.current(TOKEN, bad, WS, http)
        with pytest.raises(tt.TogglError):
            tt.stop(TOKEN, ORG, bad, NOW, http)
    assert http.calls == []
    st = Stateful()
    call_current(st)
    tt.start(TOKEN, ORG, WS, 4101, NOW, st)
    tt.stop(TOKEN, ORG, WS, NOW, st)
    tt.project_ids(TOKEN, ORG, WS, ["a"], lambda *a, **k: (200, {}, b'{"data": []}'))
    assert all(u.startswith("https://focus.toggl.com/") for _, u in st.calls)


def test_write_path_stateful():
    st = Stateful()
    started = tt.start(TOKEN, ORG, WS, 4101, NOW, st)
    assert call_current(st) == {"id": started["id"], "project_id": 4101, "start": started["start"]}
    stopped = tt.stop(TOKEN, ORG, WS, NOW, st)
    assert stopped["id"] == started["id"]
    assert call_current(st) is None
    first = tt.start(TOKEN, ORG, WS, 4101, NOW, st)
    second = tt.start(TOKEN, ORG, WS, 4102, NOW, st)
    assert call_current(st)["id"] == second["id"] != first["id"]


def test_on_reply_called_per_request_and_errors_ignored():
    seen = []
    tt.start(TOKEN, ORG, WS, 4101, NOW, Fake([fixture_reply("start A")]),
             on_reply=lambda s, h: seen.append((s, h)))
    assert seen == [(200, {})]
    seen.clear()
    with pytest.raises(tt.TogglQuotaExceeded):
        call_current(one(402, headers={"X-Toggl-Quota-Remaining": "0"}),
                     on_reply=lambda s, h: seen.append((s, h)))
    assert seen == [(402, {"X-Toggl-Quota-Remaining": "0"})]

    def boom(s, h):
        raise RuntimeError("x")

    assert call_current(Fake([fixture_reply("current", 0)]), on_reply=boom) is None


def test_source_hygiene():
    src = (HERE / "toggl_timer.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {"os", "main", "flask", "sqlite3", "socket"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not banned & {a.name.split(".")[0] for a in node.names}
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert "requests" not in [getattr(a, "name", "") for a in node.names]
    wrapper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_default_http")
    outside = src.replace(ast.get_source_segment(src, wrapper), "")
    assert "requests" not in outside
    for word in ("DELETE", "track.toggl.com", "Basic"):
        assert word not in src


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket
    import requests

    def refuse(*a, **k):
        raise AssertionError("network reached from a test")

    monkeypatch.setattr(requests.Session, "request", refuse)
    monkeypatch.setattr(requests, "request", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_400_body_text_is_never_echoed():
    canary = "Authorization: Bearer leak CANARY words with spaces"
    with pytest.raises(tt.TogglError) as ei:
        tt.start(TOKEN, ORG, WS, 4101, NOW, one(400, {"error_description": canary}))
    assert "CANARY" not in str(ei.value) and "Bearer" not in str(ei.value)
    assert "HTTP 400" in str(ei.value)
    with pytest.raises(tt.TogglError) as ei:
        tt.start(TOKEN, ORG, WS, 4101, NOW, one(400, {"error_description": "Key: 'Bad Field CANARY' x"}))
    assert "CANARY" not in str(ei.value)


@pytest.mark.parametrize("bad", ["tok bad", "tok\nbad", "", "tok\x00", None])
def test_bad_token_rejected_before_request(bad):
    http = Fake([])
    with pytest.raises(tt.TogglTokenRejected) as ei:
        tt.current(bad, ORG, WS, http)
    assert http.calls == [] and "tok" not in str(ei.value).replace("TOGGL_API_TOKEN", "")


def test_http_exception_is_wrapped_without_its_text():
    def boom(*a, **k):
        raise ConnectionError("failed with Bearer " + TOKEN)

    for fn in (lambda: call_current(boom),
               lambda: tt.start(TOKEN, ORG, WS, 1, NOW, boom),
               lambda: tt.stop(TOKEN, ORG, WS, NOW, boom),
               lambda: tt.project_ids(TOKEN, ORG, WS, ["a"], boom)):
        with pytest.raises(tt.TogglError) as ei:
            fn()
        assert TOKEN not in str(ei.value) and ei.value.__cause__ is None


def test_project_list_cap_raises_only_when_last_page_full():
    full = body_of({"data": [{"id": i, "name": "p%d" % i, "archived_at": None} for i in range(100)]})
    with pytest.raises(tt.TogglError, match="too long to read completely"):
        tt.project_ids(TOKEN, ORG, WS, ["x"], Fake([(200, {}, full)] * 3))
    short = body_of({"data": [{"id": 1, "name": "x", "archived_at": None}]})
    got = tt.project_ids(TOKEN, ORG, WS, ["x"], Fake([(200, {}, full)] * 2 + [(200, {}, short)]))
    assert got == {"x": 1}


def test_stop_clamps_negative_and_rejects_naive_now():
    future = {"id": 9, "start": "2026-11-10T19:50:00Z"}
    assert tt.stop(TOKEN, ORG, WS, NOW, one(200, future)) == {"id": 9, "duration_seconds": 0}
    naive = datetime(2026, 11, 10, 19, 36, 56)
    http = Fake([])
    with pytest.raises(tt.TogglError):
        tt.stop(TOKEN, ORG, WS, naive, http)
    with pytest.raises(tt.TogglError):
        tt.start(TOKEN, ORG, WS, 1, naive, http)
    assert http.calls == []
