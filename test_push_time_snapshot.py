"""Tests for scripts/push_time_snapshot.py. Fake ActivityWatch; no real network call."""
import importlib.util
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

import activity_snapshot
import personal_snapshots

_spec = importlib.util.spec_from_file_location(
    "push_time_snapshot", Path(__file__).parent / "scripts" / "push_time_snapshot.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

TOKEN = "tok-SECRET-7712"
URL = "https://engine.example.com"
CANARY = "ZZ-CANARY-TITLE-4471"
NOW = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)  # 15:00 America/Detroit
APPS = [("code.exe", 18000), ("claude.exe", 1800), ("msedge.exe", 7200),
        ("zzmystery.exe", 3600), ("lockapp.exe", 1800)]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real network call")
    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


class Resp:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code = status
        self._body = body
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


def events(apps=APPS):
    return [[{"timestamp": "2026-11-02T10:00:00Z", "duration": d,
              "data": {"app": a, "title": CANARY}} for a, d in apps]]


class AW:
    def __init__(self, result=None, status=200, bad_json=False, raises=None):
        self.result = events() if result is None else result
        self.status, self.bad, self.raises = status, bad_json, raises
        self.bodies, self.urls = [], []

    def __call__(self, url, json=None, timeout=None):
        self.urls.append(url)
        self.bodies.append(json)
        if self.raises:
            raise self.raises
        return Resp(self.status, self.result, self.bad)


def aw_get_ok(created="2026-10-01T00:00:00+00:00"):
    def get(url, timeout=None):
        return Resp(200, {"aw-watcher-afk_host": {"created": created, "hostname": "H"},
                          "aw-watcher-window_host": {"created": created},
                          "aw-watcher-other": {"created": "2030-01-01T00:00:00+00:00"}})
    return get


def boom_post(*a, **k):
    raise AssertionError("engine post must not be called")


def run(argv, aw=None, aw_get=None, post=boom_post, now=NOW):
    out = []
    rc = P.main(argv, aw_post=aw or AW(), aw_get=aw_get or aw_get_ok(), post=post, now=now,
                out=out.append)
    return rc, "\n".join(out)


def listing(*dirs):
    return [sorted(str(p) for p in Path(d).rglob("*")) for d in dirs]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import main
    d = tmp_path / "snaps"
    d.mkdir()
    monkeypatch.setenv("PERSONAL_SNAPSHOT_DIR", str(d))
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    client = main.app.test_client()

    def post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        assert url == URL + "/snapshots/ingest" and allow_redirects is False
        r = client.post("/snapshots/ingest", json=json, headers=headers)
        return Resp(r.status_code, r.get_json())
    return post, d


def expected_section():
    app_seconds, _ = activity_snapshot.app_seconds_from_query(events())
    return activity_snapshot.build_time_section(app_seconds, NOW)


def test_dry_run_prints_and_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    repo = Path(__file__).parent
    before = listing(tmp_path, repo)
    rc, out = run(["--dry-run", "--url", URL])
    assert rc == 0
    assert "2026-11-02" in out and "2026-11-08" in out
    assert "Building | 5.50" in out and "Browsing | 2.00" in out and "Other | 1.00" in out
    assert out.index("Building | 5.50") < out.index("Browsing | 2.00") < out.index("Other | 1.00")
    assert "Tracked: 8.5 h of 168" in out
    assert "Ignored (lock screen etc.): 0.5 h" in out
    assert "Unmapped apps (shown here only, never sent):" in out and "zzmystery.exe | 1.00" in out
    assert P.CAVEAT in out and out.rstrip().endswith("Nothing was sent.")
    assert CANARY not in out
    assert listing(tmp_path, repo) == before


