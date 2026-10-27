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
