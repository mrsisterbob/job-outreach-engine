"""Tests for scripts/measure_toggl.py. No network, no real token: the HTTP layer is faked."""
import base64
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("measure_toggl", HERE / "scripts" / "measure_toggl.py")
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

TOKEN = "tok-ZZ-4471"
C_DESC, C_PROJ, C_MAIL = "ZZ-CANARY-DESC-4471", "ZZ-CANARY-PROJ-4471", "canary4471@example.com"
from datetime import datetime, timezone  # noqa: E402
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _e(i, pid, dur, start, stop="x"):
    return {"id": 9000 + i, "project_id": pid, "duration": dur, "start": start,
            "stop": stop and start, "description": C_DESC, "tags": ["t1"], "workspace_id": 77}


ENTRIES = [
    _e(1, 500, 3600, "2026-11-07T10:00:00Z"),
    _e(2, 600, 1800, "2026-11-07T11:00:00Z"),
    _e(3, None, 4, "2026-11-06T10:00:00Z"),
    _e(4, 600, 50, "2026-11-06T11:00:00Z"),
    _e(5, 500, -1762000000, "2026-11-08T09:00:00Z", stop=None),
    _e(6, 999, 600, "2026-11-05T10:00:00Z"),
]
PROJECTS = [{"id": 500, "name": C_PROJ, "workspace_id": 77}, {"id": 600, "name": "Other", "workspace_id": 77}]
ME = {"id": 5, "email": C_MAIL, "default_workspace_id": 77, "timezone": "Europe/Paris"}


class Fake:
    def __init__(self, routes=None, reports_seconds=6054):
        self.calls = []
        self.routes = routes or {}
        self.reports_seconds = reports_seconds

    def __call__(self, method, url, headers=None, json=None, timeout=20):
        self.calls.append((method, url, headers, json))
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        if url.endswith("/api/v9/me"):
            if headers["Authorization"] != "Basic " + base64.b64encode(
                    (TOKEN + ":api_token").encode()).decode():
                return 403, {"Content-Type": "text/plain"}, b"Incorrect username and/or password"
            return 200, {"Content-Type": "application/json"}, _j(ME)
        if "/me/time_entries" in url:
            return 200, {}, _j(ENTRIES)
        if url.endswith("/projects"):
            return 200, {}, _j(PROJECTS)
        if "/summary/time_entries" in url:
            return 200, {}, _j({"groups": [{"id": 1, "sub_groups": [
                {"id": 2, "seconds": self.reports_seconds}]}]})
        return 404, {}, b"nope"


def _j(o):
    return json.dumps(o).encode()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TOGGL_API_TOKEN", TOKEN)


def run(tmp_path, fake, *extra):
    lines = []
    rc = mt.main(["--record", str(tmp_path / "rec")] + list(extra), http=fake,
                 out=lines.append, now=NOW)
    return rc, "\n".join(lines)


def _boom(*a, **k):
    raise AssertionError("http must not be called")


def test_no_token(monkeypatch, tmp_path):
    monkeypatch.delenv("TOGGL_API_TOKEN", raising=False)
    lines = []
    rc = mt.main(["--record", str(tmp_path)], http=_boom, out=lines.append, now=NOW)
    assert rc == 2 and "user environment variables, then open a new terminal." in "\n".join(lines)


def test_record_inside_repo_and_clamp(env, tmp_path):
    fake = Fake()
    rc = mt.main(["--record", str(HERE / "zz_rec")], http=fake, out=lambda s: None, now=NOW)
    assert rc == 2 and fake.calls == [] and not (HERE / "zz_rec").exists()
    f0 = Fake()
    run(tmp_path, f0, "--days", "0")
    assert "start_date=2026-11-07&" in f0.calls[1][1]
    f400 = Fake()
    run(tmp_path, f400, "--days", "400")
    assert "start_date=2026-10-08&" in f400.calls[1][1]
    assert "end_date=2026-11-09" in f400.calls[1][1]


def test_request_sequence_and_auth(env, tmp_path):
    fake = Fake()
    rc, _ = run(tmp_path, fake)
    assert rc == 0
    assert [(m, u.split("?")[0].replace("https://api.track.toggl.com", "")) for m, u, _, _ in fake.calls] == [
        ("GET", "/api/v9/me"), ("GET", "/api/v9/me/time_entries"),
        ("GET", "/api/v9/workspaces/77/projects"),
        ("POST", "/reports/api/v3/workspace/77/summary/time_entries")]
    assert all(u.startswith("https://api.track.toggl.com/") for _, u, _, _ in fake.calls)
    want = "Basic " + base64.b64encode(b"tok-ZZ-4471:api_token").decode()
    assert all(h["Authorization"] == want for _, _, h, _ in fake.calls)
    assert fake.calls[3][3] == {"start_date": "2026-10-25", "end_date": "2026-11-09"}


