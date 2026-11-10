"""Tests for scripts/measure_toggl.py (Toggl Focus API). No network, no real key: HTTP is faked."""
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "scripts" / "measure_toggl.py"
spec = importlib.util.spec_from_file_location("measure_toggl", SCRIPT)
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

TOKEN = "tok-ZZ-4471"
C_DESC, C_PROJ, C_MAIL = "ZZ-CANARY-DESC-4471", "ZZ-CANARY-PROJ-4471", "canary4471@example.com"
ORG, WS = "4001", "4002"
UUID_PID = "9f1c2a7e-uuid-b"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
SETTINGS = {"id": "u-1", "email": C_MAIL, "timezone": "Europe/Paris", "dark": True}
PROJECTS = [{"id": 501, "name": C_PROJ, "color": "#fff"}, {"id": UUID_PID, "name": C_PROJ + "-B"}]


def _e(i, pid, dur, start, stop="x"):
    return {"id": "e-%d" % i, "project_id": pid, "duration": dur, "start": start,
            "stop": stop and start, "description": C_DESC, "tags": [C_MAIL]}


ENTRIES = [
    _e(1, 501, 3600, "2026-11-09T09:00:00Z"),
    _e(2, 501, 1800, "2026-11-09T11:00:00Z"),
    _e(3, UUID_PID, 7260, "2026-11-08T10:00:00Z"),
    _e(4, None, 4, "2026-11-08T13:00:00Z"),
    _e(5, None, 56, "2026-11-08T14:00:00Z"),
    _e(6, 501, -1, "2026-11-10T08:00:00Z", stop=None),
]


class Fake:
    def __init__(self, entries=ENTRIES, default_n=None, projects=PROJECTS, tracking=(204, b""),
                 override=None):
        self.calls = []
        self.entries, self.projects, self.tracking = entries, projects, tracking
        self.default_n = len(entries) if default_n is None else default_n
        self.override = override or (lambda *a: None)

    def __call__(self, method, url, headers=None, json=None, timeout=20):
        self.calls.append((method, url, dict(headers or {})))
        o = self.override(method, url, headers, len(self.calls))
        if o is not None:
            return o
        page = int(re.search(r"[?&]page=(\d+)", url).group(1)) if "page=" in url else 1
        body = lambda x: json_dumps(x)  # noqa: E731
        if url.endswith("/api/users/me/settings"):
            if "0" * 32 in (headers or {}).get("Authorization", ""):
                return 401, {"Content-Type": "application/json"}, b'{"error":"bad key"}'
            return 200, {"Content-Type": "application/json"}, body(SETTINGS)
        if "tracking/current" in url:
            return self.tracking[0], {}, self.tracking[1]
        if "/projects" in url:
            return 200, {}, body(self.projects if page == 1 else [])
        if "time-entries" in url:
            rows = self.entries if "include_taskless=true" in url else self.entries[:self.default_n]
            return 200, {}, body(rows if page == 1 else [])
        return 404, {}, b""


def json_dumps(x):
    return json.dumps(x).encode()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TOGGL_API_TOKEN", TOKEN)
    return tmp_path / "rec"


def run(env_dir, fake=None, extra=(), now=NOW, org=ORG, ws=WS, record=None):
    lines = []
    fake = fake if fake is not None else Fake()
    argv = ["--record", str(record or env_dir), "--org", org, "--workspace", ws] + list(extra)
    rc = mt.main(argv, http=fake, out=lines.append, now=now)
    return rc, "\n".join(lines), fake


def files(d):
    return sorted(Path(d).iterdir()) if Path(d).exists() else []


def scrubbed(d):
    return json.loads(next(p for p in files(d) if p.name.endswith(".scrubbed.json")).read_text())


def test_no_token_exit_2_no_request(monkeypatch, tmp_path):
    monkeypatch.delenv("TOGGL_API_TOKEN", raising=False)

    def boom(*a, **k):
        raise AssertionError("http called")
    lines = []
    rc = mt.main(["--record", str(tmp_path / "r"), "--org", ORG, "--workspace", WS], http=boom,
                 out=lines.append, now=NOW)
    assert rc == 2
    assert "Set it under your user environment variables, then open a new terminal." in "\n".join(lines)


@pytest.mark.parametrize("org,ws", [("", WS), (ORG, ""), ("4001/../x", WS), (ORG, "4002x")])
def test_bad_org_or_workspace_exit_2(env, org, ws):
    rc, out, fake = run(env, org=org, ws=ws)
    assert rc == 2 and fake.calls == []


def test_record_inside_repo_refused(env):
    rc, out, fake = run(env, record=mt.ROOT / "zz_never_created")
    assert rc == 2 and fake.calls == []
    assert not (mt.ROOT / "zz_never_created").exists()


