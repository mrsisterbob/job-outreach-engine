"""Tests for hirebase_feed.py. Jobs are hand-built with the guessed keys (see module docstring)."""
import re
from datetime import datetime, timedelta, timezone

import hirebase_feed as hf


def norm(name):
    s = re.sub(r"[.,]", "", str(name or "")).strip()
    s = re.sub(r"\s+(inc|llc|corp|co|ltd)$", "", s, flags=re.I)
    return s.strip()


POSTED = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")


def make_job(**over):
    job = {
        "job_title": "Account Manager",
        "application_link": "https://careers.haworth.com/job/1",
        "location_raw": "Holland, MI",
        "locations": [{"city": "Holland", "region": "Michigan", "country": "United States"}],
        "date_posted": POSTED,
        "platform_job_id": "abc123",
        "company_name": "Haworth Inc.",
        "company_slug": "haworth",
        "description": "<p>Sell &amp; grow</p><ul><li>Travel</li></ul>",
    }
    job.update(over)
    return job


def test_hirebase_parse_slug_guesses_fifth_third():
    assert hf.slug_guesses("Fifth Third Bank", norm) == ["fifth-third-bank"]


def test_hirebase_parse_slug_guesses_haworth_inc():
    assert hf.slug_guesses("Haworth Inc.", norm) == ["haworth-inc", "haworth"]


def test_hirebase_parse_slug_guesses_ampersand_and_trailing_period():
    assert hf.slug_guesses("Johnson & Johnson", norm) == ["johnson-johnson"]
    assert hf.slug_guesses("Acme Corp.", norm) == ["acme-corp", "acme"]


def test_hirebase_parse_body_clamps_and_has_no_title_filter():
    body = hf.build_search_body(["haworth"], 999, 5000)
    assert body == {"company_slugs": ["haworth"], "geo_locations": [{"region": "Michigan"}],
                    "days_ago": 30, "page": 1, "limit": 100}
    low = hf.build_search_body(["x"], 0, 0)
    assert low["days_ago"] == 1 and low["limit"] == 1
    assert not any("title" in k or "exclu" in k for k in body)


def test_hirebase_parse_michigan_entry_chosen_over_first():
    locs = [{"city": "Orlando", "region": "Florida", "country": "United States"},
            {"city": "Holland", "region": "Michigan", "country": "United States"}]
    p = hf.parse_job(make_job(locations=locs))
    assert (p["job_city"], p["job_state"], p["job_country"]) == ("Holland", "MI", "US")


def test_hirebase_parse_orlando_only_is_not_michigan():
    locs = [{"city": "Orlando", "region": "Florida", "country": "United States"}]
    p = hf.parse_job(make_job(locations=locs, location_raw="Holland, MI"))
    assert p["job_city"] == "Orlando" and p["job_state"] != "MI"


def test_hirebase_parse_no_locations_gives_empty():
    p = hf.parse_job(make_job(locations=[]))
    assert (p["job_city"], p["job_state"]) == ("", "")


def test_hirebase_parse_html_stripped_and_job_id_carries_slug():
    p = hf.parse_job(make_job())
    assert p["job_description"] == "Sell & grow Travel"
    assert p["job_id"] == "hirebase_haworth_abc123"
    assert p["job_requisition_id"] == "abc123"
    assert p["employer_name"] == "Haworth Inc."
    assert p["job_posted_at_datetime_utc"] == POSTED


def test_hirebase_parse_malformed_counted_not_raised():
    jobs = [make_job(), make_job(application_link="http://x.com/j"), make_job(platform_job_id=None),
            make_job(job_title=""), "junk", None, make_job(company_slug=5)]
    kept, stats = hf.split_jobs(jobs, "Haworth Inc.", ["haworth"], norm)
    assert stats == {"returned": 7, "malformed": 6, "kept": 1, "unmatched": 0}
    assert len(kept) == 1