def test_token_never_leaks(env, tmp_path, capfd):
    b64 = base64.b64encode(b"tok-ZZ-4471:api_token").decode()
    echo = ("echo Authorization: Basic %s token %s" % (b64, TOKEN)).encode()
    fake = Fake({"/api/v9/me/time_entries": (500, {"Content-Type": "text/plain"}, echo)})
    rc, text = run(tmp_path, fake)
    err = capfd.readouterr()
    assert rc == 1
    blob = text + err.out + err.err
    for p in (tmp_path / "rec").iterdir():
        assert TOKEN not in p.name
        blob += p.read_text(encoding="utf-8")
    assert TOKEN not in blob and b64 not in blob


def test_hand_computed_report(env, tmp_path):
    rc, text = run(tmp_path, Fake())
    assert rc == 0
    assert "time entries: 6" in text
    assert "total (running timers excluded): 1.68 h (6054 s)" in text
    assert "entries with no project: 1, 0.00 h (4 s)" in text
    assert "entries shorter than 60 s: 2" in text
    assert "running timers: 1; duration holds: [-1762000000]" in text
    assert "entries with unknown project: 1, 0.17 h (600 s)" in text
    assert "Project 1: 1.00 h (3600 s)" in text
    assert "Project 2: 0.51 h (1850 s)" in text
    for line in ("2026-11-05: 1", "2026-11-06: 2", "2026-11-07: 2", "2026-11-08: 1", "2026-11-04: 0"):
        assert line in text
    assert "duration smallest: -1762000000, largest: 3600" in text
    assert "distinct projects: 2" in text


def test_me_refused(env, tmp_path):
    fake = Fake({"/api/v9/me": (401, {"Content-Type": "text/plain"}, b"Incorrect username and/or password")})
    rc, text = run(tmp_path, fake)
    assert rc == 1 and len(fake.calls) == 1
    assert "status 401" in text and "Incorrect username and/or password" in text
    assert "The token was refused by the Track API v9. See the status above." in text


def test_429_stops(env, tmp_path):
    fake = Fake({"/projects": (429, {"Retry-After": "30", "X-Quota-Remaining": "0"}, b"slow down")})
    rc, text = run(tmp_path, fake)
    assert rc == 1 and len(fake.calls) == 3
    assert "Retry-After=30" in text and "X-Quota-Remaining=0" in text and "429" in text


def test_scrubbed_file(env, tmp_path):
    rc, _ = run(tmp_path, Fake())
    rec = tmp_path / "rec"
    scrub = next(rec.glob("*.scrubbed.json")).read_text(encoding="utf-8")
    raw = next(p for p in rec.glob("*.json") if not p.name.endswith(".scrubbed.json")).read_text("utf-8")
    for c in (C_DESC, C_PROJ, C_MAIL):
        assert c not in scrub
    assert C_DESC in raw and C_PROJ in raw
    d = json.loads(scrub)
    ents, projs = d["time_entries"], d["projects"]
    assert [p["name"] for p in projs] == ["Project 1", "Project 2"]
    assert ents[0]["project_id"] == projs[0]["id"] and ents[1]["project_id"] == projs[1]["id"]
    assert ents[4]["project_id"] == projs[0]["id"] and ents[2]["project_id"] is None
    assert ents[0]["id"] != 9001 and projs[0]["id"] >= 1001
    assert [e["duration"] for e in ents] == [3600, 1800, 4, 50, -1762000000, 600]
    assert ents[0]["start"] == "2026-11-07T10:00:00Z" and ents[4]["stop"] is None
    assert ents[0]["description"] == "<text>"


def test_check_bad_token(env, tmp_path):
    fake = Fake()
    rc, text = run(tmp_path, fake, "--check-bad-token")
    assert rc == 0 and len(fake.calls) == 5
    m, u, h, _ = fake.calls[4]
    assert (m, u.endswith("/api/v9/me")) == ("GET", True)
    assert h["Authorization"] != fake.calls[0][2]["Authorization"]
    assert "bad token check: status 403" in text and "Incorrect username and/or password" in text
    plain = Fake()
    run(tmp_path, plain)
    assert len(plain.calls) == 4


def test_unknown_shape_and_reports_403(env, tmp_path):
    fake = Fake({"/api/v9/me\0": None})
    fake.routes = {}
    ok = Fake()
    orig = ok.__call__

    def odd(method, url, headers=None, json=None, timeout=20):
        if url.endswith("/api/v9/me"):
            ok.calls.append((method, url, headers, json))
            return 200, {}, _j({"id": 1, "fullname": "x"})
        return orig(method, url, headers, json, timeout)
    lines = []
    rc = mt.main(["--record", str(tmp_path / "r1")], http=odd, out=lines.append, now=NOW)
    text = "\n".join(lines)
    assert rc == 0 and "fullname" in text and "unverified" in text
    assert not any("projects" in c[1] or "reports" in c[1] for c in ok.calls)
    f403 = Fake({"/summary/time_entries": (403, {}, b"no")})
    rc, text = run(tmp_path, f403)
    assert rc == 0 and "Reports API not available to this token/plan (HTTP 403)" in text