def test_days_clamped(env):
    _, _, f0 = run(env, extra=["--days", "0"])
    assert "date_from=2026-11-09T12:00:00Z" in f0.calls[4][1]
    _, _, f1 = run(env, extra=["--days", "400"])
    assert "date_from=2026-10-10T12:00:00Z" in f1.calls[4][1]
    assert "date_to=2026-11-10T12:00:00Z" in f1.calls[4][1]


def test_request_sequence(env):
    rc, out, fake = run(env)
    assert rc == 0
    urls = [c[1] for c in fake.calls]
    assert [c[0] for c in fake.calls] == ["GET"] * 8
    assert all(u.startswith("https://focus.toggl.com/") for u in urls)
    assert all(c[2]["Authorization"] == "Bearer " + TOKEN for c in fake.calls)
    base = "https://focus.toggl.com/api/organizations/4001/workspaces/4002/"
    assert urls[0] == "https://focus.toggl.com/api/users/me/settings"
    assert urls[1] == base + "tracking/current"
    assert urls[2].startswith(base + "projects?") and urls[3].startswith(base + "projects?")
    # page 1 has rows, page 2 is empty -> 2 requests per paged query
    assert urls[4].startswith(base + "time-entries?") and "include_taskless" not in urls[4]
    assert urls[5].startswith(base + "time-entries?") and "page=2" in urls[5]
    assert "include_taskless=true" in urls[6] and "include_taskless=true" in urls[7]


def test_token_and_ids_never_leak(env, capfd):
    def echo(method, url, headers, n):
        if "tracking/current" in url:
            return 500, {}, ("oops " + headers["Authorization"]).encode()
        return None
    rc, out, fake = run(env, Fake(override=echo))
    cap = capfd.readouterr()
    blob = out + cap.out + cap.err
    for p in files(env):
        blob += p.name + p.read_text()
    assert TOKEN not in blob
    assert ORG not in out + "".join(p.read_text() for p in files(env) if "scrubbed" in p.name)
    assert WS not in out
    assert "<org>" in out and "<ws>" in out


def test_settings_401_stops(env):
    def deny(method, url, headers, n):
        return 401, {"Content-Type": "application/json"}, b'{"message":"nope ' + headers["Authorization"].encode() + b'"}'
    rc, out, fake = run(env, Fake(override=deny))
    assert rc == 1 and len(fake.calls) == 1
    assert "status 401" in out and "application/json" in out and "nope" in out
    assert "The token was refused by the Focus API. See the status above." in out
    assert TOKEN not in out


@pytest.mark.parametrize("code,hdr", [(402, {"X-Toggl-Quota-Remaining": "0", "X-Toggl-Quota-Resets-In": "1234"}),
                                      (429, {"Retry-After": "77"})])
def test_quota_stops(env, code, hdr):
    def limit(method, url, headers, n):
        return (code, hdr, b"") if "time-entries" in url else None
    rc, out, fake = run(env, Fake(override=limit))
    assert rc == 1
    assert "time-entries" in fake.calls[-1][1] and "include_taskless" not in fake.calls[-1][1]
    for k, v in hdr.items():
        assert "%s=%s" % (k, v) in out
    assert "Toggl is limiting this key (HTTP %d); stopped, no further request made." % code in out
    assert len(files(env)) == 2


def test_tracking_idle_and_running(env):
    _, out, _ = run(env)
    assert "idle (204, empty body)" in out
    body = json_dumps({"id": "t1", "description": C_DESC, "started_at": "2026-11-10T08:00:00Z",
                       "project_id": 501})
    _, out2, _ = run(env, Fake(tracking=(200, body)), record=env / "two")
    assert "description (str)" in out2 and "started_at (str)" in out2 and "project_id (int)" in out2
    assert "start field present: yes; project field present: yes" in out2
    assert C_DESC not in out2


def test_paging(env):
    def varied(method, url, headers, n):
        if "/projects" in url or "time-entries" in url:
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            return 200, {}, json_dumps([_e(page * 10, 501, 100 + page, "2026-11-09T0%d:00:00Z" % page)])
        return None
    rc, out, fake = run(env, Fake(override=varied))
    urls = [c[1] for c in fake.calls]
    assert len([u for u in urls if "/projects" in u]) == 3
    assert len([u for u in urls if "time-entries" in u and "taskless" not in u]) == 3
    assert "time entries, default query: 3" in out

    def same(method, url, headers, n):
        if "time-entries" in url:
            return 200, {}, json_dumps(ENTRIES[:2])
        return None
    rc, out, fake = run(env, Fake(override=same), record=env / "b")
    assert "paging parameter appears ignored" in out
    assert "time entries, default query: 2" in out


