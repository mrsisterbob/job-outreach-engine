import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import toggl_aw_sync as sync
from scripts import toggl_to_activitywatch as ta

HERE = Path(__file__).parent
FIX = json.loads((HERE / "fixtures" / "toggl_focus.json").read_text("utf-8"))
TOKEN = "tok-ZZ-4471"
NOW = datetime(2026, 11, 10, 18, 0, 0, tzinfo=timezone.utc)
BUCKET = "aw-stopwatch"
ARGS = ["--org", "4001", "--workspace", "4002"]
CANARY = "ZZ-CANARY-DESC-4471"


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class FakeToggl:
    def __init__(self):
        self.entries = copy.deepcopy(FIX["time_entries_taskless"]["data"])
        self.projects = copy.deepcopy(FIX["projects"]["data"])
        self.calls = []
        self.status = 200
        self.headers = {}
        self.entries_reply = None
        self.ignore_page = False

    def __call__(self, method, url, headers=None, json=None, timeout=20):
        self.calls.append((method, url, headers))
        if self.status != 200:
            return self.status, dict(self.headers), b"{}"
        if "/projects" in url:
            body = {"data": self.projects}
        else:
            if self.entries_reply is not None:
                body = self.entries_reply
            else:
                page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
                if self.ignore_page:
                    page = 1
                body = {"data": self.entries[(page - 1) * 100:page * 100]}
        return 200, {}, __import__("json").dumps(body).encode()

    def entry(self, eid):
        return next(e for e in self.entries if e["id"] == eid)


class FakeAW:
    def __init__(self, bucket_type="general.stopwatch", exists=True):
        self.events = []
        self.next_id = 100
        self.log = []
        self.bucket_type = bucket_type
        self.exists = exists
        self.delete_status = 200

    def mark(self):
        self.log = []

    def kinds(self):
        return [k for k, _, _ in self.log]

    def get(self, url, **kw):
        self.log.append(("GET", url, None))
        p = urlsplit(url)
        if p.path.endswith("/events"):
            q = parse_qs(p.query)
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            s = datetime.strptime(q["start"][0], fmt).replace(tzinfo=timezone.utc)
            e = datetime.strptime(q["end"][0], fmt).replace(tzinfo=timezone.utc)
            out = [ev for ev in self.events if s <= sync._parse_ts(ev["timestamp"]) < e]
            return Resp(200, copy.deepcopy(out))
        if not self.exists:
            return Resp(404, {})
        return Resp(200, {"id": BUCKET, "type": self.bucket_type})

    def post(self, url, json=None, **kw):
        self.log.append(("POST", url, json))
        assert url.endswith("/events"), "never create the bucket"
        assert isinstance(json, list)
        for ev in json:
            assert "id" not in ev
            ev = copy.deepcopy(ev)
            ev["id"] = self.next_id
            self.next_id += 1
            self.events.append(ev)
        return Resp(200, {})

    def delete(self, url, **kw):
        self.log.append(("DELETE", url, None))
        if self.delete_status != 200:
            return Resp(self.delete_status, {})
        i = int(url.rsplit("/", 1)[1])
        self.events = [e for e in self.events if e["id"] != i]
        return Resp(200, {})

    def ours(self):
        return sorted((e for e in self.events if e["data"].get("source") == "toggl-focus"),
                      key=lambda e: e["data"]["toggl_id"])


def run(toggl, aw, extra=(), now=NOW):
    lines = []
    rc = ta.main(ARGS + list(extra), toggl_http=toggl, aw_get=aw.get, aw_post=aw.post,
                 aw_delete=aw.delete, now=now, out=lines.append)
    return rc, "\n".join(lines)