def test_real_route_end_to_end(engine, monkeypatch):
    post, d = engine
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    rc, out = run(["--url", URL], post=post)
    assert rc == 0 and "HTTP 200" in out and "received_at:" in out and TOKEN not in out
    assert personal_snapshots.load_latest()["time"] == expected_section()
    raw = "".join(p.read_text(encoding="utf-8") for p in d.iterdir())
    for bad in (CANARY, "code.exe", "msedge.exe", "zzmystery.exe"):
        assert bad not in raw
    fm = pytest.importorskip("finance_model")
    view = fm.build_finance_view(personal_snapshots.load_recent(30), NOW)
    cats = view["time"]["editable_categories"]
    assert all(b in cats for b in activity_snapshot.DEFAULT_BUCKETS) and len(cats) >= 9


def test_money_snapshot_survives_35_time_pushes(engine):
    post, _ = engine
    personal_snapshots.save_snapshot({"money": {
        "period": "2026-10", "as_of": "2026-10-31", "flow": [],
        "off_budget": [{"name": "Cash", "balance": 5.0}]}})
    for _ in range(35):
        assert run(["--url", URL], post=post)[0] == 0
    assert any("money" in s for s in personal_snapshots.load_recent(30))


def test_activitywatch_failures():
    rc, out = run(["--url", URL], aw=AW(raises=requests.exceptions.ConnectionError("x")))
    assert rc == 2 and "ActivityWatch is not answering" in out
    rc, out = run(["--url", URL], aw=AW(raises=ConnectionError("x")))
    assert rc == 2
    rc, out = run(["--url", URL], aw=AW(status=500))
    assert rc == 1 and "500" in out
    rc, out = run(["--url", URL], aw=AW(bad_json=True))
    assert rc == 1 and "not with JSON" in out
    rc, out = run(["--url", URL], aw=AW(raises=KeyError("secret-detail")))
    assert rc == 1 and "KeyError" in out and "secret-detail" not in out


@pytest.mark.parametrize("bad", ["http://192.168.1.5:5600", "https://example.com",
                                 "http://localhost.evil.com:5600", "http://u@localhost:5600"])
def test_bad_aw_url_makes_no_request(bad):
    aw = AW()
    rc, out = run(["--dry-run", "--aw-url", bad], aw=aw)
    assert rc == 2 and aw.urls == []


@pytest.mark.parametrize("good", ["http://localhost:5600", "http://127.0.0.1:5600",
                                  "http://[::1]:5600"])
def test_good_aw_url(good):
    aw = AW()
    rc, _ = run(["--dry-run", "--aw-url", good], aw=aw)
    assert rc == 0 and aw.urls[0] == good + "/api/0/query/"


def test_week_last(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    aw = AW()
    sent = {}

    def post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        sent.update(json)
        return Resp(200, {"received_at": "t"})
    rc, out = run(["--week", "last", "--url", URL], aw=aw, post=post)
    assert rc == 0
    assert aw.bodies[0]["timeperiods"] == ["2026-10-26T04:00:00Z/2026-11-02T05:00:00Z"]
    assert sent["time"]["week_of"] == "2026-10-26" and sent["time"]["as_of"] == "2026-11-01"
    assert "Push the current week again afterwards" in out
    rc, out = run(["--dry-run"])
    assert "Push the current week again" not in out


def test_engine_url_and_token_checks(monkeypatch):
    monkeypatch.delenv("ENGINE_URL", raising=False)
    monkeypatch.delenv("SNAPSHOT_INGEST_TOKEN", raising=False)
    rc, out = run([])
    assert rc == 2 and "No engine URL" in out
    rc, out = run(["--url", URL])
    assert rc == 2 and "No token" in out
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    rc, out = run(["--url", "http://example.com"])
    assert rc == 2 and "Refusing the URL" in out and TOKEN not in out
    calls = []

    def post(url, **kw):
        calls.append(url)
        return Resp(200, {"received_at": "t"})
    for ok in ("https://example.com", "http://localhost:5000"):
        assert run(["--url", ok], post=post)[0] == 0
    assert len(calls) == 2


def test_nothing_tracked(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    rc, out = run(["--url", "http://localhost:5000"], aw=AW(result=[[]]))
    assert rc == 1 and "Nothing tracked for the week of 2026-11-02" in out


def test_map_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    rc, out = run(["--dry-run"])
    assert rc == 0 and "categories from defaults" in out and "Building | 5.50" in out
    good = tmp_path / "mymap.json"
    good.write_text(json.dumps({"buckets": ["Job search", "Building", "Browsing", "Other"],
                                "apps": {"code.exe": "Job search"}}), encoding="utf-8")
    rc, out = run(["--dry-run", "--map", str(good)])
    assert rc == 0 and "Job search | 5.00" in out and "mymap.json" in out
    assert str(tmp_path) not in out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"buckets": ["Building"], "apps": {"code.exe": "Nope"}}),
                   encoding="utf-8")
    aw = AW()
    rc, out = run(["--dry-run", "--map", str(bad)], aw=aw)
    assert rc == 2 and "not in buckets" in out and aw.urls == []
    notjson = tmp_path / "x.json"
    notjson.write_text("{", encoding="utf-8")
    aw = AW()
    assert run(["--dry-run", "--map", str(notjson)], aw=aw)[0] == 2 and aw.urls == []
    (tmp_path / "time_categories.json").write_text(json.dumps(
        {"buckets": ["Gaming", "Building", "Other"], "apps": {"code.exe": "Gaming"}}),
        encoding="utf-8")
    rc, out = run(["--dry-run"])
    assert rc == 0 and "time_categories.json" in out and "Gaming | 5.00" in out
    assert str(tmp_path) not in out


