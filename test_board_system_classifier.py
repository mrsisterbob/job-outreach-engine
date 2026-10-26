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
