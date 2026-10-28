from board_system_classifier import classify_board_system as c


def test_workday():
    assert c("https://acme.wd5.myworkdayjobs.com/en-US/Careers", "") == "workday"


def test_greenhouse_iframe():
    assert c("https://acme.com/careers", '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>') == "greenhouse"


def test_lever_host():
    assert c("https://jobs.lever.co/acme", "") == "lever"


def test_icims_link():
    assert c("https://acme.com/careers", '<a href="https://careers-acme.icims.com/jobs">Jobs</a>') == "icims"


def test_taleo():
    assert c("https://acme.taleo.net/careersection/ex/jobsearch.ftl", "") == "taleo"


def test_successfactors_script():
    assert c("https://acme.com/c", '<script src="https://career5.successfactors.com/x.js"></script>') == "successfactors"


def test_oracle():
    assert c("https://eabc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience", "") == "oracle"


def test_own_site():
    assert c("https://acme.com/careers", "<h1>Open positions</h1><ul><li>Engineer</li></ul>") == "own_site"


def test_unknown():
    assert c("https://acme.com/about", "<p>We make chairs.</p>") == "unknown"


def test_two_vendors_not_first_wins():
    html = '<a href="https://jobs.lever.co/a">a</a><a href="https://boards.greenhouse.io/b">b</a>'
    assert c("https://acme.com/careers", html) == "unknown"


def test_empty():
    assert c("", "") == "unknown"


def test_non_string():
    assert c(None, 123) == "unknown"


# ---- scripts/measure_board_systems.py endpoint judging (fake responses, no network) ----
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "measure_board_systems", pathlib.Path(__file__).parent / "scripts" / "measure_board_systems.py")
mbs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mbs)


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data, self.headers, self.text, self.url = status, data, {}, "", "u"

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def test_judge_smartrecruiters_needs_postings_and_workable_needs_name():
    assert mbs.judge("smartrecruiters", _Resp(data={"totalFound": 0, "content": []})) == (False, "")
    assert mbs.judge("smartrecruiters", _Resp(data={"totalFound": 3, "content": [{"company": {"name": "Acme"}}]})) == (True, "Acme")
    assert mbs.judge("workable", _Resp(data={"name": "Acme", "jobs": [{}]})) == (True, "Acme")
    assert mbs.judge("workable", _Resp(data={"name": "Acme", "jobs": []}))[0] is False
    assert mbs.judge("workable", _Resp(404)) == (False, "")


def test_judge_bamboohr_result_key_and_no_answer():
    assert mbs.judge("bamboohr", _Resp(data={"meta": {}, "result": []}))[0] is True
    assert mbs.judge("bamboohr", _Resp(302))[0] is False
    assert mbs.judge("lever", None)[0] is None


def test_measure_stays_within_nine_requests(monkeypatch):
    calls = []
    monkeypatch.setattr(mbs, "_get", lambda req, url, pause=0: calls.append(url) or _Resp(404))
    rec = mbs.measure(None, {"domains": []}, "acme", 0)
    assert rec["bucket"] == "not_located" and rec["domain_source"] == "guess" and len(calls) <= 9


def test_vendor_markers_match_on_domain_boundaries():
    assert c("https://clever.com/jobs", "") != "lever"
    assert c("https://unworkable.com/jobs", "") != "workable"
    assert c("https://jobs.lever.co/acme", "") == "lever"
    assert c("https://acme.myworkdayjobs.com/en-US/x", "") == "workday"
    assert c("https://www.acme.com/careers", '<a href="https://notlever.co/x">') != "lever"
    assert c("https://www.acme.com/careers", '<a href="https://jobs.lever.co/acme">') == "lever"
    assert c("https://adp.com/recruit/x", "") == "adp"
    assert c("https://adp.com/about", "") != "adp"


def test_judge_throttle_and_server_errors_are_no_answer():
    for code in (429, 500, 503):
        assert mbs.judge("lever", _Resp(code))[0] is None
    assert mbs.judge("lever", _Resp(404))[0] is False


