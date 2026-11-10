"""Tests for scripts/probe_toggl_write.py. Fakes only: nothing here touches the network."""
import importlib.util
import json
import json as jl
import re
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "scripts" / "probe_toggl_write.py"
spec = importlib.util.spec_from_file_location("probe_toggl_write", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

TOKEN = "tok-ZZ-4471"
PROJ = "ZZ-CANARY-PROJ-4471"
EMAIL = "canary4471@example.com"
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
BASE = "https://focus.toggl.com/api/organizations/4001/workspaces/4002"


class NetworkBlocked(BaseException):
    """Raised by the autouse guard; a BaseException so no `except Exception` can hide it."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise NetworkBlocked("a test tried to reach the network")
    monkeypatch.setattr(requests.Session, "request", blocked)
    monkeypatch.setattr(requests, "request", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


class FakeFocus:
    """Behaves like Focus as documented: start replaces a running timer, stop ends whatever runs,
    204 when idle."""

    def __init__(self, replace=True, running=None, foreign_from=None, stop_status=200,
                 delete_status=204, stubborn=False, override=None, echo_auth=False, projects=None,
                 quota=None, raise_at=None, start_fail=None, delete_keeps_running=False,
                 stop_fail_first=False):
        self.replace = replace
        self.running = list(running or [])
        self.entries = {}
        self.next_id = 7001
        self.foreign_from = foreign_from
        self.stop_status = stop_status
        self.delete_status = delete_status
        self.stubborn = stubborn
        self.override = override or {}
        self.echo_auth = echo_auth
        self.projects = projects if projects is not None else []
        self.quota = quota
        self.raise_at = raise_at or {}
        self.start_fail = start_fail or {}      # nth start -> True if the entry is created anyway
        self.delete_keeps_running = delete_keeps_running
        self.stop_fail_first = stop_fail_first
        self.calls = []
        self.ncurrent = 0
        self.nstart = 0
        self.nstop = 0

    def _entry(self, i):
        return dict(self.entries.get(i, {"id": i, "project_id": None}))

    def __call__(self, method, url, headers=None, json=None, timeout=20):
        return self._handle(method, url, headers or {}, json)

    def _handle(self, method, url, headers, body):
        n = len(self.calls) + 1
        rec = {"method": method, "url": url, "headers": headers, "json": body, "returned": None}
        self.calls.append(rec)
        if n in self.raise_at:
            raise self.raise_at[n]
        rh = {}
        if n == 1 and self.quota is not None:
            rh["X-Toggl-Quota-Remaining"] = str(self.quota)
        if n in self.override:
            st, h = self.override[n]
            rec["status"] = st
            return st, dict(h), b'{"error": "limit"}'
        path = url.split("?")[0]
        if path.endswith("/tracking/current") and method == "GET":
            self.ncurrent += 1
            if self.foreign_from and self.ncurrent >= self.foreign_from:
                rec["returned"] = "999"
                return 200, rh, b'{"id": 999, "project_id": null, "start": "2026-11-10T11:00:00Z"}'
            if not self.running:
                return 204, rh, b""
            i = self.running[0]
            rec["returned"] = str(i)
            return 200, rh, jl.dumps(self._entry(i)).encode()
        if path.endswith("/tracking/start") and method == "POST":
            self.nstart += 1
            i = self.next_id
            self.next_id += 1
            fail = self.start_fail.get(self.nstart)
            if self.nstart in self.start_fail and fail is False:
                return 500, rh, b"{}"
            self.entries[i] = {"id": i, "project_id": body.get("project_id"),
                               "description": body["description"], "start": body["start"],
                               "user_email": EMAIL}
            if self.replace:
                for r in self.running:
                    self.entries[r]["duration"] = 5
                self.running = []
            self.running.append(i)
            if self.nstart in self.start_fail:
                return 500, rh, b"{}"
            return 200, rh, jl.dumps(self._entry(i)).encode()
        if path.endswith("/tracking/stop") and method == "POST":
            self.nstop += 1
            if self.stop_status != 200 or (self.stop_fail_first and self.nstop == 1):
                err = {"error": "boom " + headers.get("Authorization", "")} if self.echo_auth else {}
                return (self.stop_status if self.stop_status != 200 else 500), rh, jl.dumps(err).encode()
            if not self.running:
                return 404, rh, b'{"error": "Not tracking a time entry"}'
            if self.stubborn:
                return 200, rh, jl.dumps(self._entry(self.running[0])).encode()
            i = self.running.pop(0)
            if i in self.entries:
                self.entries[i]["duration"] = 5
                self.entries[i]["end"] = body["end"]
            return 200, rh, jl.dumps(self._entry(i)).encode()
        if path.endswith("/time-entries/bulk") and method == "DELETE":
            if self.delete_status < 300:
                for s in re.search(r"ids=([^&]*)", url).group(1).split(","):
                    self.entries.pop(int(s), None)
                    if not (self.stubborn or self.delete_keeps_running) and int(s) in self.running:
                        self.running.remove(int(s))
            return self.delete_status, rh, b""
        if path.endswith("/time-entries") and method == "GET":
            return 200, rh, jl.dumps({"data": [self._entry(i) for i in self.entries]}).encode()
        if path.endswith("/projects") and method == "GET":
            return 200, rh, jl.dumps({"data": self.projects}).encode()
        return 404, rh, b"{}"

    def labels(self):
        out = []
        for c in self.calls:
            p = c["url"].split("?")[0]
            out.append(c["method"] + " " + p.rsplit("/", 1)[1])
        return out

    def writes(self):
        return [c for c in self.calls if c["method"] in ("POST", "DELETE")]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TOGGL_API_TOKEN", TOKEN)


def run(tmp_path, fake, extra=(), yes=True, record=None):
    out = []
    rec = record or str(tmp_path / "rec")
    argv = ["--record", rec, "--org", "4001", "--workspace", "4002"] + list(extra)
    if yes:
        argv.append("--yes")
    code = probe.main(argv, http=fake, out=out.append, now=NOW)
    return code, "\n".join(out), Path(rec)


def test_no_yes_prints_plan_and_sends_nothing(tmp_path, env):
    fake = FakeFocus()
    code, text, _ = run(tmp_path, fake, yes=False)
    assert code == 2 and fake.calls == []
    assert "start throwaway entry A" in text


def test_no_token_and_bad_org(tmp_path, monkeypatch):
    monkeypatch.delenv("TOGGL_API_TOKEN", raising=False)
    fake = FakeFocus()
    code, text, _ = run(tmp_path, fake)
    assert code == 2 and "TOGGL_API_TOKEN" in text and fake.calls == []
    monkeypatch.setenv("TOGGL_API_TOKEN", TOKEN)
    out = []
    code = probe.main(["--record", str(tmp_path / "r"), "--org", "4001/x", "--workspace", "4002", "--yes"],
                      http=fake, out=out.append, now=NOW)
    assert code == 2 and fake.calls == []


def test_record_inside_repo_refused(tmp_path, env):
    fake = FakeFocus()
    code, text, _ = run(tmp_path, fake, record=str(probe.ROOT / "zz_probe_rec"))
    assert code == 2 and fake.calls == []
    assert not (probe.ROOT / "zz_probe_rec").exists()


def test_happy_path(tmp_path, env):
    fake = FakeFocus()
    code, text, _ = run(tmp_path, fake)
    assert code == 0, text
    assert fake.labels() == ["GET current", "POST start", "GET current", "POST start", "GET current",
                             "POST stop", "GET time-entries", "DELETE bulk", "GET current"]
    for c in fake.calls:
        assert c["url"].startswith("https://focus.toggl.com/")
        assert c["headers"]["Authorization"] == "Bearer " + TOKEN
    starts = [c["json"] for c in fake.calls if c["url"].endswith("/tracking/start")]
    assert all("start" in b and b["description"] for b in starts)
    assert "start while running: replaced the timer" in text
    assert "start without a project: works" in text
    assert probe.CLOSING in text


def test_already_running_makes_no_write(tmp_path, env):
    fake = FakeFocus(running=[555])
    code, text, _ = run(tmp_path, fake)
    assert code == 1 and probe.RUNNING_MSG in text
    assert fake.writes() == [] and len(fake.calls) == 1


def test_pre_stop_rule_foreign_timer(tmp_path, env):
    fake = FakeFocus(foreign_from=3)
    code, text, _ = run(tmp_path, fake)
    assert code == 1
    assert "left it alone" in text
    assert not any(c["url"].endswith("/tracking/stop") for c in fake.calls)
    dels = [c for c in fake.calls if c["method"] == "DELETE"]
    assert len(dels) == 1
    assert dels[0]["url"].endswith("ids=7001,7002")


def test_delete_ids_and_stop_preceded_by_current(tmp_path, env):
    fake = FakeFocus()
    run(tmp_path, fake)
    dels = [c for c in fake.calls if c["method"] == "DELETE"]
    assert len(dels) == 1
    assert dels[0]["url"].split("?")[1] == "ids=7001,7002"
    assert dels[0]["url"].startswith(BASE + "/time-entries/bulk")
    for i, c in enumerate(fake.calls):
        if c["url"].endswith("/tracking/stop"):
            prev = fake.calls[i - 1]
            assert prev["url"].endswith("/tracking/current") and prev["returned"] in ("7001", "7002")


def test_stop_fails_delete_still_attempted(tmp_path, env):
    fake = FakeFocus(stop_status=500)
    code, text, _ = run(tmp_path, fake)
    assert code == 1
    assert any(c["method"] == "DELETE" for c in fake.calls)
    fake2 = FakeFocus(stop_status=500, delete_status=500)
    code, text, _ = run(tmp_path / "b", fake2)
    assert code == 1 and probe.MAY_EXIST_MSG in text
    assert sum(1 for c in fake2.calls if c["method"] == "DELETE") == 1


def test_both_running_cleanup_stops_until_idle(tmp_path, env):
    fake = FakeFocus(replace=False)
    code, text, _ = run(tmp_path, fake)
    assert "start while running: both running" in text
    assert fake.running == []
    assert len(fake.calls) <= 14
    for i, c in enumerate(fake.calls):
        if c["url"].endswith("/tracking/stop"):
            assert fake.calls[i - 1]["returned"] in ("7001", "7002")


def test_project_name_matching(tmp_path, env):
    projects = [{"id": 321, "name": PROJ, "archived_at": None}]
    fake = FakeFocus(projects=projects)
    code, text, _ = run(tmp_path, fake, extra=["--project-name", PROJ.lower()])
    assert code == 0, text
    starts = [c["json"] for c in fake.calls if c["url"].endswith("/tracking/start")]
    assert all(b["project_id"] == 321 for b in starts)
    assert PROJ not in text and PROJ.lower() not in text
    # missing project
    fake2 = FakeFocus(projects=projects)
    code, text, _ = run(tmp_path / "m", fake2, extra=["--project-name", "nope-zz"])
    assert code == 1 and "Project not found." in text and fake2.writes() == []
    assert "nope-zz" not in text
    # archived project
    fake3 = FakeFocus(projects=[{"id": 9, "name": PROJ, "archived_at": "2026-01-01T00:00:00Z"}])
    code, text, _ = run(tmp_path / "a", fake3, extra=["--project-name", PROJ])
    assert code == 1 and "Project not found." in text and fake3.writes() == []
    assert PROJ not in text


def test_token_and_ids_never_leak(tmp_path, env):
    fake = FakeFocus(stop_status=500, echo_auth=True)
    code, text, rec = run(tmp_path, fake)
    files = sorted(rec.iterdir())
    assert files
    for f in files:
        assert TOKEN not in f.name
        assert TOKEN not in f.read_text(encoding="utf-8")
    assert TOKEN not in text
    assert "4001" not in text and "4002" not in text
    scrubbed = [f for f in files if f.name.endswith(".scrubbed.json")][0].read_text(encoding="utf-8")
    assert "4001" not in scrubbed and "4002" not in scrubbed
    assert "<org>" in scrubbed and "<ws>" in scrubbed


def test_shape_only_and_scrubbed_aliases(tmp_path, env, capsys):
    projects = [{"id": 321, "name": PROJ, "archived_at": None}]
    fake = FakeFocus(projects=projects)
    code, text, rec = run(tmp_path, fake, extra=["--project-name", PROJ])
    cap = capsys.readouterr()
    for s in (text, cap.out, cap.err):
        for bad in (PROJ, EMAIL, probe.DESC_A, probe.DESC_B):
            assert bad not in s
    raw = [f for f in rec.iterdir() if not f.name.endswith(".scrubbed.json")][0].read_text(encoding="utf-8")
    sc_text = [f for f in rec.iterdir() if f.name.endswith(".scrubbed.json")][0].read_text(encoding="utf-8")
    assert EMAIL in raw and PROJ in raw and probe.DESC_A in raw
    for bad in (PROJ, EMAIL, probe.DESC_A, "ZZ-CANARY"):
        assert bad not in sc_text
    doc = json.loads(sc_text)
    start_a = [e for e in doc if e["name"] == "start A"][0]
    cur = [e for e in doc if e["name"] == "current"][1]
    assert start_a["reply"]["id"] == cur["reply"]["id"] >= 1001
    assert start_a["reply"]["id"] != 7001
    assert start_a["reply"]["start"].startswith("2026-11-10T")
    stop = [e for e in doc if e["name"] == "stop"][0]
    assert stop["reply"]["duration"] == 5
    assert start_a["reply"]["project_id"] == start_a["request"]["project_id"]


def test_402_stops_and_quota_guard(tmp_path, env):
    hdr = {"X-Toggl-Quota-Remaining": "0", "X-Toggl-Quota-Resets-In": "1800"}
    fake = FakeFocus(override={5: (402, hdr)})
    code, text, _ = run(tmp_path, fake)
    assert code == 1
    assert "X-Toggl-Quota-Remaining=0" in text and "X-Toggl-Quota-Resets-In=1800" in text
    assert len(fake.calls) == 6
    assert sum(1 for c in fake.calls if c["method"] == "DELETE") == 1
    assert fake.calls[-1]["url"].endswith("ids=7001,7002")
    # low quota after step 1
    fake2 = FakeFocus(quota=9)
    code, text, _ = run(tmp_path / "q", fake2)
    assert code == 1 and "Not enough Toggl quota" in text
    assert fake2.writes() == []
    # 402 on the very first request
    fake3 = FakeFocus(override={1: (402, hdr)})
    code, text, _ = run(tmp_path / "r", fake3)
    assert code == 1 and len(fake3.calls) == 1


def test_request_cap(tmp_path, env):
    for fake in (FakeFocus(stubborn=True, replace=False), FakeFocus(stubborn=True, stop_status=200),
                 FakeFocus(replace=False, stop_status=500, delete_status=500)):
        code, text, _ = run(tmp_path / str(id(fake)), fake)
        assert len(fake.calls) <= 14
        assert code == 1


def test_files_only_under_dir_and_source_rules(tmp_path, env):
    before = sorted(p.name for p in tmp_path.iterdir())
    fake = FakeFocus()
    code, text, rec = run(tmp_path, fake)
    after = sorted(p.name for p in tmp_path.iterdir())
    assert set(after) - set(before) == {"rec"}
    assert len(list(rec.iterdir())) == 2
    src = SCRIPT.read_text(encoding="utf-8")
    assert "track.toggl.com" not in src and "Basic" not in src
    assert ".put(" not in src and ".patch(" not in src
    methods = set(re.findall(r'"(GET|POST|DELETE|PUT|PATCH|HEAD|OPTIONS)"', src))
    assert methods == {"GET", "POST", "DELETE"}


def test_keyboard_interrupt_still_cleans_up(tmp_path, env):
    fake = FakeFocus(raise_at={3: KeyboardInterrupt()})
    out = []
    rec = tmp_path / "rec"
    with pytest.raises(KeyboardInterrupt):
        probe.main(["--record", str(rec), "--org", "4001", "--workspace", "4002", "--yes"],
                   http=fake, out=out.append, now=NOW)
    dels = [c for c in fake.calls if c["method"] == "DELETE"]
    assert len(dels) == 1 and dels[0]["url"].endswith("ids=7001")
    assert fake.entries == {} and fake.running == []
    assert len(list(rec.iterdir())) == 2
    assert any("Report" in line for line in out)
    # delete also failing: the warning is printed
    fake2 = FakeFocus(raise_at={3: KeyboardInterrupt()}, delete_status=500)
    out2 = []
    with pytest.raises(KeyboardInterrupt):
        probe.main(["--record", str(tmp_path / "rec2"), "--org", "4001", "--workspace", "4002", "--yes"],
                   http=fake2, out=out2.append, now=NOW)
    assert probe.MAY_EXIST_MSG in "\n".join(out2)


def test_interrupt_during_start_looks_for_the_entry(tmp_path, env):
    # the interrupt hits while start A is in flight; the fake already created it server side
    class Fake(FakeFocus):
        def _handle(self, method, url, headers, body):
            r = FakeFocus._handle(self, method, url, headers, body)
            if method == "POST" and url.endswith("/tracking/start"):
                raise KeyboardInterrupt()
            return r
    fake = Fake()
    with pytest.raises(KeyboardInterrupt):
        probe.main(["--record", str(tmp_path / "rec"), "--org", "4001", "--workspace", "4002", "--yes"],
                   http=fake, out=lambda t: None, now=NOW)
    assert fake.entries == {} and fake.running == []
    assert any(c["method"] == "DELETE" for c in fake.calls)


def test_failed_start_a_has_unknown_outcome(tmp_path, env):
    # 500 but the entry was created: cleanup finds it through current and deletes it
    fake = FakeFocus(start_fail={1: True})
    code, text, _ = run(tmp_path, fake)
    assert code == 1
    assert fake.entries == {} and fake.running == []
    dels = [c for c in fake.calls if c["method"] == "DELETE"]
    assert len(dels) == 1 and dels[0]["url"].endswith("ids=7001")
    # 500 and nothing created: ids is empty but the warning is still printed
    fake2 = FakeFocus(start_fail={1: False})
    code, text, _ = run(tmp_path / "b", fake2)
    assert code == 1 and probe.MAY_EXIST_MSG in text
    assert "GET current" in fake2.labels()[2:]
    assert not any(c["method"] == "DELETE" for c in fake2.calls)


def test_failed_start_b_is_adopted_by_description(tmp_path, env):
    fake = FakeFocus(start_fail={2: True})
    code, text, _ = run(tmp_path, fake)
    assert code == 1
    assert fake.entries == {} and fake.running == []
    dels = [c for c in fake.calls if c["method"] == "DELETE"]
    assert len(dels) == 1 and dels[0]["url"].endswith("ids=7001,7002")
    for i, c in enumerate(fake.calls):
        if c["url"].endswith("/tracking/stop"):
            assert fake.calls[i - 1]["returned"] in ("7001", "7002")


def test_scrub_hides_numeric_identifiers():
    aliases = {}
    obj = {"user": 4829113, "organization": 4001, "workspace": 4002, "duration": 3600, "n": 7,
           "start": "2026-11-10T12:00:00Z", "nested": [{"owner": 99887}]}
    out = probe._scrub(obj, aliases, known={"4001", "4002"})
    text = json.dumps(out)
    for bad in ("4829113", "4001", "4002", "99887"):
        assert bad not in text
    assert out["duration"] == 3600 and out["n"] == 7 and out["start"] == obj["start"]


def test_scrubbed_file_hides_user_ints(tmp_path, env):
    class Fake(FakeFocus):
        def _entry(self, i):
            e = FakeFocus._entry(self, i)
            e.update({"user": 4829113, "organization": 4001, "workspace": 4002})
            return e
    code, text, rec = run(tmp_path, Fake())
    sc = [f for f in rec.iterdir() if f.name.endswith(".scrubbed.json")][0].read_text(encoding="utf-8")
    for bad in ("4829113", "4001", "4002"):
        assert bad not in sc


def test_record_refused_in_any_git_tree(tmp_path, env):
    fake = FakeFocus()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    code, text, _ = run(tmp_path, fake, record=str(repo / "sub" / "rec"))
    assert code == 2 and fake.calls == []
    assert not (repo / "sub").exists()
    main_dir = probe.MAIN_CHECKOUT / "zz_probe_rec_never"
    code, text, _ = run(tmp_path, fake, record=str(main_dir))
    assert code == 2 and fake.calls == [] and not main_dir.exists()
    sibling = probe.ROOT.parent / "TASK-zzz" / "rec"
    code, text, _ = run(tmp_path, fake, record=str(sibling))
    assert code == 2 and fake.calls == []


def test_default_http_uses_session_without_env(monkeypatch):
    seen = {}

    class Resp:
        status_code = 204
        headers = {}
        content = b""

    def fake_request(self, method, url, **kw):
        seen["trust_env"] = self.trust_env
        seen["redirects"] = kw.get("allow_redirects")
        return Resp()
    monkeypatch.setattr(requests.Session, "request", fake_request)
    assert probe._default_http("GET", "https://focus.toggl.com/x", headers={}) == (204, {}, b"")
    assert seen == {"trust_env": False, "redirects": False}


def test_unpatched_default_http_fails_loudly(tmp_path, env):
    with pytest.raises(NetworkBlocked):
        probe.main(["--record", str(tmp_path / "rec"), "--org", "4001", "--workspace", "4002", "--yes"],
                   out=lambda t: None, now=NOW)


def test_abbreviated_yes_is_rejected(tmp_path, env):
    fake = FakeFocus()
    with pytest.raises(SystemExit) as ei:
        probe.main(["--record", str(tmp_path / "rec"), "--org", "4001", "--workspace", "4002", "--y"],
                   http=fake, out=lambda t: None, now=NOW)
    assert ei.value.code == 2 and fake.calls == []


def test_cleanup_stops_after_delete_then_rereads_current(tmp_path, env):
    fake = FakeFocus(stop_fail_first=True, delete_keeps_running=True)
    code, text, _ = run(tmp_path, fake)
    assert code == 1
    assert fake.labels()[-4:] == ["DELETE bulk", "GET current", "POST stop", "GET current"]
    assert fake.running == []
    assert "current idle: 204" in text and "final stop: accepted" in text
    # the stop in cleanup does not end it: the fresh read is what gets reported
    fake2 = FakeFocus(stubborn=True)
    code, text, _ = run(tmp_path / "s", fake2)
    assert code == 1 and "current idle: other (status 200)" in text