@pytest.mark.parametrize("flag", [[], ["--check-bad-token"]])
def test_budget_never_exceeded(env, flag):
    def forever(method, url, headers, n):
        if "/projects" in url or "time-entries" in url:
            return 200, {}, json_dumps([{"id": n, "duration": n}])
        return None
    rc, out, fake = run(env, Fake(override=forever), extra=flag)
    assert len(fake.calls) <= (13 if flag else 12)


def test_taskless_difference(env):
    rc, out, fake = run(env, Fake(entries=ENTRIES[:5], default_n=3))
    assert "time entries, default query: 3" in out
    assert "time entries, include_taskless=true: 5" in out
    assert "hidden by the default query: 2" in out


def test_hand_computed_report(env):
    rc, out, fake = run(env, Fake(default_n=4))
    assert rc == 0
    assert "hidden by the default query: 2" in out
    assert "fields used: duration=duration, start=start, end=stop, project=project_id" in out
    assert "unit assumed SECONDS, UNVERIFIED" in out
    assert "total (running entries excluded): 3.53 h (12720 s)" in out
    assert "entries with no project: 2, 0.02 h (60 s)" in out
    assert "entries shorter than 60 s: 2" in out
    assert "running entries: 1" in out
    assert "  Project 1: 1.50 h (5400 s)" in out
    assert "  Project 2: 2.02 h (7260 s)" in out
    assert "  2026-11-09: 2" in out and "  2026-11-08: 3" in out and "  2026-11-10: 1" in out
    assert "distinct projects: 2" in out
    assert "earliest start: 2026-11-08T10:00:00+00:00" in out
    assert out.rstrip().endswith(mt.CLOSING)


def test_unknown_fields_cannot_total(env):
    rows = [{"foo": 1, "bar": "x"}]
    rc, out, fake = run(env, Fake(entries=rows))
    assert rc == 0
    assert "cannot total: no known duration or start/end field; see the key names above" in out
    assert "foo (int)" in out and "bar (str)" in out


def test_scrubbed_file(env):
    rc, out, fake = run(env)
    sc = scrubbed(env)
    text = json.dumps(sc)
    for c in (C_DESC, C_PROJ, C_MAIL, TOKEN, UUID_PID):
        assert c not in text
    projs = sc["projects"]
    assert [p["name"] for p in projs] == ["Project 1", "Project 2"]
    int_alias, str_alias = projs[0]["id"], projs[1]["id"]
    assert isinstance(int_alias, int) and isinstance(str_alias, int) and int_alias != str_alias
    ents = sc["time_entries_taskless"]
    assert ents[0]["project_id"] == int_alias and ents[2]["project_id"] == str_alias
    assert ents[3]["project_id"] is None
    assert ents[0]["duration"] == 3600 and ents[0]["start"] == "2026-11-09T09:00:00Z"
    assert ents[0]["description"] == "<text>"
    raw = next(p for p in files(env) if not p.name.endswith(".scrubbed.json")).read_text()
    assert C_DESC in raw and C_PROJ in raw and C_MAIL in raw


def test_shape_only_stdout_stderr(env, capfd):
    rc, out, fake = run(env)
    cap = capfd.readouterr()
    for c in (C_DESC, C_PROJ, C_MAIL):
        assert c not in out + cap.out + cap.err
    assert "distinct projects:" in out


def test_check_bad_token(env):
    rc, out, fake = run(env, extra=["--check-bad-token"])
    extra = [c for c in fake.calls if c[1].endswith("/api/users/me/settings")]
    assert len(extra) == 2 and TOKEN not in extra[1][2]["Authorization"]
    assert "0" * 32 in extra[1][2]["Authorization"]
    assert "bad token check: status 401" in out
    _, _, plain = run(env, record=env / "p")
    assert len([c for c in plain.calls if c[1].endswith("/api/users/me/settings")]) == 1


def test_redirect_and_transport_failure(env):
    rc, out, fake = run(env, Fake(override=lambda m, u, h, n: (302, {"Location": "https://evil.example/"}, b"")))
    assert rc == 1 and len(fake.calls) == 1
    assert "redirect not followed (status 302)" in out and "refused" not in out

    def fail(method, url, headers, n):
        raise ConnectionError("boom " + headers["Authorization"])
    rc, out, fake = run(env, Fake(override=fail), record=env / "c")
    assert rc == 1 and "request failed" in out and "refused" not in out and TOKEN not in out
    src = SCRIPT.read_text()
    assert "allow_redirects=False" in src.split("def _default_http")[1].split("def _inside")[0]


def test_non_list_200_prints_only_shape(env):
    def weird(method, url, headers, n):
        if "/projects" in url:
            return 200, {}, json_dumps({"data": "x", "note": C_DESC, "n": 1}).replace(b"[", b"[")
        return None
    rc, out, fake = run(env, Fake(override=weird))
    assert C_DESC not in out and "unexpected 200 body; JSON type: dict" in out