def test_partial_week_line():
    rc, out = run(["--dry-run"], aw_get=aw_get_ok("2026-11-03T12:00:00+00:00"))
    assert "ActivityWatch only started on 2026-11-03, so this week is partial." in out
    rc, out = run(["--dry-run"], aw_get=aw_get_ok("2026-10-01T00:00:00+00:00"))
    assert "only started" not in out

    def failing(url, timeout=None):
        raise RuntimeError("down")
    rc, out = run(["--dry-run"], aw_get=failing)
    assert rc == 0 and "Nothing was sent." in out


def test_wrong_token_and_exception_never_leak_token(engine, capsys, monkeypatch):
    post, d = engine
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", "server-real")
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", "server-real")

    def wrong(url, json=None, headers=None, timeout=None, allow_redirects=True):
        import main
        r = main.app.test_client().post("/snapshots/ingest", json=json, headers=headers)
        return Resp(r.status_code, r.get_json())
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", "server-real")
    out = []
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", "server-real")
    # the script sends the env token; make the server expect a different one
    def post_other(url, json=None, headers=None, timeout=None, allow_redirects=True):
        monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", "server-other")
        try:
            return wrong(url, json, headers, timeout)
        finally:
            monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", "server-real")
    rc = P.main(["--url", URL], aw_post=AW(), aw_get=aw_get_ok(), post=post_other, now=NOW,
                out=out.append)
    text = "\n".join(out)
    assert rc == 1 and "403" in text and "server-real" not in text
    assert personal_snapshots.list_snapshots() == []

    def raiser(*a, **k):
        raise RuntimeError("server-real")
    rc = P.main(["--url", URL], aw_post=AW(), aw_get=aw_get_ok(), post=raiser, now=NOW,
                out=out.append)
    text = "\n".join(out)
    assert rc == 1 and "RuntimeError" in text and "server-real" not in text
    cap = capsys.readouterr()
    assert "server-real" not in cap.out + cap.err