@pytest.fixture(autouse=True)
def tok(monkeypatch):
    monkeypatch.setenv("TOGGL_API_TOKEN", TOKEN)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    import requests
    import local_http

    def boom(*a, **k):
        raise AssertionError("a test reached the real network")

    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(requests, "request", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    for n in ("local_get", "local_post", "local_delete", "_request"):
        monkeypatch.setattr(local_http, n, boom)


def test_first_run_inserts_eight():
    t, a = FakeToggl(), FakeAW()
    rc, text = run(t, a)
    assert rc == 0 and "inserted 8, replaced 0, deleted 0, unchanged 0" in text
    evs = a.ours()
    assert len(evs) == 8
    names = {str(p["id"]): p["name"] for p in t.projects}
    for ev in evs:
        d = ev["data"]
        assert d["source"] == "toggl-focus" and ev["timestamp"].endswith("Z")
        e = t.entry(int(d["toggl_id"]))
        want = names[str(e["project_id"])] if e["project_id"] is not None else "No project"
        assert d["label"] == want
        assert d["running"] is ("duration" not in e)
    assert sum(1 for e in evs if e["data"]["running"]) == 1


def test_second_run_changes_nothing():
    t, a = FakeToggl(), FakeAW()
    run(t, a)
    before = copy.deepcopy(a.events)
    a.mark()
    rc, text = run(t, a)
    assert rc == 0 and a.events == before
    assert "POST" not in a.kinds() and "DELETE" not in a.kinds()
    assert "inserted 0, replaced 0, deleted 0, unchanged 8" in text


def test_running_entry_grows_then_finishes():
    t, a = FakeToggl(), FakeAW()
    run(t, a)
    a.mark()
    rc, text = run(t, a, now=NOW + timedelta(minutes=10))
    assert a.kinds().count("POST") == 1 and a.kinds().count("DELETE") == 1
    assert len(a.events) == 8 and "replaced 1" in text
    t.entry(1002)["duration"] = 2000
    run(t, a, now=NOW + timedelta(minutes=20))
    assert len(a.events) == 8
    ev = next(x for x in a.events if x["data"]["toggl_id"] == "1002")
    assert ev["data"]["running"] is False and ev["duration"] == 2000
    a.mark()
    run(t, a, now=NOW + timedelta(minutes=30))
    assert "POST" not in a.kinds() and "DELETE" not in a.kinds()


@pytest.mark.parametrize("change", ["project", "desc", "duration"])
def test_changed_finished_entry_replaced(change):
    t, a = FakeToggl(), FakeAW()
    run(t, a)
    e = t.entry(1028)
    if change == "project":
        e["project_id"] = 1011
    elif change == "desc":
        e["description"] = "edited"
    else:
        e["duration"] = 999
    a.mark()
    run(t, a)
    assert a.kinds().count("POST") == 1 and a.kinds().count("DELETE") == 1
    assert len([x for x in a.events if x["data"]["toggl_id"] == "1028"]) == 1
    assert len(a.events) == 8


def test_removed_entry_deleted_and_old_ours_left_alone():
    t, a = FakeToggl(), FakeAW()
    run(t, a)
    old = {"id": 5, "timestamp": "2026-10-01T10:00:00.000Z", "duration": 5.0,
           "data": {"label": "x", "source": "toggl-focus", "toggl_id": "9999",
                    "running": False, "description": "", "type": ""}}
    a.events.append(old)
    t.entries = [e for e in t.entries if e["id"] != 1023]
    rc, text = run(t, a)
    ids = {x["data"]["toggl_id"] for x in a.events}
    assert "1023" not in ids and "9999" in ids and len(a.ours()) == 8
    assert "deleted 1" in text


def test_foreign_events_never_touched():
    t, a = FakeToggl(), FakeAW()
    lab = t.projects[0]["name"]
    a.events.append({"id": 7, "timestamp": "2026-11-10T15:00:00.000Z", "duration": 60.0,
                     "data": {"label": lab}})
    run(t, a)
    t.entry(1028)["duration"] = 5
    run(t, a)
    t.entries = t.entries[:2]
    run(t, a)
    assert any(e["id"] == 7 for e in a.events)
    assert not any(k == "DELETE" and u.endswith("/7") for k, u, _ in a.log)


def test_no_id_in_any_body():
    t, a = FakeToggl(), FakeAW()
    run(t, a)
    t.entry(1028)["duration"] = 7
    run(t, a)
    posts = [b for k, _, b in a.log if k == "POST"]
    assert posts
    assert all("id" not in ev for b in posts for ev in b)


def test_dry_run_matches_real_run():
    t, a = FakeToggl(), FakeAW()
    rc, text = run(t, a, ["--dry-run"])
    assert rc == 0 and "DRY RUN" in text and a.events == []
    assert set(a.kinds()) == {"GET"}
    assert len(t.calls) == 2
    assert "DRY RUN: would insert 8, replace 0, delete 0, unchanged 0" in text
    rc, text2 = run(t, a)
    assert "inserted 8, replaced 0, deleted 0, unchanged 0" in text2


def test_zero_entries_exit3_no_aw_call():
    t, a = FakeToggl(), FakeAW()
    run(t, a)
    keep = copy.deepcopy(a.events)
    a.mark()
    t.entries = []
    rc, text = run(t, a)
    assert rc == 3 and "Toggl returned 0 entries for the last 7 days." in text
    assert a.log == [] and a.events == keep


@pytest.mark.parametrize("reply", [{"error": "x"}, [], "nope"])
def test_wrong_shape_no_aw_call(reply):
    t, a = FakeToggl(), FakeAW()
    t.entries_reply = reply
    a.events.append({"id": 3, "timestamp": "2026-11-10T15:00:00.000Z", "duration": 1.0,
                     "data": {"label": "a", "source": "toggl-focus", "toggl_id": "1"}})
    rc, text = run(t, a)
    assert rc == 1 and a.log == [] and len(a.events) == 1


def test_401_and_402():
    t, a = FakeToggl(), FakeAW()
    t.status = 401
    rc, text = run(t, a)
    assert rc == 1 and "TOGGL_API_TOKEN" in text and a.log == [] and len(t.calls) == 1
    t, a = FakeToggl(), FakeAW()
    t.status = 402
    t.headers = {"X-Toggl-Quota-Resets-In": "1234"}
    rc, text = run(t, a)
    assert rc == 1 and "1234" in text and a.log == [] and len(t.calls) == 1


def test_request_budget():
    t, a = FakeToggl(), FakeAW()
    run(t, a)
    assert len(t.calls) == 2 and all(m == "GET" for m, _, _ in t.calls)
    assert all(u.startswith("https://focus.toggl.com/") for _, u, _ in t.calls)
    assert "include_taskless=true" in t.calls[1][1]
    t, a = FakeToggl(), FakeAW()
    base = t.entries[0]
    t.entries = [dict(base, id=5000 + i) for i in range(100)]
    rc, _ = run(t, a)
    assert rc == 0 and len(t.calls) == 3  # projects, a full page, an empty page


@pytest.mark.parametrize("kw", [{"exists": False}, {"bucket_type": "currentwindow"}])
def test_missing_or_wrong_bucket(kw):
    t, a = FakeToggl(), FakeAW(**kw)
    rc, text = run(t, a)
    assert rc == 1 and "Open ActivityWatch, go to the Stopwatch page" in text
    assert "POST" not in a.kinds() and "DELETE" not in a.kinds()


@pytest.mark.parametrize("extra", [["--aw-url", "http://example.com:5600"],
                                   ["--aw-url", "https://localhost:5600"],
                                   ["--days", "0"], ["--days", "40"], ["--days", "x"]])
def test_refused_arguments(extra):
    t, a = FakeToggl(), FakeAW()
    rc, text = run(t, a, extra)
    assert rc == 2 and t.calls == [] and a.log == []


def test_bad_org_and_missing_token(monkeypatch):
    t, a = FakeToggl(), FakeAW()
    lines = []
    assert ta.main(["--org", "4x", "--workspace", "1"], toggl_http=t, aw_get=a.get,
                   out=lines.append) == 2
    assert t.calls == []
    monkeypatch.delenv("TOGGL_API_TOKEN")
    rc, text = run(t, a)
    assert rc == 2 and "TOGGL_API_TOKEN" in text and t.calls == [] and a.log == []


def test_no_leaks(capsys):
    t, a = FakeToggl(), FakeAW()
    t.entry(1028)["description"] = CANARY
    rc, text = run(t, a)
    rc2, text2 = run(t, a, ["--dry-run"])
    cap = capsys.readouterr()
    blob = text + text2 + cap.out + cap.err
    assert TOKEN not in blob and CANARY not in blob
    assert "4001" not in blob and "4002" not in blob
    assert not re.search(r"\b10(0[0-9]|2[0-9])\b", blob)
    assert TOKEN not in json.dumps([b for _, _, b in a.log])
    assert CANARY in json.dumps([b for _, _, b in a.log])  # data only, never printed


def test_delete_failure_heals_on_rerun():
    t, a = FakeToggl(), FakeAW()
    run(t, a)
    t.entry(1028)["duration"] = 7
    a.delete_status = 500
    rc, text = run(t, a)
    assert rc == 1 and len(a.events) == 9
    a.delete_status = 200
    rc, _ = run(t, a)
    assert rc == 0 and len(a.events) == 8


def test_window_boundary_detroit_midnight():
    # entry 1027 starts 2026-11-09T04:06Z = Nov 8 23:06 in Detroit
    t, a = FakeToggl(), FakeAW()
    run(t, a, ["--days", "3"])
    assert any(e["data"]["toggl_id"] == "1027" for e in a.events)
    t2, a2 = FakeToggl(), FakeAW()
    run(t2, a2, ["--days", "2"])
    assert not any(e["data"]["toggl_id"] == "1027" for e in a2.events)
    assert any(e["data"]["toggl_id"] == "1028" for e in a2.events)


def test_delete_cap():
    t, a = FakeToggl(), FakeAW()
    for i in range(201):
        a.events.append({"id": i, "timestamp": "2026-11-10T15:00:00.000Z", "duration": 1.0,
                         "data": {"label": "a", "source": "toggl-focus", "toggl_id": f"x{i}"}})
    rc, text = run(t, a)
    assert rc == 1 and "Refusing to delete 201 events at once." in text
    assert "POST" not in a.kinds() and "DELETE" not in a.kinds()


def test_source_rules():
    src = (HERE / "scripts" / "toggl_to_activitywatch.py").read_text("utf-8")
    assert "os.system" not in src and "subprocess" not in src
    assert len(re.findall(r"\brequests\.\w+\(", src)) == 1
    assert not re.search(r"""["'](POST|PUT|PATCH|DELETE)["']""", src)
    assert "local_http.local_get" in src and "local_http.local_delete" in src


def test_window_larger_than_budget_refused():
    t, a = FakeToggl(), FakeAW()
    base = t.entries[0]
    t.entries = [dict(base, id=5000 + i) for i in range(600)]
    a.events.append({"id": 3, "timestamp": "2026-11-10T15:00:00.000Z", "duration": 1.0,
                     "data": {"label": "a", "source": "toggl-focus", "toggl_id": "1"}})
    rc, text = run(t, a)
    assert rc == 1 and "larger than this script reads" in text
    assert len(t.calls) == 5 and a.log == [] and len(a.events) == 1


def test_page_ignored_by_server_not_inflated():
    t, a = FakeToggl(), FakeAW()
    base = t.entries[0]
    t.entries = [dict(base, id=5000 + i) for i in range(100)]
    t.ignore_page = True
    rc, text = run(t, a)
    assert rc == 0 and "Toggl: 100 entries" in text and len(t.calls) == 3
    assert len(a.events) == 100


def test_full_projects_page_refused():
    t, a = FakeToggl(), FakeAW()
    t.projects = [dict(t.projects[0], id=9000 + i) for i in range(100)]
    rc, text = run(t, a)
    assert rc == 1 and "larger than this script reads" in text and a.log == []
    assert len(t.calls) == 1


def test_delete_404_counts_as_done():
    t, a = FakeToggl(), FakeAW()
    run(t, a)
    t.entry(1028)["duration"] = 7
    t.entry(1023)["duration"] = 9
    a.delete_status = 404
    rc, text = run(t, a)
    assert rc == 0 and a.kinds().count("DELETE") == 2


@pytest.mark.parametrize("url", ["http://localhost:5600/foo", "http://localhost:5600?x=1",
                                 "http://localhost:5600/#f", "http://localhost:5600/?"])
def test_aw_url_must_be_bare(url):
    t, a = FakeToggl(), FakeAW()
    rc, text = run(t, a, ["--aw-url", url])
    assert rc == 2 and "Nothing was requested" in text and t.calls == [] and a.log == []
