import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

import local_http
from local_http import local_delete, local_get, local_post


class _Fake:
    def __init__(self, handler_fn):
        self.requests = []
        self.connections = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def setup(self):
                outer.connections += 1
                super().setup()

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append((self.command, self.path, body))
                handler_fn(self, body)

            do_GET = do_POST = do_DELETE = _do

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _ok(h, body):
    out = json.dumps({"ok": True}).encode()
    h.send_response(200)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(out)))
    h.end_headers()
    h.wfile.write(out)


@pytest.fixture
def servers():
    made = []

    def make(fn=_ok):
        s = _Fake(fn)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


@pytest.fixture
def proxy_env(servers, monkeypatch):
    proxy = servers()
    url = f"http://127.0.0.1:{proxy.port}"
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    return proxy


def test_post_bypasses_proxy_with_control(servers, proxy_env):
    aw = servers()
    body = {"query": ["x"], "timeperiods": ["a/b"]}
    r = local_post(f"http://localhost:{aw.port}/api/0/query/", json=body, timeout=5)
    assert r.status_code == 200
    assert json.loads(aw.requests[0][2]) == body
    assert proxy_env.requests == [] and proxy_env.connections == 0
    # control: plain requests.post leaks to the proxy, so the fixture can fail
    requests.post(f"http://localhost:{aw.port}/api/0/query/", json=body, timeout=5)
    assert len(proxy_env.requests) == 1


def test_get_bypasses_proxy_with_control(servers, proxy_env):
    aw = servers()
    r = local_get(f"http://127.0.0.1:{aw.port}/api/0/info", timeout=5)
    assert r.json() == {"ok": True}
    assert aw.requests[0][:2] == ("GET", "/api/0/info")
    assert proxy_env.requests == []
    requests.get(f"http://localhost:{aw.port}/api/0/info", timeout=5)
    assert len(proxy_env.requests) == 1


REFUSED = [
    "http://localhost.evil.com/",
    "http://127.0.0.1.evil.com/",
    "http://localhost@evil.com/",
    "http://evil.com#@localhost/",
    "https://localhost:5600/",
    "ftp://localhost/",
    "http://localhost:5600/\n",
    "http://localhost:5600\n/x",
    "http://user:pw@localhost:5600/",
    "http://localhost:abc/",
    "http://localhost:99999/",
    "http://localhost:5600 /",
    "http://localhost\\@evil.com/",
    "",
    None,
    123,
    b"http://localhost/",
]


@pytest.mark.parametrize("url", REFUSED)
def test_refused_urls(servers, proxy_env, url):
    aw = servers()
    for fn in (local_post, local_get, local_delete):
        with pytest.raises(ValueError) as e:
            fn(url)
        assert str(e.value) == "local_http only talks to this machine"
        if isinstance(url, str) and len(url) > 3:
            assert url not in str(e.value)
    assert proxy_env.connections == 0 and aw.connections == 0


def test_ipv6_loopback_passes_gate():
    with pytest.raises(requests.exceptions.ConnectionError):
        local_get("http://[::1]:1/", timeout=2)


def test_redirect_not_followed(servers, proxy_env):
    other = servers()

    def redir(h, body):
        h.send_response(302)
        h.send_header("Location", f"http://127.0.0.1:{other.port}/stolen")
        h.send_header("Content-Length", "0")
        h.end_headers()

    aw = servers(redir)
    r = local_post(f"http://localhost:{aw.port}/x", json={"a": 1}, timeout=5)
    assert r.status_code == 302
    assert other.requests == [] and other.connections == 0


def test_redirects_and_proxies_rejected(servers, proxy_env):
    aw = servers()
    u = f"http://localhost:{aw.port}/"
    for kw in ({"allow_redirects": True}, {"allow_redirects": False},
               {"proxies": {"http": "http://127.0.0.1:1"}}):
        for fn in (local_post, local_get, local_delete):
            with pytest.raises(TypeError):
                fn(u, **kw)
    assert aw.connections == 0 and proxy_env.connections == 0


def test_timeout_and_closed_port(servers):
    def hang(h, body):
        time.sleep(1.5)

    slow = servers(hang)
    with pytest.raises(requests.exceptions.Timeout):
        local_get(f"http://127.0.0.1:{slow.port}/", timeout=0.2)
    dead = _Fake(_ok)
    port = dead.port
    dead.close()
    with pytest.raises(requests.exceptions.ConnectionError):
        local_get(f"http://127.0.0.1:{port}/", timeout=2)


def test_environment_untouched(servers, proxy_env):
    aw = servers()
    before = dict(os.environ)
    local_post(f"http://localhost:{aw.port}/", json={}, timeout=5)
    local_get(f"http://localhost:{aw.port}/", timeout=5)
    assert dict(os.environ) == before
    assert os.environ["HTTP_PROXY"].startswith("http://127.0.0.1:")


def test_response_and_source(servers, proxy_env):
    aw = servers()
    r = local_get(f"http://localhost:{aw.port}/", timeout=5)
    assert isinstance(r, requests.Response)
    assert r.status_code == 200 and r.json() == {"ok": True} and '"ok"' in r.text
    src = Path(local_http.__file__).read_text(encoding="utf-8")
    assert "trust_env = False" in src
    assert not re.search(r"requests\.(get|post)\(", src)
    imports = re.findall(r"^\s*(?:import|from)\s+(\w+)", src, re.M)
    assert set(imports) <= {"urllib", "requests"}


def test_delete_bypasses_proxy_with_control(servers, proxy_env):
    aw = servers()
    r = local_delete(f"http://localhost:{aw.port}/api/0/buckets/b/events/7", timeout=5)
    assert r.status_code == 200
    assert aw.requests[0][:2] == ("DELETE", "/api/0/buckets/b/events/7")
    assert proxy_env.requests == [] and proxy_env.connections == 0
    requests.delete(f"http://localhost:{aw.port}/x", timeout=5)
    assert len(proxy_env.requests) == 1


def test_delete_refuses_non_loopback_before_socket(servers, proxy_env):
    aw = servers()
    for url in ("http://example.com:5600/x", "https://localhost:5600/x"):
        with pytest.raises(ValueError):
            local_delete(url)
    for kw in ({"allow_redirects": True}, {"proxies": {}}):
        with pytest.raises(TypeError):
            local_delete(f"http://localhost:{aw.port}/", **kw)
    assert aw.connections == 0 and proxy_env.connections == 0