def test_shape_only_output(env, tmp_path, capfd):
    rc, text = run(tmp_path, Fake())
    err = capfd.readouterr()
    for c in (C_PROJ, C_DESC, C_MAIL):
        assert c not in text + err.out + err.err
    assert "Project 1:" in text and "distinct projects: 2" in text
    assert text.rstrip().endswith(mt.CLOSING)


def test_files_only_under_dir_and_no_writes_in_source(env, tmp_path):
    before = sorted(p.name for p in HERE.iterdir())
    run(tmp_path, Fake())
    assert sorted(p.name for p in HERE.iterdir()) == before
    assert len(list((tmp_path / "rec").iterdir())) == 2
    src = (HERE / "scripts" / "measure_toggl.py").read_text(encoding="utf-8")
    for bad in (".put(", ".patch(", ".delete(", '"PUT"', '"PATCH"', '"DELETE"'):
        assert bad not in src


def test_totals_compare(env, tmp_path):
    _, text = run(tmp_path, Fake(reports_seconds=6054 + 120))
    assert "differ by 2 minutes" in text
    _, text = run(tmp_path, Fake())
    assert "agree to the minute" in text


def _frags(secret, n=6):
    return {secret[i:i + n] for i in range(len(secret) - n + 1)}


def test_token_straddling_the_200_char_cut(env, tmp_path, capfd):
    b64 = base64.b64encode(b"tok-ZZ-4471:api_token").decode()
    body = ("x" * 185 + "Basic " + b64 + " " + TOKEN).encode()
    fake = Fake({"/api/v9/me": (401, {"Content-Type": "text/plain"}, body)})
    rc, text = run(tmp_path, fake)
    err = capfd.readouterr()
    blob = text + err.out + err.err
    assert rc == 1
    for f in _frags(b64) | _frags(TOKEN):
        assert f not in blob


def test_entries_200_non_list_prints_no_body(env, tmp_path):
    fake = Fake({"/me/time_entries": (200, {"Content-Type": "application/json"},
                                      _j({"email": C_MAIL, "detail": C_DESC}))})
    rc, text = run(tmp_path, fake)
    assert rc == 1
    assert C_MAIL not in text and C_DESC not in text
    assert "JSON type: dict" in text and "detail, email" in text


def test_me_transport_failure_and_redirect(env, tmp_path):
    def boom(*a, **k):
        raise OSError("down")
    lines = []
    rc = mt.main(["--record", str(tmp_path / "a")], http=boom, out=lines.append, now=NOW)
    text = "\n".join(lines)
    assert rc == 1 and "request failed (no answer)" in text and "token was refused" not in text
    rc, text = run(tmp_path, Fake({"/api/v9/me": (302, {"Location": "https://evil.example/"}, b"")}))
    assert rc == 1 and "redirect not followed (status 302)" in text and "token was refused" not in text


def test_escapable_token_redacted_in_files(monkeypatch, tmp_path):
    tok = "tok" + chr(92) + "ZZ-é-4471"
    monkeypatch.setenv("TOGGL_API_TOKEN", tok)
    fake = Fake({"/me/time_entries": (500, {}, ("echo " + tok).encode("utf-8"))})
    lines = []
    mt.main(["--record", str(tmp_path / "rec")], http=fake, out=lines.append, now=NOW)
    blob = "\n".join(lines)
    for p in (tmp_path / "rec").iterdir():
        blob += p.read_text(encoding="utf-8")
    for form in (tok, json.dumps(tok)[1:-1], json.dumps(tok, ensure_ascii=False)[1:-1],
                 base64.b64encode((tok + ":api_token").encode()).decode()):
        assert form not in blob


def test_idless_project_alias_matches_scrubbed(env, tmp_path, monkeypatch):
    projs = [{"name": "noid"}] + PROJECTS
    monkeypatch.setitem(globals(), "PROJECTS", projs)
    rc, text = run(tmp_path, Fake())
    assert rc == 0 and "Project 1: 1.00 h (3600 s)" in text
    d = json.loads(next((tmp_path / "rec").glob("*.scrubbed.json")).read_text("utf-8"))
    pr = [p for p in d["projects"] if "id" in p]
    assert pr[0]["name"] == "Project 1" and d["time_entries"][0]["project_id"] == pr[0]["id"]
