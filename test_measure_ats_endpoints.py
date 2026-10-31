"""Offline tests for scripts/measure_ats_endpoints.py: injected fakes, no network, no sleeping."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

import ats_endpoint_report as aer  # noqa: E402
import measure_ats_endpoints as m  # noqa: E402


class Resp:
    def __init__(self, status=200, text="{}"):
        self.status_code, self.text = status, text


class Fake:
    def __init__(self, resp=None, exc=None):
        self.calls, self.resp, self.exc = [], resp or Resp(), exc

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


def go(argv, get=None):
    lines = []
    code = m.run(argv, get=get or Fake(), post=Fake(), pause=0, out=lines.append)
    return code, lines


def test_no_board_makes_zero_requests():
    fake = Fake()
    code, lines = go([], get=fake)
    assert code == 2 and fake.calls == []
    assert sum("not measured (no board supplied)" in l for l in lines) == 8


def test_three_boards_three_requests():
    fake = Fake()
    code, lines = go(["--board", "workable=a", "--board", "bamboohr=b",
                      "--board", "oracle=c.example.com"], get=fake)
    assert code == 0 and len(fake.calls) == 3
    assert sum("not measured (no board supplied)" in l for l in lines) == 5
    assert "3 of 3 supplied boards got an answer" in lines[-1]
    assert fake.calls[0][1]["allow_redirects"] is False


def test_none_spec_makes_no_request():
    fake = Fake()
    code, lines = go(["--board", "icims=acme"], get=fake)
    assert fake.calls == []
    assert any("icims: no public endpoint" in l for l in lines)


def test_timeout_is_no_answer_not_empty():
    code, lines = go(["--board", "workable=a"], get=Fake(exc=TimeoutError("x")))
    line = next(l for l in lines if l.startswith("workable:"))
    assert "no_answer" in line and "ok_empty" not in line


def test_redirect_is_http_error_not_followed():
    fake = Fake(Resp(302, ""))
    code, lines = go(["--board", "bamboohr=a"], get=fake)
    assert len(fake.calls) == 1
    assert any(l.startswith("bamboohr: http_error | http 302") for l in lines)


def test_max_requests_stops_and_says_so():
    fake = Fake()
    code, lines = go(["--board", "workable=a", "--board", "bamboohr=b",
                      "--board", "oracle=c.example.com", "--max-requests", "2"], get=fake)
    assert len(fake.calls) == 2
    assert any("skipped: --max-requests 2 reached" in l for l in lines)


def test_record_refused_inside_repo_and_off_by_default(tmp_path):
    fake = Fake()
    code, lines = go(["--board", "workable=a", "--record", str(ROOT / "rec_x")], get=fake)
    assert code == 2 and fake.calls == [] and not (ROOT / "rec_x").exists()
    body = Resp(200, json.dumps({"jobs": []}))
    go(["--board", "workable=a"], get=Fake(body))
    assert list(tmp_path.iterdir()) == []
    code, _ = go(["--board", "workable=a", "--record", str(tmp_path / "out")], get=Fake(body))
    assert code == 0 and (tmp_path / "out" / "workable.json").exists()


def test_specs_are_well_formed():
    assert set(m.ENDPOINT_SPECS) <= set(aer.UNFETCHED_SYSTEMS)
    for system, spec in m.ENDPOINT_SPECS.items():
        if spec is None:
            continue
        assert spec.system == system
        assert spec.url_template.startswith("https://")
        assert spec.method in ("GET", "POST")
        assert spec.source_note.strip()


def test_no_answer_cause_is_printed():
    _, lines = go(["--board", "workable=a"], get=Fake(exc=TimeoutError("x")))
    assert "    cause: TimeoutError" in lines
    _, lines = go(["--board", "oracle=a/b"])
    assert any(l.startswith("    cause: bad board ref") for l in lines)


def test_oracle_apply_not_available_and_url_only_documented_params():
    spec = m.ENDPOINT_SPECS["oracle"]
    assert spec.apply_paths == () and "limit" not in spec.url_template
    body = json.dumps({"items": [{"requisitionList": [{"Title": "t", "PrimaryLocation": "x", "Id": "9"}]}]})
    _, lines = go(["--board", "oracle=h.example.com"], get=Fake(Resp(200, body)))
    assert any("apply link: not available" in l for l in lines)
    assert any("apply 0/1" in l for l in lines)


def test_record_failure_does_not_lose_report(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    code, lines = go(["--board", "workable=a", "--record", str(blocker / "sub")],
                     get=Fake(Resp(200, json.dumps({"jobs": []}))))
    assert code == 0
    assert any(l.startswith("workable: ok_empty") for l in lines)
    assert lines[-1].startswith("record failed for workable")


def test_truncated_body_saved_as_txt_and_stale_removed(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "workable.json").write_text("old")
    big = '{"jobs": [], "pad": "' + "x" * (m.MAX_BYTES + 10) + '"}'
    go(["--board", "workable=a", "--record", str(d)], get=Fake(Resp(200, big)))
    assert not (d / "workable.json").exists()
    assert (d / "workable.txt").exists() and (d / "workable.truncated").exists()
    go(["--board", "workable=a", "--record", str(d)], get=Fake(Resp(200, "{}")))
    assert (d / "workable.json").exists()
    assert not (d / "workable.txt").exists() and not (d / "workable.truncated").exists()


def test_no_public_endpoint_prints_one_line():
    _, lines = go(["--board", "icims=acme"])
    assert sum(l.startswith("icims:") for l in lines) == 1


def test_summary_separates_answered_failed_skipped():
    _, lines = go(["--board", "workable=a", "--board", "bamboohr=b", "--board", "oracle=h.example.com",
                   "--max-requests", "2"], get=Fake(exc=TimeoutError()))
    assert "0 of 3 supplied boards got an answer; 2 failed (no answer); 1 skipped" in lines[-1]
    _, lines = go(["--board", "workable=a"], get=Fake(Resp(200, "{}")))
    assert "1 of 1 supplied boards got an answer; 0 failed" in lines[-1]


def test_duplicate_board_is_an_error():
    fake = Fake()
    code, lines = go(["--board", "workable=a", "--board", "workable=b"], get=fake)
    assert code == 2 and fake.calls == [] and "duplicate --board for workable" in lines[0]


def test_record_off_by_default_never_calls_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_record", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote")))
    code, _ = go(["--board", "workable=a"], get=Fake(Resp(200, "{}")))
    assert code == 0 and list(tmp_path.iterdir()) == []


def test_run_counts_title_location_apply():
    jobs = [{"title": "a", "city": "x", "url": "u"}, {"title": "b"}, {"city": "y", "shortlink": "s"}]
    _, lines = go(["--board", "workable=a"], get=Fake(Resp(200, json.dumps({"jobs": jobs}))))
    line = next(l for l in lines if l.startswith("workable:"))
    assert "postings 3 | title 2/3 | location 2/3 | apply 2/3 | all three 1/3" in line


def test_post_spec_uses_post_with_body(monkeypatch):
    spec = aer.EndpointSpec("ukg", "POST", "https://{0}.example.com/s", '{"q":1}', ("jobs",),
                            (("t",),), (("l",),), (("u",),), "test note")
    monkeypatch.setitem(m.ENDPOINT_SPECS, "ukg", spec)
    g, p = Fake(), Fake(Resp(200, json.dumps({"jobs": [{"t": 1, "l": 2, "u": 3}]})))
    lines = []
    m.run(["--board", "ukg=acme"], get=g, post=p, pause=0, out=lines.append)
    assert g.calls == [] and len(p.calls) == 1
    assert p.calls[0][1]["data"] == '{"q":1}' and p.calls[0][1]["allow_redirects"] is False