def test_hirebase_belongs_rejects_sibling_slug():
    sib = make_job(company_slug="haworth-international", company_name="Haworth International")
    assert not hf.job_belongs_to(sib, "Haworth Inc.", ["haworth-inc", "haworth"], norm)
    kept, stats = hf.split_jobs([sib], "Haworth Inc.", ["haworth"], norm)
    assert kept == [] and stats["unmatched"] == 1


def test_hirebase_belongs_rejects_matching_slug_different_name():
    other = make_job(company_name="Haworth International")
    assert not hf.job_belongs_to(other, "Haworth Inc.", ["haworth"], norm)


def test_hirebase_belongs_accepts_exact():
    assert hf.job_belongs_to(make_job(), "Haworth Inc.", ["haworth-inc", "haworth"], norm)


class _Resp:
    def __init__(self, code=200, data=None, raises=False):
        self.status_code, self._data, self._raises = code, data, raises

    def json(self):
        if self._raises:
            raise ValueError("secret body text")
        return self._data


def _post(resp=None, exc=None):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        if exc:
            raise exc
        return resp
    post.calls = calls
    return post


KEY = "sk-SECRET-123"


def test_hirebase_fetch_no_key_makes_no_call():
    post = _post(_Resp(200, {}))
    assert hf.fetch_company_jobs({}, None, post).status == "unconfigured"
    assert post.calls == []
    assert hf.get_key({"HIREBASE_API_KEY": "  "}) is None
    assert hf.get_key({"HIREBASE_API_KEY": " k "}) == "k"


def test_hirebase_fetch_raising_post_is_errored():
    r = hf.fetch_company_jobs({}, KEY, _post(exc=TimeoutError("boom " + KEY)))
    assert r.status == "errored" and r.error == "post raised TimeoutError"


def test_hirebase_fetch_500_and_429_are_errored_with_code():
    for code in (500, 429):
        r = hf.fetch_company_jobs({}, KEY, _post(_Resp(code, {"jobs": [], "total_count": 0})))
        assert r.status == "errored" and str(code) in r.error and r.jobs == []


def test_hirebase_fetch_bad_bodies_are_errored():
    for data in ([], {"total_count": 0}, {"jobs": "x", "total_count": 0}, {"jobs": []},
                 {"jobs": [], "total_count": "3"}):
        assert hf.fetch_company_jobs({}, KEY, _post(_Resp(200, data))).status == "errored"
    assert hf.fetch_company_jobs({}, KEY, _post(_Resp(200, raises=True))).status == "errored"


def test_hirebase_fetch_all_malformed_jobs_are_errored():
    r = hf.fetch_company_jobs({}, KEY, _post(_Resp(200, {"jobs": [{"x": 1}, "y"], "total_count": 2})))
    assert r.status == "errored" and "malformed" in r.error


def test_hirebase_fetch_empty_jobs_is_clean_zero():
    r = hf.fetch_company_jobs({}, KEY, _post(_Resp(200, {"jobs": [], "total_count": 0})))
    assert (r.status, r.jobs, r.capped) == ("ok", [], False)


def test_hirebase_fetch_total_above_returned_is_capped():
    jobs = [make_job(platform_job_id=str(i)) for i in range(5)]
    r = hf.fetch_company_jobs({}, KEY, _post(_Resp(200, {"jobs": jobs, "total_count": 38})))
    assert r.status == "ok" and r.capped and r.total_count == 38 and len(r.jobs) == 5


def test_hirebase_fetch_key_only_in_header():
    post = _post(_Resp(200, {"jobs": [make_job()], "total_count": 1}))
    r = hf.fetch_company_jobs({"q": 1}, KEY, post)
    assert KEY not in repr(r) and KEY not in post.calls[0]["url"]
    assert post.calls[0]["headers"] == {"x-api-key": KEY}
    bad = hf.fetch_company_jobs({}, KEY, _post(_Resp(200, raises=True)))
    assert KEY not in repr(bad) and "secret body" not in repr(bad)