def test_map_file_with_bom(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    f = tmp_path / "bom.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"buckets": ["Job search", "Other"], "apps": {"code.exe": "Job search"}}).encode())
    rc, out = run(["--dry-run", "--map", str(f)])
    assert rc == 0 and "Job search | 5.00" in out


@pytest.mark.parametrize("frac", ["", ".5", ".123", ".123456789"])
@pytest.mark.parametrize("tail", ["Z", "+00:00"])
def test_created_parses_any_fraction(frac, tail):
    dt = P._parse_created(f"2026-11-03T12:00:00{frac}{tail}")
    assert dt.utcoffset().total_seconds() == 0 and dt.day == 3
    rc, out = run(["--dry-run"], aw_get=aw_get_ok(f"2026-11-03T12:00:00{frac}{tail}"))
    assert "only started on 2026-11-03" in out


def test_unreadable_bucket_dates_warn():
    rc, out = run(["--dry-run"], aw_get=aw_get_ok("not-a-date"))
    assert rc == 0 and "cannot tell if the week is partial" in out


def test_unmapped_app_names_are_sanitised():
    evs = events([("code.exe", 3600), ("bad\x1b[31mname\n" + "x" * 300 + ".exe", 3600)])
    rc, out = run(["--dry-run"], aw=AW(result=evs))
    assert rc == 0 and "\x1b" not in out and "x" * 100 not in out and "badname" not in out.replace(
        "bad [31mname", "")


def test_url_problem_beats_empty_week_on_real_send(monkeypatch):
    monkeypatch.delenv("ENGINE_URL", raising=False)
    monkeypatch.delenv("SNAPSHOT_INGEST_TOKEN", raising=False)
    rc, out = run([], aw=AW(result=[[]]))
    assert rc == 2 and "No engine URL" in out
    rc, out = run(["--url", URL], aw=AW(result=[[]]))
    assert rc == 2 and "No token" in out
    rc, out = run(["--dry-run"], aw=AW(result=[[]]))
    assert rc == 0


def test_read_timeout_is_friendly():
    rc, out = run(["--url", URL], aw=AW(raises=requests.exceptions.ReadTimeout("slow")))
    assert rc == 1 and "ActivityWatch took too long" in out


def test_missing_timezone_data_is_announced(monkeypatch):
    monkeypatch.setattr(P, "_tz_available", lambda: False)
    rc, out = run(["--dry-run"])
    assert "dates are shown in UTC" in out


# ---- proxy bypass: ActivityWatch calls never reach a proxy; the engine post still may ----
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_REAL_SOCKET = socket.socket
_REAL_CREATE = socket.create_connection
_REAL_POST = requests.post


class _FakeServer:
    def __init__(self, reply):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                outer.requests.append((self.command, self.path))
                out = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = _do

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def proxy_servers(monkeypatch):
    # the autouse no_network fixture blocks sockets; these tests talk to loopback fakes only
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE)
    monkeypatch.setattr(requests, "post", _REAL_POST)
    made = []

    def make(reply):
        s = _FakeServer(reply)
        made.append(s)
        return s

    proxy = make({"ok": True})
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, f"http://127.0.0.1:{proxy.port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    yield make, proxy
    for s in made:
        s.close()


def test_proxy_default_aw_calls_bypass_proxy(proxy_servers, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    make, proxy = proxy_servers


    aw = make(events())
    out = []
    rc = P.main(["--dry-run", "--aw-url", f"http://127.0.0.1:{aw.port}"], now=NOW,
                post=boom_post, out=out.append)
    assert rc == 0, out
    assert ("POST", "/api/0/query/") in aw.requests
    assert ("GET", "/api/0/buckets/") in aw.requests
    assert proxy.requests == []


def test_proxy_control_requests_post_leaks_to_proxy(proxy_servers):
    make, proxy = proxy_servers
    aw = make([])
    requests.post(f"http://localhost:{aw.port}/api/0/query/", json={}, timeout=5)
    assert len(proxy.requests) == 1


def test_proxy_engine_post_still_follows_proxy(proxy_servers):
    make, proxy = proxy_servers
    import push_actual_snapshot as pas
    pas.default_post("http://engine.invalid/snapshots/ingest", json={}, timeout=5,
                     allow_redirects=False)
    assert [r[1] for r in proxy.requests] == ["http://engine.invalid/snapshots/ingest"]


def test_proxy_default_function_source_has_no_requests_call():
    src = (Path(__file__).parent / "scripts" / "push_time_snapshot.py").read_text(encoding="utf-8")
    body = src.split("def _aw_post_default", 1)[1].split("def aw_url_ok", 1)[0]
    assert "requests.post(" not in body and "requests.get(" not in body
    assert body.count("local_http.") == 2