def test_files_only_under_dir_and_source_clean(env, tmp_path):
    before = sorted(p.name for p in mt.ROOT.iterdir())
    rc, out, fake = run(env)
    assert sorted(p.name for p in mt.ROOT.iterdir()) == before
    assert len(files(env)) == 2
    assert all(p.name.startswith("toggl_focus_") for p in files(env))
    src = SCRIPT.read_text()
    assert "focus.toggl.com" in src and "track.toggl.com" not in src and "Basic" not in src
    for bad in (".post(", ".put(", ".patch(", ".delete(", '"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
        assert bad not in src


def test_small_ids_do_not_rewrite_fixture_numbers(env):
    rows = [{"id": 1001, "project_id": 7, "duration": 17, "start": "2026-11-09T09:00:00Z",
             "stop": "2026-11-09T09:00:17Z", "count": 1001, "n": 7, "description": C_DESC}]
    projs = [{"id": 7, "name": C_PROJ}]
    rc, out, fake = run(env, Fake(entries=rows, projects=projs), org="7", ws="1001")
    assert rc == 0
    # ids hidden whole-number only, in printed paths
    assert "/organizations/<org>/workspaces/<ws>/" in out
    assert "organizations/7/" not in out and "workspaces/1001/" not in out
    # counts and totals that happen to equal an id are left alone
    assert "projects: 1" in out and "total (running entries excluded): 0.00 h (17 s)" in out
    assert "17 s" in out and "<ws>7" not in out and "1<org>" not in out
    sc = scrubbed(env)
    ent = sc["time_entries_taskless"][0]
    assert ent["duration"] == 17 and ent["count"] == 1001 and ent["n"] == 7
    assert "<org>" not in json.dumps(sc) and "<ws>" not in json.dumps(sc)


def test_page_cap_reported(env):
    def varied(method, url, headers, n):
        if "time-entries" in url and "taskless" not in url:
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            return 200, {}, json_dumps([_e(page, 501, 100 + page, "2026-11-09T0%d:00:00Z" % page)])
        return None
    rc, out, fake = run(env, Fake(override=varied))
    assert "time_entries: page cap reached, results truncated" in out
    assert "undercounted" in out
    # the normal short-page run never says it
    _, out2, _ = run(env, record=env / "n")
    assert "page cap reached" not in out2


def test_partial_paging_keeps_shape_and_scrubs(env):
    def page2_fails(method, url, headers, n):
        if "/projects" in url and "page=1" in url:
            return 200, {}, json_dumps({"items": PROJECTS})
        if "/projects" in url:
            return 500, {}, ("down " + C_MAIL).encode()
        return None
    rc, out, fake = run(env, Fake(override=page2_fails))
    assert "reply is an object holding the list under key 'items'" in out
    sc = scrubbed(env)
    assert isinstance(sc["projects"], dict) and "items" in sc["projects"]
    names = [p["name"] for p in sc["projects"]["items"]]
    assert names == ["Project 1", "Project 2"]
    assert sc["time_entries_taskless"][0]["project_id"] == sc["projects"]["items"][0]["id"]
    assert C_MAIL not in out and C_PROJ not in json.dumps(sc)


@pytest.mark.parametrize("path", ["/projects", "tracking/current", "time-entries"])
def test_non_200_prints_no_body(env, path):
    def err(method, url, headers, n):
        if path in url:
            return 500, {"Content-Type": "text/plain"}, ("fail " + C_MAIL + " Kevin Smith").encode()
        return None
    rc, out, fake = run(env, Fake(override=err))
    assert C_MAIL not in out and "Kevin Smith" not in out and "body starts" not in out.split("\n", 2)[2]
    assert "status 500" in out or "HTTP 500" in out


def test_quoted_key_forms_redacted(monkeypatch, tmp_path):
    key = "ab/cd+ef=gh=="
    monkeypatch.setenv("TOGGL_API_TOKEN", key)
    import base64
    import urllib.parse
    b64 = base64.b64encode(key.encode()).decode()
    forms = [key, urllib.parse.quote(key), urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key),
             b64, urllib.parse.quote(b64, safe=""), urllib.parse.quote_plus(b64)]

    def echo(method, url, headers, n):
        if "tracking/current" in url:
            return 200, {}, json_dumps({"echo": " ".join(forms)})
        if "/projects" in url:
            return 200, {}, json_dumps([{"id": 1, "name": " ".join(forms)}])
        return None
    d = tmp_path / "rec"
    rc, out, fake = run(d, Fake(override=echo))
    blob = out + "".join(p.read_text() for p in files(d))
    for f in forms:
        assert f not in blob
    assert mt._redactor(key)("x " + forms[3] + "; " + forms[2]) == "x ***; ***"