def test_redirect_resolved_relative_and_vendor_host_classified_before_200(monkeypatch):
    r302 = _Resp(302)
    r302.headers = {"Location": "https://jobs.lever.co/acme"}
    seq = iter([_Resp(404)] * 6 + [r302])
    monkeypatch.setattr(mbs, "_get", lambda req, url, pause=0: next(seq))
    monkeypatch.setattr(mbs, "_get_url", lambda req, url, pause: next(seq))
    rec = mbs.measure(None, {"domains": []}, "acme", 0)
    assert rec["bucket"] == "lever"
    rel = _Resp(302)
    rel.headers = {"Location": "/en/careers"}
    seen = []
    seq2 = iter([rel, _Resp(404), _Resp(404), _Resp(404)])
    monkeypatch.setattr(mbs, "_get", lambda req, url, pause=0: _Resp(404))
    monkeypatch.setattr(mbs, "_get_url", lambda req, url, pause: seen.append(url) or next(seq2))
    mbs.measure(None, {"domains": []}, "acme", 0)
    assert "https://acme.com/en/careers" in seen


def test_no_answer_company_is_flagged_and_cache_is_atomic_and_corrupt_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(mbs, "_get", lambda req, url, pause=0: _Resp(503))
    monkeypatch.setattr(mbs, "_get_url", lambda req, url, pause: _Resp(404))
    rec = mbs.measure(None, {"domains": []}, "acme", 0)
    assert set(rec["no_answer"]) == set(mbs.SYSTEMS)
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert mbs.load_cache(p) == {}
    mbs.save_cache(p, {"a": {"bucket": "x"}})
    assert mbs.load_cache(p) == {"a": {"bucket": "x"}}
    assert not (tmp_path / "c.json.tmp").exists()


def test_report_counts_only_target_keys(capsys):
    cache = {"in": {"bucket": "has_board", "boards": ["lever"], "count": 12, "slug": "in", "name": "In", "board_names": {}},
             "stale": {"bucket": "has_board", "boards": ["lever"], "count": 12, "slug": "stale", "name": "Stale", "board_names": {}}}
    mbs.report(cache, {"in": {}}, lambda s: s)
    out = capsys.readouterr().out
    assert "DENOMINATOR 1" in out and "Stale" not in out


# ---- board_url / workday_triple_shape recorded by measure (fake responses, no network) ----
def _measure_careers(monkeypatch, first, second=None):
    seq = iter([first] + ([second] if second else []))
    monkeypatch.setattr(mbs, "_get", lambda req, url, pause=0: _Resp(404))
    monkeypatch.setattr(mbs, "_get_url", lambda req, url, pause: next(seq))
    return mbs.measure(None, {"domains": []}, "acme", 0)


def test_board_url_redirect_onto_workday_keeps_host_path_and_triple(monkeypatch):
    r302 = _Resp(302)
    r302.headers = {"Location": "https://acme.wd5.myworkdayjobs.com/en-US/External_Careers?q=1#top"}
    rec = _measure_careers(monkeypatch, r302)
    assert rec["bucket"] == "workday"
    assert rec["board_url"] == "acme.wd5.myworkdayjobs.com/en-US/External_Careers"
    assert rec["workday_triple_shape"] == "acme/wd5/External_Careers"


def test_board_url_200_lever_sets_url_and_no_triple(monkeypatch):
    r = _Resp(200)
    r.url, r.text = "https://jobs.lever.co/acme", "<html></html>"
    rec = _measure_careers(monkeypatch, r)
    assert rec["bucket"] == "lever"
    assert rec["board_url"] == "jobs.lever.co/acme"
    assert "workday_triple_shape" not in rec


def test_board_url_absent_for_own_site(monkeypatch):
    r = _Resp(200)
    r.url, r.text = "https://acme.com/careers", "<h1>Open positions</h1><ul><li>Engineer</li></ul>"
    rec = _measure_careers(monkeypatch, r)
    assert rec["bucket"] == "own_site"
    assert "board_url" not in rec and "workday_triple_shape" not in rec


def test_board_url_never_carries_query_or_fragment(monkeypatch):
    r = _Resp(200)
    r.url, r.text = "https://jobs.lever.co/acme?lever-source=x&token=secret#apply", ""
    rec = _measure_careers(monkeypatch, r)
    assert rec["board_url"] == "jobs.lever.co/acme"
    assert "?" not in rec["board_url"] and "#" not in rec["board_url"]


def test_board_url_cache_file_name_is_v3():
    assert "v3" in mbs.CACHE_NAME and "v2" not in mbs.CACHE_NAME
