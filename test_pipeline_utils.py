"""Unit tests for pipeline_utils.py: pure scoring/dork/dedup/formatting helpers.
No network dependency - safe to run in any environment.

The outreach-voice tests at the bottom are the exception to "no main.py import": they have to
render copy through the real sanitize_text()/interpolate_template() to be worth anything. They
follow test_main_integration.py's isolation pattern - point JOBS_DB_PATH at a temp file BEFORE
importing main, so main's init_db() never touches the real jobs_cache.db. setdefault() means
whichever test module imports main first owns the temp DB and the other reuses it.
"""
import json
import os
import pytest
import re
import tempfile
import types
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import pipeline_utils as pu

_tmp_db_fd, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_tmp_db_fd)
os.environ.setdefault("JOBS_DB_PATH", _TMP_DB_PATH)

import main as m  # noqa: E402  (must import after JOBS_DB_PATH is set)


# ---- Google dork builders ----

def test_build_hiring_manager_dork_encodes_company_and_targets_ops_titles():
    url = pu.build_hiring_manager_dork("Ann Arbor SPARK, Inc.")
    assert url.startswith("https://www.google.com/search?q=")
    decoded = urllib.parse.unquote(url.split("q=", 1)[1])
    assert "Ann Arbor SPARK" in decoded
    assert "VP of Operations" in decoded
    assert "site:linkedin.com/in" in decoded


def test_dork_builders_strip_legal_entity_suffixes():
    for suffix in ["Inc.", "LLC", "Holdings", "Corp", "Corporation", "Ltd", "PLC", "Group"]:
        decoded_manager = urllib.parse.unquote(pu.build_hiring_manager_dork(f"Acme {suffix}"))
        decoded_recruiter = urllib.parse.unquote(pu.build_recruiter_dork(f"Acme {suffix}"))
        assert suffix.rstrip(".").lower() not in decoded_manager.split('"')[1].lower()
        assert suffix.rstrip(".").lower() not in decoded_recruiter.split('"')[1].lower()
        assert "Acme" in decoded_manager
        assert "Acme" in decoded_recruiter


def test_build_recruiter_dork_targets_talent_acquisition():
    url = pu.build_recruiter_dork("CAPTRUST")
    decoded = urllib.parse.unquote(url)
    assert "Technical Recruiter" in decoded
    assert "Talent Acquisition" in decoded
    assert "CAPTRUST" in decoded


def test_build_alumni_dork_includes_school():
    url = pu.build_alumni_dork("Acme Corp", school="Hope College")
    decoded = urllib.parse.unquote(url)
    assert "Acme Corp" in decoded
    assert "Hope College" in decoded


def test_dork_builders_strip_special_characters():
    url = pu.build_hiring_manager_dork("Acme & Co. (Detroit)!")
    decoded = urllib.parse.unquote(url)
    assert "&" not in decoded.split("site:")[0]  # special chars stripped before querystring encoding


def test_build_linkedin_company_posts_url_slugs_the_company_name():
    assert pu.build_linkedin_company_posts_url("Crain Communications Inc") == (
        "https://www.linkedin.com/company/crain-communications/posts/?feedView=all"
    )
    assert pu.build_linkedin_company_posts_url("Acme & Co. (Detroit)!") == (
        "https://www.linkedin.com/company/acme-detroit/posts/?feedView=all"
    )
    assert pu.build_linkedin_company_posts_url("  Ann   Arbor SPARK, LLC ") == (
        "https://www.linkedin.com/company/ann-arbor-spark/posts/?feedView=all"
    )
    assert pu.build_linkedin_company_posts_url(None).startswith("https://www.linkedin.com/company/")


def test_dork_builders_handle_missing_company():
    # Should not raise on None/empty input
    assert pu.build_hiring_manager_dork(None).startswith("https://www.google.com/search?q=")
    assert pu.build_recruiter_dork("").startswith("https://www.google.com/search?q=")


# ---- Priority normalization (Dynamic Contact Quality Multiplier) ----

def test_normalize_priority_value_text_tiers():
    assert pu.normalize_priority_value("High") == 9
    assert pu.normalize_priority_value("Medium priority") == 5
    assert pu.normalize_priority_value("low") == 2


def test_normalize_priority_value_numeric_scale_is_clamped():
    assert pu.normalize_priority_value("10") == 10
    assert pu.normalize_priority_value("1") == 1
    assert pu.normalize_priority_value("999") == 10  # clamped to max
    assert pu.normalize_priority_value("0") == 1  # clamped to min (re.search on "0" matches "0" -> clamped to 1)


def test_normalize_priority_value_defaults_when_ambiguous():
    assert pu.normalize_priority_value("") == 5
    assert pu.normalize_priority_value(None) == 5
    assert pu.normalize_priority_value("n/a") == 5


def test_score_boost_formula_caps_at_thirty():
    for priority_score in (1, 5, 9, 10, 15):
        boost = min(30, priority_score * 3)
        assert boost <= 30
    assert min(30, 10 * 3) == 30
    assert min(30, 5 * 3) == 15


# ---- Follow-up interval decay ----

def test_calculate_followup_interval_higher_priority_means_sooner_followup():
    soon = pu.calculate_followup_interval(9)
    later = pu.calculate_followup_interval(2)
    assert soon < later
    assert soon >= 3  # floor enforced


def test_calculate_followup_interval_invalid_input_falls_back():
    assert pu.calculate_followup_interval("not-a-number") == 14


# ---- Follow-up sequencer policy (pure) ----

_TODAY = date(2026, 6, 1)


def _added(days_ago):
    """Date Added string `days_ago` days before _TODAY."""
    return (_TODAY - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_followup_action_applied_boundary_around_the_bump():
    """The day before the bump is silence; the bump day and the day after both issue it.

    Written against FOLLOWUP_1_DAYS rather than a literal, so retuning the cadence does not
    require editing the boundary logic - only the values below that are deliberately absolute.
    """
    assert pu.followup_action("Applied", _added(pu.FOLLOWUP_1_DAYS - 1), "", _TODAY) == "none"
    assert pu.followup_action("Applied", _added(pu.FOLLOWUP_1_DAYS), "", _TODAY) == "send_followup_1"
    assert pu.followup_action("Applied", _added(pu.FOLLOWUP_1_DAYS + 1), "", _TODAY) == "send_followup_1"


def test_the_bump_waits_ten_days_and_the_bury_waits_three_weeks():
    """The retuned cadence, pinned to absolute days on purpose.

    The old numbers (bump at 2, bury at 14) fired a follow-up roughly 48 hours after the first
    email - before a busy recipient had plausibly acted on it. Kevin retuned this on 2026-09-24
    to one bump at day 10 and a 21-day total lifespan. These literals are the contract; if a
    future change moves them, that should be a decision, not a silent drift.
    """
    assert pu.FOLLOWUP_1_DAYS == 10
    assert pu.FOLLOWUP_BURY_DAYS == 21
    # Silent through the first nine days - no touch at all.
    for day in (1, 5, 9):
        assert pu.followup_action("Applied", _added(day), "", _TODAY) == "none", day
    # One bump, then it stands until the bury boundary.
    for day in (10, 15, 20):
        assert pu.followup_action("Applied", _added(day), "", _TODAY) == "send_followup_1", day
    for day in (21, 30):
        assert pu.followup_action("Applied", _added(day), "", _TODAY) == "bury_ghosted", day


def test_followup_action_never_issues_a_second_bump():
    """One follow-up, then the bury. No day may produce send_followup_2 - the rung was cut."""
    for day in range(0, 40):
        assert pu.followup_action("Applied", _added(day), "", _TODAY) != "send_followup_2"


def test_followup_action_future_next_followup_always_none():
    future = (_TODAY + timedelta(days=1)).strftime("%Y-%m-%d")
    assert pu.followup_action("Applied", _added(30), future, _TODAY) == "none"
    assert pu.followup_action("Interviewing", _added(30), future, _TODAY) == "none"


def test_followup_action_next_followup_today_is_not_future():
    # Due today (== today, not > today) -> the window math still applies.
    assert pu.followup_action(
        "Applied", _added(pu.FOLLOWUP_BURY_DAYS), _TODAY.strftime("%Y-%m-%d"), _TODAY
    ) == "bury_ghosted"


def test_followup_action_hot_statuses_stale_nudge_after_five_days():
    for status in ("Replied", "Screening", "Interviewing"):
        assert pu.followup_action(status, _added(5), "", _TODAY) == "none"
        assert pu.followup_action(status, _added(6), "", _TODAY) == "stale_nudge"
        # Hot statuses never auto-bury, however old.
        assert pu.followup_action(status, _added(90), "", _TODAY) == "stale_nudge"


def test_followup_action_terminal_and_matched_statuses_are_none():
    for status in ("Matched", "Offer", "Rejected"):
        assert pu.followup_action(status, _added(90), "", _TODAY) == "none"


def test_followup_action_unknown_status_is_none():
    for status in ("Ghosted", "", None, "pending review", "APPLIED?"):
        assert pu.followup_action(status, _added(90), "", _TODAY) == "none"


def test_followup_action_is_status_case_insensitive():
    assert pu.followup_action("  applied  ", _added(pu.FOLLOWUP_1_DAYS), "", _TODAY) == "send_followup_1"


def test_followup_action_blank_dates_yield_none():
    assert pu.followup_action("Applied", "", "", _TODAY) == "none"
    assert pu.followup_action("Applied", None, None, _TODAY) == "none"
    assert pu.followup_action("Applied", "1970-01-01", "1970-01-01", _TODAY) == "none"


def test_followup_action_malformed_dates_yield_none():
    assert pu.followup_action("Applied", "not-a-date", "", _TODAY) == "none"
    assert pu.followup_action("Applied", "2026-13-99", "garbage", _TODAY) == "none"


def test_followup_action_falls_back_to_next_followup_when_date_added_blank():
    # Date Added missing, past Next Followup Date -> used as the anchor.
    assert pu.followup_action("Applied", "", _added(pu.FOLLOWUP_BURY_DAYS + 2), _TODAY) == "bury_ghosted"


def test_followup_action_accepts_datetime_for_today():
    assert pu.followup_action(
        "Applied", _added(pu.FOLLOWUP_1_DAYS), "", datetime(2026, 6, 1, 7, 30)
    ) == "send_followup_1"


def test_followup_anchor_prefers_date_added_over_next_followup():
    assert pu.followup_anchor("2026-05-01", "2026-05-20") == date(2026, 5, 1)
    assert pu.followup_anchor("", "2026-05-20") == date(2026, 5, 20)
    assert pu.followup_anchor("1970-01-01", "") is None


def test_followup_cadence_knobs_are_strictly_increasing():
    # The nightly job pushes Next Followup Date to the next boundary; out-of-order knobs
    # would skip or repeat a step. STALE_HOT_DAYS is independent.
    assert 0 < pu.FOLLOWUP_1_DAYS < pu.FOLLOWUP_2_DAYS < pu.FOLLOWUP_BURY_DAYS
    assert pu.STALE_HOT_DAYS > 0


# ---- Smart tab routing ----

def test_resolve_smart_target_tab_carmen_family():
    assert pu.resolve_smart_target_tab("Carmen Cold", "warm") == "Carmen Warm"
    assert pu.resolve_smart_target_tab("Carmen Warm", "kill") == "Killed"
    assert pu.resolve_smart_target_tab("Carmen Warm", "cold") == "Carmen Cold"


def test_resolve_smart_target_tab_tetiana_and_staging_family():
    assert pu.resolve_smart_target_tab("Tetiana Cold", "warm") == "Tetiana Warm"
    assert pu.resolve_smart_target_tab("Pipeline_Candidates", "kill") == "Died"
    assert pu.resolve_smart_target_tab("Pipeline_Candidates", "cold") == "Tetiana Cold"


# ---- Sentence limiter & fit indicator ----

def test_enforce_sentence_limit_truncates():
    text = "First sentence. Second sentence! Third sentence? Fourth."
    assert pu.enforce_sentence_limit(text, 2) == "First sentence. Second sentence!"


def test_get_fit_score_indicator_thresholds():
    assert pu.get_fit_score_indicator(95) == "🟢"
    assert pu.get_fit_score_indicator(80) == "🟢"
    assert pu.get_fit_score_indicator(70) == "🟡"
    assert pu.get_fit_score_indicator(64) == "🔴"


# ---- Dedup / short key hashing ----

def test_generate_dedup_hash_is_case_and_whitespace_insensitive():
    a = pu.generate_dedup_hash("Acme Corp", "Operations Manager")
    b = pu.generate_dedup_hash(" acme corp ", " OPERATIONS MANAGER ")
    assert a == b


def test_generate_dedup_hash_differs_for_different_jobs():
    a = pu.generate_dedup_hash("Acme Corp", "Operations Manager")
    b = pu.generate_dedup_hash("Acme Corp", "Data Analyst")
    assert a != b


def test_generate_dedup_hash_is_legal_suffix_insensitive():
    base = pu.generate_dedup_hash("Acme Corp", "Operations Manager")
    for suffix in ["Inc.", "LLC", "Holdings", "Corp", "Corporation", "Ltd", "PLC", "Group"]:
        assert pu.generate_dedup_hash(f"Acme {suffix}", "Operations Manager") == base
    assert pu.generate_dedup_hash("Acme", "Operations Manager") == base


def test_normalize_dedup_key_is_case_insensitive():
    assert pu.normalize_dedup_key("Aptiv", "Ops Analyst") == pu.normalize_dedup_key("APTIV", "ops ANALYST")


def test_normalize_dedup_key_strips_punctuation():
    assert pu.normalize_dedup_key("AAA, Inc.", "Operations - Manager") == \
           pu.normalize_dedup_key("AAA Inc", "Operations Manager")


def test_normalize_dedup_key_drops_common_suffixes_and_filler():
    base = pu.normalize_dedup_key("Blue Chip", "Analyst")
    assert pu.normalize_dedup_key("The Blue Chip Co", "Analyst") == base
    assert pu.normalize_dedup_key("Blue Chip LLC", "Analyst") == base
    assert pu.normalize_dedup_key("Blue Chip Corp.", "Analyst") == base


def test_normalize_dedup_key_collapses_internal_whitespace():
    assert pu.normalize_dedup_key("  Aptiv   PLC ", "  Senior   Ops  Analyst ") == \
           pu.normalize_dedup_key("Aptiv PLC", "Senior Ops Analyst")


def test_normalize_dedup_key_handles_empty_and_none():
    assert pu.normalize_dedup_key("", "") == "|"
    assert pu.normalize_dedup_key(None, None) == "|"
    assert pu.normalize_dedup_key("Aptiv", None) == "aptiv|"
    assert pu.normalize_dedup_key(None, "Analyst") == "|analyst"


def test_normalize_dedup_key_distinguishes_different_roles_same_company():
    assert pu.normalize_dedup_key("Aptiv", "Ops Analyst") != pu.normalize_dedup_key("Aptiv", "Data Analyst")


def test_status_rank_every_canonical_value_is_ordered():
    assert [pu.status_rank(v) for v in pu.STATUS_VOCAB] == list(range(len(pu.STATUS_VOCAB)))
    assert pu.status_rank("Matched") == 0
    assert pu.status_rank("Rejected") == len(pu.STATUS_VOCAB) - 1


def test_status_rank_is_case_insensitive_and_trims_whitespace():
    assert pu.status_rank("  interviewing  ") == pu.STATUS_VOCAB.index("Interviewing")
    assert pu.status_rank("ApPlIeD") == pu.STATUS_VOCAB.index("Applied")


def test_status_rank_unknown_and_none_return_minus_one():
    assert pu.status_rank("Ghosted") == -1
    assert pu.status_rank("") == -1
    assert pu.status_rank(None) == -1


def test_generate_short_key_deterministic_for_same_raw_id():
    assert pu.generate_short_key("job_123") == pu.generate_short_key("job_123")
    assert len(pu.generate_short_key("job_123")) == 12


def test_generate_short_key_uses_fallback_when_raw_id_missing():
    assert pu.generate_short_key(None, fallback="entropy-value") == pu.generate_short_key(None, fallback="entropy-value")
    assert pu.generate_short_key(None, fallback="a") != pu.generate_short_key(None, fallback="b")


# ---- Posted-hours parsing & age badge ----

def test_parse_posted_hours_recent_iso_timestamp():
    recent = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    hours = pu.parse_posted_hours(recent)
    assert 4 <= hours <= 6


def test_parse_posted_hours_missing_or_bad_input_defaults_to_48():
    assert pu.parse_posted_hours(None) == 48
    assert pu.parse_posted_hours("not-a-date") == 48


def test_get_age_badge_buckets():
    assert "FRESH" in pu.get_age_badge(1)
    assert "RECENT" in pu.get_age_badge(48)
    assert "ACTIVE" in pu.get_age_badge(100)
    assert "AGING" in pu.get_age_badge(200)
    assert "STALE" in pu.get_age_badge(400)


# ---- Salary & work style extraction ----

def test_extract_salary_annualizes_hourly_rate():
    job = {"job_min_salary": 20, "job_max_salary": 25, "job_salary_period": "hour"}
    salary_str, max_sal = pu.extract_salary(job)
    assert max_sal == 25 * 2080
    assert "/year" in salary_str


def test_extract_salary_handles_missing_data():
    salary_str, max_sal = pu.extract_salary({})
    assert salary_str == "Salary Unlisted"
    assert max_sal == 0


def test_extract_work_style_detects_remote_and_hybrid():
    assert pu.extract_work_style({"job_is_remote": True, "job_description": ""}) == "Remote"
    assert pu.extract_work_style({"job_description": "This is a hybrid role"}) == "Hybrid"
    assert pu.extract_work_style({"job_description": "on-site only"}) == "On-Site / Unspecified"


# ---- Description simhash (dedup fingerprint) ----

def test_compute_description_simhash_stable_for_identical_text():
    text = "Manage operations workflows across custodial platforms."
    assert pu.compute_description_simhash(text) == pu.compute_description_simhash(text)


def test_compute_description_simhash_empty_text_does_not_raise():
    assert pu.compute_description_simhash("") == pu.compute_description_simhash(None)


def test_compute_description_simhash_returns_blank_for_unidentifiable_text():
    """Empty/short descriptions must yield "" (no signature), NOT a real hash.

    Regression guard for the bug that buried hundreds of roles: every description-less posting
    used to hash to the empty-string MD5, so the first one saved that token and every later one
    collided with it and was dropped permanently. Greenhouse returned no description at all
    before the content=true fix, so one poisoned hash could bury unbounded unrelated jobs.
    """
    assert pu.compute_description_simhash("") == ""
    assert pu.compute_description_simhash(None) == ""
    assert pu.compute_description_simhash("   ") == ""
    assert pu.compute_description_simhash("Operations analyst role") == ""


def test_compute_description_simhash_distinguishes_real_postings():
    """Two unrelated real postings must not collide - the dedup is only safe above the
    MIN_SIMHASH_TOKENS floor, and must still do its actual job there."""
    a = ("We are seeking an operations analyst to manage reconciliation workflows "
         "using SQL and Salesforce across our wealth platform.")
    b = ("The healthcare operations specialist will drive process improvement across "
         "intake, billing and claims using Excel and internal tooling.")
    hash_a, hash_b = pu.compute_description_simhash(a), pu.compute_description_simhash(b)
    assert hash_a and hash_b
    assert hash_a != hash_b
    assert hash_a == pu.compute_description_simhash(a)


# ---- Email waterfall (network calls mocked/disabled) ----

def test_resolve_email_waterfall_falls_back_without_api_keys(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("PROSPEO_API_KEY", raising=False)
    monkeypatch.delenv("GETPROSPECT_API_KEY", raising=False)
    result = pu.resolve_email_waterfall("Jane Doe", "Acme Corp")
    assert result == "jane.doe@acmecorp.com [⚠️ Unverified]"


def test_resolve_email_waterfall_uses_domain_hint_when_provided(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("PROSPEO_API_KEY", raising=False)
    monkeypatch.delenv("GETPROSPECT_API_KEY", raising=False)
    result = pu.resolve_email_waterfall("Jane Doe", "Acme Corp", domain_hint="acme.io")
    assert result == "jane.doe@acme.io [⚠️ Unverified]"


def test_resolve_email_waterfall_single_name_uses_operations_fallback(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("PROSPEO_API_KEY", raising=False)
    monkeypatch.delenv("GETPROSPECT_API_KEY", raising=False)
    result = pu.resolve_email_waterfall("Cher", "Acme Corp")
    assert result == "operations@acmecorp.com [⚠️ Fallback]"


def test_resolve_email_waterfall_uses_hunter_when_configured(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")

    class FakeResponse:
        def json(self):
            return {"data": {"email": "jane@acmecorp.com"}}

    monkeypatch.setattr(pu.requests, "get", lambda *a, **k: FakeResponse())
    result = pu.resolve_email_waterfall("Jane Doe", "Acme Corp")
    assert result == "jane@acmecorp.com"


def test_resolve_email_waterfall_fires_on_provider_attempt_callback(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")

    class FakeResponse:
        def json(self):
            return {"data": {"email": "jane@acmecorp.com"}}

    monkeypatch.setattr(pu.requests, "get", lambda *a, **k: FakeResponse())
    attempts = []
    pu.resolve_email_waterfall("Jane Doe", "Acme Corp", on_provider_attempt=attempts.append)
    assert attempts == ["hunter"]


def test_resolve_email_waterfall_falls_through_to_prospeo(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    monkeypatch.setenv("PROSPEO_API_KEY", "test-key-2")
    monkeypatch.delenv("GETPROSPECT_API_KEY", raising=False)

    class FakeHunterResponse:
        def json(self):
            return {"data": {}}  # no email found

    class FakeProspeoResponse:
        def json(self):
            return {"response": {"email": "jane@acmecorp.com"}}

    monkeypatch.setattr(pu.requests, "get", lambda *a, **k: FakeHunterResponse())
    monkeypatch.setattr(pu.requests, "post", lambda *a, **k: FakeProspeoResponse())
    result = pu.resolve_email_waterfall("Jane Doe", "Acme Corp")
    assert result == "jane@acmecorp.com"


def test_resolve_email_waterfall_falls_through_to_getprospect(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    monkeypatch.setenv("PROSPEO_API_KEY", "test-key-2")
    monkeypatch.setenv("GETPROSPECT_API_KEY", "test-key-3")

    class FakeHunterResponse:
        def json(self):
            return {"data": {}}  # no email found

    class FakeProspeoResponse:
        def json(self):
            return {"response": {}}  # no email found

    class FakeGetProspectResponse:
        def json(self):
            return {"email": "jane@acmecorp.com"}

    def fake_get(url, *a, **k):
        return FakeGetProspectResponse() if "getprospect" in url else FakeHunterResponse()

    monkeypatch.setattr(pu.requests, "get", fake_get)
    monkeypatch.setattr(pu.requests, "post", lambda *a, **k: FakeProspeoResponse())
    result = pu.resolve_email_waterfall("Jane Doe", "Acme Corp")
    assert result == "jane@acmecorp.com"


# ---- Job source attribution ----

def test_derive_job_source_recognizes_ats_prefixes():
    assert pu.derive_job_source("gh_acme_123") == "greenhouse"
    assert pu.derive_job_source("lever_acme_123") == "lever"
    assert pu.derive_job_source("ashby_acme_123") == "ashby"
    assert pu.derive_job_source("ingest_abc123") == "manual_ingest"


def test_derive_job_source_defaults_to_jsearch():
    assert pu.derive_job_source("some-random-jsearch-id") == "jsearch"
    assert pu.derive_job_source(None) == "jsearch"


def test_jsearch_source_carries_the_publisher_so_decoys_can_name_it():
    """"jsearch" was one bucket holding every aggregator JSearch syndicates, so /decoys could only
    report a blended rate for the whole feed - the actual question (WHICH publisher serves dead
    inventory) was unanswerable."""
    assert pu.derive_job_source("x", "https://www.learn4good.com/jobs/a/1/e/") == "jsearch:learn4good.com"
    assert pu.derive_job_source("x", "https://www.jobleads.com/us/job/abc") == "jsearch:jobleads.com"
    # www. is stripped so one publisher is not split across two buckets.
    assert pu.derive_job_source("x", "https://learn4good.com/a") == "jsearch:learn4good.com"
    # An ATS prefix still wins - those are not JSearch results at all.
    assert pu.derive_job_source("gh_acme", "https://www.learn4good.com/a") == "greenhouse"
    # No link is still the bare bucket, so nothing regresses.
    assert pu.derive_job_source("x", "") == "jsearch"


def test_job_link_host_normalizes_and_survives_junk():
    assert pu.job_link_host("https://WWW.Learn4Good.com/jobs/x?a=1") == "learn4good.com"
    assert pu.job_link_host("http://jobs.learn4good.com:443/x") == "jobs.learn4good.com"
    assert pu.job_link_host("") == "" and pu.job_link_host(None) == ""
    assert pu.derive_job_source("") == "jsearch"


# ---- Email confidence gating ----

def test_is_unverified_email_detects_warning_tags():
    assert pu.is_unverified_email("jane.doe@acmecorp.com [⚠️ Unverified]") is True
    assert pu.is_unverified_email("operations@acmecorp.com [⚠️ Fallback]") is True


def test_is_unverified_email_false_for_clean_address():
    assert pu.is_unverified_email("jane@acmecorp.com") is False
    assert pu.is_unverified_email("") is False
    assert pu.is_unverified_email(None) is False


# ---- Outreach voice linter ----

def test_lint_outreach_template_flags_the_phrases_that_caused_the_rewrite():
    stiff = ("Hi there,\n\nI saw the role and wanted to discuss alignment. I hope you have been "
             "doing well. Would you be open to a quick chat?\n\nBest regards,\nKevin Miller")
    violations = " | ".join(pu.lint_outreach_template(stiff, "email"))
    assert "Best regards" in violations
    assert "alignment" in violations
    assert "Hi there" in violations
    assert "quick chat" in violations
    # ...and the stiffness is only advisory - it never fails a template on its own.
    assert "no contractions" not in violations
    assert any("no contractions" in n for n in pu.advise_outreach_template(stiff, "email"))


def test_lint_outreach_template_flags_punctuation_sanitize_text_would_delete():
    # sanitize_text() DELETES these rather than rewriting around them, so "Hi Dana - saw the
    # role" silently ships as "Hi Dana saw the role". Catch them in the raw template.
    assert any("em/en-dash" in v for v in pu.lint_outreach_template("Hi Dana — I've seen it.", "email"))
    assert any("colon" in v for v in pu.lint_outreach_template("Here's the thing: I've seen it.", "email"))
    assert any("semicolon" in v for v in pu.lint_outreach_template("I've seen it; you have not.", "email"))
    assert any("exclamation" in v for v in pu.lint_outreach_template("I've seen it!", "email"))


def test_lint_outreach_template_enforces_length_caps_per_kind():
    long_email = "I've " + ("word " * pu.OUTREACH_EMAIL_WORD_CAP)
    assert any(f"over the {pu.OUTREACH_EMAIL_WORD_CAP}-word" in v for v in pu.lint_outreach_template(long_email, "email"))
    long_note = "I've " + ("x" * pu.OUTREACH_LINKEDIN_CHAR_CAP)
    assert any("over the 220-char" in v for v in pu.lint_outreach_template(long_note, "linkedin"))
    # ...and the caps do not cross over: a 100-word email-length string is fine as an email
    # only under the cap, and a short note is clean either way.
    assert pu.lint_outreach_template("Hi. Saw the role and I'd like to connect.", "linkedin") == []


def test_contraction_advice_is_advisory_and_ignores_possessives():
    # "team's" is a possessive, not a contraction - it must not satisfy the rule...
    assert pu.advise_outreach_template("Saw the role on my team's board.", "email") != []
    assert pu.advise_outreach_template("Saw the role. I'd like to connect.", "email") == []
    # ...but either way it stays out of the hard violations, so a contraction-free template
    # that is otherwise clean still ships. cold_ops[2] and followup_bumps[0] are exactly that.
    assert pu.lint_outreach_template("Saw the role on my team's board.", "email") == []
    assert pu.lint_outreach_template("Saw the role. I'd like to connect.", "email") == []


def test_lint_outreach_template_flags_space_before_name_placeholder():
    # interpolate_template() supplies {name}'s own leading space; "Hi {name}," would double it.
    assert any("space before {name}" in v for v in pu.lint_outreach_template("Hi {name}, I've seen it.", "email"))


def test_lint_allows_a_ten_minute_ask_but_still_flags_quick_chat():
    """Reverted after the wrong-mailbox pass: the correct corpus (kjmiller406@gmail.com) has
    '10 minutes' x3 and zero odd-minute asks, so a round timebox is the voice. 'quick chat'
    has zero uses and stays banned."""
    assert pu.lint_outreach_template("Do you have 10 minutes for a brief call?", "email") == []
    assert pu.lint_outreach_template("Do you have 15 minutes for a brief call?", "email") == []
    assert pu.lint_outreach_template("Do you have 13 minutes for a call?", "email") == []
    assert any("quick chat" in v for v in pu.lint_outreach_template("Open to a quick chat this week?", "email"))


def test_roleless_followup_bumps_pass_the_voice_linter():
    """PEOPLE-schema (Carmen Cold) bump copy is held to the same hard rules as the JSON banks,
    raw and sanitized, since it is authored in main.py rather than a template file."""
    failures = []
    for idx, template in enumerate(m._ROLELESS_FOLLOWUP_BUMPS):
        for name in ("", "Dana"):
            rendered = m.interpolate_template(template, name=name, company=_LINT_COMPANY)
            for stage, text in (("raw", rendered), ("sanitized", m.sanitize_text(rendered))):
                for violation in pu.lint_outreach_template(text, "email"):
                    failures.append(f"_ROLELESS_FOLLOWUP_BUMPS[{idx}] ({stage}, name={name!r}): {violation}")
    assert failures == []


def test_outreach_track_follows_the_jobs_people_schema_split():
    """A title is present exactly when the row came from a JOBS tab, where Column C is the role
    Kevin APPLIED TO (Code.gs:495). PEOPLE tabs hardcode "" because they have no role column. So
    presence of a title - not its wording - is the recruiter/peer signal.

    The wording cases are pinned because a keyword heuristic over this field is the obvious wrong
    turn: it never holds the CONTACT's job title, so "Technical Recruiter" here is a REQ for a
    recruiting position, which is still the recruiter track for the ordinary reason (Kevin applied
    to it), and "Business Data Analyst" is not the peer track despite naming no recruiter.
    """
    assert pu.classify_outreach_track("Business Data Analyst") == "recruiter"
    assert pu.classify_outreach_track("Technical Recruiter") == "recruiter"
    for blank in ("", "   ", None):
        assert pu.classify_outreach_track(blank) == "peer"


def test_followup_bump_copy_is_exact_for_both_tracks():
    """The shipped follow-up wording, asserted verbatim. Driven through build_followup_bump_draft
    (the real sequencer entry point), not the template constants, so a break in the bank, the
    interpolation, the track lookup or the greeting all surface here.

    The two tracks differ in ONE sentence: a recruiter owns a req Kevin applied to, so "still
    interested" is accurate; a peer does not, and claiming interest in a role they have no say over
    is what makes a follow-up read as a bot working a list.
    """
    recruiter = m.build_followup_bump_draft(
        {"name": "Kimberly Haller", "company": "Trinity Health MI", "title": "Business Data Analyst"}, 1)
    assert recruiter == (
        "Hi Kimberly,\n\n"
        "I'm just circling back on my earlier note about the Business Data Analyst role at Trinity Health MI.\n\n"
        "I am still interested, and I'm happy to answer anything helpful.\n\n"
        "Best,\nKevin"
    )

    peer = m.build_followup_bump_draft(
        {"name": "Chaunta Marshall", "company": "Trinity Health MI", "title": ""}, 1)
    assert peer == (
        "Hi Chaunta,\n\n"
        "I'm just circling back on my earlier note to Trinity Health MI.\n\n"
        "I would still like to connect if you have a moment, and I'm happy to answer anything helpful.\n\n"
        "Best,\nKevin"
    )


def test_both_bump_paths_agree_on_the_track_sentence():
    """generate_bump_email() and build_followup_bump_draft() feed the same bank. The bank's
    {track_sentence} defaults to PEER when unfilled, so a path that forgot to pass the track would
    silently send the peer line to a recruiter - a drift that renders as valid English and would
    never fail a smoke test."""
    for title in ("Business Data Analyst", ""):
        via_draft = m.build_followup_bump_draft({"name": "Dana Reyes", "company": "Nliven", "title": title}, 1)
        via_email = m.generate_bump_email(contact_name="Dana Reyes", job_title=title,
                                          company_name="Nliven", template_id=1)
        assert via_draft == via_email, f"bump paths disagree for title={title!r}"


def test_an_unfilled_track_sentence_never_leaves_braces_in_an_email():
    """interpolate_template() returns the RAW template on a KeyError, which would put literal
    braces in a candidate-facing email. The slot must therefore have a real default."""
    for template in _load_bank("outreach_templates.json")["followup_bumps"]:
        rendered = m.interpolate_template(template, name="Dana", company=_LINT_COMPANY,
                                          job_title=_LINT_TITLE)
        assert "{" not in rendered and "}" not in rendered
        assert "I would still like to connect" in rendered  # the peer default


def test_warm_alumni_entries_are_unsendable_scaffolds():
    """Warm outreach is hand-written now. Each warm_alumni entry must be an obviously-unfinished
    skeleton (explicit bracketed blanks) so nothing generic can be fired off by /warm, yet still
    pass the voice linter and keep the 6-entry addressing contract (W0-W5)."""
    warm = _load_bank("outreach_templates.json")["warm_alumni"]
    assert len(warm) == 6
    for idx, template in enumerate(warm):
        rendered = m.interpolate_template(template, name="", company=_LINT_COMPANY)
        assert rendered.count("[") >= 3 and rendered.count("]") >= 3, f"warm_alumni[{idx}] has no blanks"
        assert pu.lint_outreach_template(rendered, "email") == [], f"warm_alumni[{idx}] fails lint"
        assert pu.lint_outreach_template(m.sanitize_text(rendered), "email") == []


def test_reactivation_entries_are_built_around_one_banked_gap_block():
    """Reactivation copy (dormant Carmen Warm contacts). Its middle paragraph is BANKED - the
    Signal ending is the same fact for every recipient, so it is written once and must be
    byte-identical across all four slots. If an edit drifts one copy, Kevin tells two versions of
    one story to people who may compare notes."""
    pool = _load_bank("outreach_templates.json")["reactivation"]
    assert len(pool) == 4, "reactivation is a /edit addressing contract (R0-R3)"

    banked = "I was at Signal through the summer."
    for idx, template in enumerate(pool):
        rendered = m.interpolate_template(template, name="", company=_LINT_COMPANY,
                                          job_title=_LINT_TITLE)
        assert banked in rendered, f"reactivation[{idx}] lost the banked gap block"
        assert pu.lint_outreach_template(rendered, "email") == [], f"reactivation[{idx}] fails lint"
        assert pu.lint_outreach_template(m.sanitize_text(rendered), "email") == []

    # The banked paragraph itself, identical everywhere. Compare the full sentence run, not just
    # the opener, so a reworded middle clause fails loudly instead of drifting.
    blocks = []
    for template in pool:
        start = template.index(banked)
        blocks.append(template[start:template.index("\n\n", start)])
    assert len(set(blocks)) == 1, f"the banked gap block drifted across slots: {set(blocks)}"


def test_reactivation_splits_into_banked_ask_and_open_ask_halves():
    """Kevin's bench splits in two (confirmed 2026-09-23). About half have a live posting, so the
    ask is always the same and is BANKED - R0/R1 are sendable with only the greeting and title
    filled in. The other half have no posting and the ask depends on the person, so R2/R3 keep it
    bracketed. A blank left in R0/R1 means the banked half silently became hand-work again."""
    pool = _load_bank("outreach_templates.json")["reactivation"]

    for idx in (0, 1):
        rendered = m.interpolate_template(pool[idx], name="Dana", company=_LINT_COMPANY,
                                          job_title=_LINT_TITLE)
        assert "Who owns that req on your side?" in rendered, f"R{idx} lost the banked ask"
        assert "[" not in rendered.split("\n\n")[-3], f"R{idx} has a blank in its ask paragraph"

    for idx in (2, 3):
        rendered = m.interpolate_template(pool[idx], name="Dana", company=_LINT_COMPANY,
                                          job_title=_LINT_TITLE)
        assert rendered.count("[") >= 2 and rendered.count("]") >= 2, \
            f"R{idx} is the open-ask half and must stay unsendable until Kevin fills it"

    # R0 is the one fully-banked slot: promise + gap + ask, no blanks anywhere. It is the default
    # for the posting half and must be sendable as-is.
    r0 = m.interpolate_template(pool[0], name="Dana", company=_LINT_COMPANY, job_title=_LINT_TITLE)
    assert "[" not in r0 and "]" not in r0, "R0 must be fully banked - no blanks left to fill"


def test_reactivation_banked_ask_survives_a_garbage_job_title():
    """Board titles carry garbage ("... /work from home reputed company/"), and a long one
    interpolated into R0/R1 could push the email over the word cap. sanitize_job_title() runs
    first on the real send path; this pins that the banked half still lints after it."""
    garbage = "Financial Operations Analyst Intermediate /work from home reputed company/"
    pool = _load_bank("outreach_templates.json")["reactivation"]
    for idx in (0, 1):
        rendered = m.interpolate_template(pool[idx], name="Dana", company=_LINT_COMPANY,
                                          job_title=pu.sanitize_job_title(garbage))
        assert pu.lint_outreach_template(rendered, "email") == [], \
            f"R{idx} busts the cap on a real sanitized board title"


# ---- One voice, both paths: the real banks and the real generators ----

_LINT_COMPANY = "Atwell"
_LINT_TITLE = "Technology Business Operations Specialist"


def _load_bank(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_template_banks_have_the_exact_lengths_gemini_routes_against():
    # Gemini routes by integer index and /edit addresses by position; a resize silently
    # corrupts routing (a bad index falls back to 0, so every email becomes identical).
    # These counts are mirrored in response_schema.py (le=9, le=7) and main.build_system_prompt().
    outreach = _load_bank("outreach_templates.json")
    linkedin = _load_bank("linkedin_templates.json")
    assert len(outreach["cold_ops"]) == 8
    # warm_alumni is the one pool Gemini does NOT route into: outreach_template_id is le=7 and
    # selects cold_ops, while generate_warm_email takes an explicit template_id (default 0). So
    # this count is a /edit addressing contract (W0-W5), not a routing one, and growing the pool
    # is safe. Six entries so the highest-converting path has real variety in the ask.
    assert len(outreach["warm_alumni"]) == 6
    assert len(outreach["followup_bumps"]) == 2
    assert len(linkedin["linkedin_templates"]) == 10


def test_every_shipped_template_passes_the_voice_linter():
    """Interpolate every entry in both real banks, sanitize it the way a send would, and demand
    zero hard violations - banned phrases, punctuation sanitize_text() eats, {name} spacing, the
    length caps. Style advice (advise_outreach_template) is deliberately not asserted on: it is a
    nudge, and good copy is allowed to ignore it. Lints the raw interpolation too, because
    sanitize_text() would have already swallowed any em-dash/colon/semicolon by the time the
    sanitized string is inspected."""
    banks = [
        ("cold_ops", "email", _load_bank("outreach_templates.json")["cold_ops"]),
        ("warm_alumni", "email", _load_bank("outreach_templates.json")["warm_alumni"]),
        ("followup_bumps", "email", _load_bank("outreach_templates.json")["followup_bumps"]),
        ("recruiter", "email", _load_bank("outreach_templates.json")["recruiter"]),
        ("reactivation", "email", _load_bank("outreach_templates.json")["reactivation"]),
        ("linkedin_templates", "linkedin", _load_bank("linkedin_templates.json")["linkedin_templates"]),
    ]
    failures = []
    for pool_key, kind, pool in banks:
        for idx, template in enumerate(pool):
            rendered = m.interpolate_template(template, name="", company=_LINT_COMPANY, job_title=_LINT_TITLE)
            for stage, text in (("raw", rendered), ("sanitized", m.sanitize_text(rendered))):
                for violation in pu.lint_outreach_template(text, kind):
                    failures.append(f"{pool_key}[{idx}] ({stage}): {violation}")
    assert failures == []


def test_shipped_templates_open_on_a_bare_hi_when_no_name_is_known():
    # The card never knows the recipient's name, so the old "Hi there," default is gone. The
    # forensic analysis of the correct mailbox (kjmiller406@gmail.com) shows name-alone openers
    # are a warm marker for people already spoken to, not a cold voice, so {name_bare} is retired:
    # every cold template opens "Hi{name}," and must degrade to a bare "Hi," with no contact name
    # and render "Hi Dana," (never "Dana,") when one is known.
    for template in _load_bank("outreach_templates.json")["cold_ops"]:
        no_name = m.interpolate_template(template, name="", company=_LINT_COMPANY, job_title=_LINT_TITLE)
        with_name = m.interpolate_template(template, name="Dana", company=_LINT_COMPANY, job_title=_LINT_TITLE)
        assert no_name.startswith("Hi,")
        assert with_name.startswith("Hi Dana,")


def test_cold_ops_encodes_the_professional_corpus_voice():
    """Rules traceable to counts in the correct mailbox (kjmiller406@gmail.com, 62 emails):
    a 15-minute timebox, 'Best,' + 'Kevin', and no college-corpus habits.

    Retuned 2026-09-24 to the template Kevin wrote by hand and chose as the new standard. Three
    rules moved with it, each a deliberate override of what the 62-email corpus showed:
      * 15 minutes, not 10. A bigger ask, made once.
      * Signs 'Kevin', not 'Kevin Miller'.
      * "I saw you were hiring for this role" is now ALLOWED to open the second paragraph. The old
        rule barred announcing the posting, on the theory it wastes the strongest line; Kevin's
        copy uses it as the bridge into the ask and he kept it deliberately.

    The 'brief' / 'Happy to work around your schedule' / 'perspective' coverage floors are gone:
    they described the previous bank's habits, and the new copy shares one body by design.
    """
    cold = _load_bank("outreach_templates.json")["cold_ops"]
    assert len(cold) == 8
    rendered_all = [
        m.interpolate_template(t, name="", company=_LINT_COMPANY, job_title=_LINT_TITLE)
        for t in cold
    ]

    for idx, (template, rendered) in enumerate(zip(cold, rendered_all)):
        ctx = f"cold_ops[{idx}]"
        assert "15 minutes" in rendered, ctx
        assert rendered.rstrip().endswith("Best,\nKevin"), ctx
        # college-corpus tells the professional corpus disproves
        assert "Yours In Service" not in rendered and "YIS" not in rendered, ctx
        assert "{name_bare}" not in template, ctx
        for banned_minutes in ("13 minute", "14 minute", "16 minute", "17 minute"):
            assert banned_minutes not in rendered, ctx
        assert "I built" not in rendered and "I automated" not in rendered, ctx
        # Opens by naming the application, which is the one fact that earns the reply.
        assert rendered.split("\n\n")[1].startswith("I recently applied to"), ctx

    # Every entry states the day-to-day ask - that is the whole point of the note.
    assert all(("day-to-day" in r) or ("day to day" in r) for r in rendered_all)


def test_gmail_generators_pass_the_same_linter_as_the_card_templates():
    """The regression guard against the two voice paths re-splitting. generate_*_email() render
    from the same JSON banks the Telegram card interpolates, so anything that would fail the card
    copy fails here too - and if someone reintroduces a hardcoded f-string body, this catches it.
    Hard violations only, matching the bank test above.
    """
    generated = [
        ("generate_cold_email", m.generate_cold_email(_LINT_TITLE, _LINT_COMPANY)),
        ("generate_warm_email", m.generate_warm_email(company_name=_LINT_COMPANY)),
        ("generate_bump_email", m.generate_bump_email(job_title=_LINT_TITLE, company_name=_LINT_COMPANY)),
    ]
    failures = [f"{name}: {v}" for name, body in generated for v in pu.lint_outreach_template(body, "email")]
    assert failures == []
    for name, body in generated:
        assert body.startswith("Hi,"), f"{name} should open on a bare 'Hi,' with no contact name"
        assert "Best regards" not in body
        assert "{" not in body, f"{name} left a placeholder uninterpolated"


def test_gmail_generators_render_the_same_string_the_card_shows():
    """Byte-for-byte parity is the point: /draft re-renders the routed template_id, so the Gmail
    body is the copy Kevin already approved on the card, not cold_ops[0] every time."""
    for template_id in range(8):
        card_copy = m.render_outreach_email(
            "cold_ops", template_id, name="", company=_LINT_COMPANY, job_title=_LINT_TITLE
        )
        gmail_copy = m.generate_cold_email(_LINT_TITLE, _LINT_COMPANY, template_id=template_id)
        assert card_copy == gmail_copy
    # Distinct entries really are distinct - a silent fallback-to-index-0 would collapse them.
    assert len({m.generate_cold_email(_LINT_TITLE, _LINT_COMPANY, template_id=i) for i in range(8)}) == 8


def test_generators_use_the_contact_name_when_one_is_known():
    assert m.generate_bump_email(contact_name="Dana", job_title=_LINT_TITLE).startswith("Hi Dana,")
    assert m.generate_warm_email(contact_name="Dana", company_name=_LINT_COMPANY).startswith("Hi Dana,")
    # The retired "there" sentinel degrades to a bare "Hi," instead of reappearing as "Hi there,".
    assert m.generate_cold_email(_LINT_TITLE, _LINT_COMPANY, contact_name="there").startswith("Hi,")



# ---- Unscheduled vs. overdue: Code.gs's blank-date sentinel ----

def test_is_followup_unscheduled_treats_blank_and_the_sentinel_as_unset():
    # Code.gs coerces a blank Next Followup Date cell to "1970-01-01" so its own overdue
    # sort never feeds NaN to new Date(). Python has to translate it back, or every undated
    # row reads as maximally overdue.
    assert pu.is_followup_unscheduled("") is True
    assert pu.is_followup_unscheduled(None) is True
    assert pu.is_followup_unscheduled("   ") is True
    assert pu.is_followup_unscheduled(pu.FOLLOWUP_BLANK_DATE_SENTINEL) is True
    assert pu.is_followup_unscheduled("1970-01-01T00:00:00Z") is True  # only the date part is read


def test_is_followup_unscheduled_leaves_real_dates_alone():
    for real in ("2026-08-31", "1970-01-02", "2020-01-01", "1969-12-31"):
        assert pu.is_followup_unscheduled(real) is False, real


# ---- ATS auto-expansion name guard ----

def test_ats_guard_rejects_the_personal_contacts_in_the_warm_crm():
    """Every one of these is a real Carmen Warm 'company' value. Each costs 3 sequential
    HTTP probes at up to 8s if it gets through."""
    for junk in ("mom", "cousin", "(fuck)", "Guy from birmingham venture capital",
                 "https://www.linkedin.com/in/elaine-ezekiel/", "Nathan at speaker event"):
        assert pu.is_probable_company_name(junk) is False, junk


def test_ats_guard_still_accepts_real_company_names():
    for company in ("Stellantis", "Atwell", "Guy Carpenter", "Recourse Communications, Inc."):
        assert pu.is_probable_company_name(company) is True, company


def test_ats_guard_matches_person_words_only_as_the_whole_name():
    # The reason the list is whole-name: "Guy Carpenter" is a real reinsurance broker, and
    # a substring match would silently stop probing it forever.
    assert pu.is_probable_company_name("guy") is False
    assert pu.is_probable_company_name("  Mom  ") is False   # trimmed and case-folded
    assert pu.is_probable_company_name("Guy Carpenter") is True
    assert pu.is_probable_company_name("Friend Studios") is True


def test_ats_guard_rejects_names_too_short_to_be_a_board_slug():
    for tiny in ("", None, "  ", "AB", "-", "&&&", "A.B."):
        assert pu.is_probable_company_name(tiny) is False, repr(tiny)
    assert pu.is_probable_company_name("IBM") is True  # exactly at the 3-char floor


def test_ats_guard_rejects_uncapitalized_names_and_pasted_urls():
    # Capitalization is what catches lowercase junk no keyword list could enumerate.
    assert pu.is_probable_company_name("some guy i met") is False
    assert pu.is_probable_company_name("stellantis") is False
    assert pu.is_probable_company_name("http://acme.com") is False
    assert pu.is_probable_company_name("www.Acme.com") is False
    assert pu.is_probable_company_name("Acme Corp") is True


def test_ats_slug_guess_strips_to_lowercase_alphanumerics():
    assert pu.ats_slug_guess("Recourse Communications, Inc.") == "recoursecommunicationsinc"
    assert pu.ats_slug_guess("Atwell") == "atwell"
    assert pu.ats_slug_guess(None) == ""


_SENT_CRM_COMPANIES = {"Affirm", "Signal Advisors", "Crain Communications", "AAA-The Auto Club", "Stellantis"}


def test_sent_capture_takes_real_people_at_tracked_job_companies():
    """The capture gate: a person is worth a Carmen Cold row only when Kevin emailed them
    because of a job already in the CRM."""
    got = pu.build_sent_contact('"Eina Assali" <eina.assali@affirm.com>', _SENT_CRM_COMPANIES)
    assert got == {"name": "Eina Assali", "email": "eina.assali@affirm.com", "company": "Affirm"}

    # No display name in the header - derive one from the local part rather than dropping it.
    derived = pu.build_sent_contact("eina.assali@affirm.com", _SENT_CRM_COMPANIES)
    assert derived["name"] == "Eina Assali"

    # A corporate mail subdomain still resolves to its company.
    assert pu.build_sent_contact("Jen <jen@mail.crain.com>", _SENT_CRM_COMPANIES)["company"] == "Crain Communications"


def test_sent_capture_skips_everything_that_is_not_a_tracked_person():
    """Role mailboxes are the job pipeline's own targets and already exist as job rows; consumer
    and ATS domains carry no employer; an untracked company means the email was not job outreach."""
    for header in (
        "operations@affirm.com",
        "wealthops@signaladvisors.com",
        "careers@stellantis.com",
        "Sandy <sandy.jones@gmail.com>",
        "recruiter@greenhouse.io",
        "Rob <rjk@some-untracked-co.com>",
        "",
    ):
        assert pu.build_sent_contact(header, _SENT_CRM_COMPANIES) is None, header


def test_sent_capture_domain_match_is_not_a_loose_substring():
    """'aa.com' must not match 'AAA-The Auto Club' - a substring test would credit a stranger's
    email to a tracked company and write a bogus contact."""
    assert pu.build_sent_contact("Bob <bob@aa.com>", _SENT_CRM_COMPANIES) is None
    assert pu.domain_matches_company("kevin@signaladvisors.com", "Signal Advisors") is True
    assert pu.domain_matches_company("bob@aa.com", "AAA-The Auto Club") is False


def test_brand_token_matches_a_company_whose_domain_differs_from_its_legal_name():
    """'Intact Services USA LLC' sends from intactinsurance.com. Neither whole string contains
    the other, so every substring test missed and a real contact Kevin had already drafted to was
    silently dropped from capture. The brand token (first significant word) has to carry it."""
    assert pu.domain_matches_company("sszajner@intactinsurance.com", "Intact Services USA LLC") is True
    assert pu.domain_matches_company("a@marinerwealthadvisors.com", "Mariner") is True

    # ...without opening up a generic lead word as a match. "First Financial" must not claim
    # firstsolar.com, and the token must be a PREFIX, so contactcenter.com is not "Intact".
    assert pu.domain_matches_company("a@firstsolar.com", "First Financial") is False
    assert pu.domain_matches_company("a@unitedairlines.com", "United Wholesale Mortgage") is False
    assert pu.domain_matches_company("a@contactcenter.com", "Intact Services USA LLC") is False
    assert pu.domain_matches_company("a@mainstreetbank.com", "Main Financial Group") is False
    # A short brand cannot match as a PREFIX - autozone.com is not "Auto Club". (An EXACT
    # short-brand match is allowed; see test_short_brand_matches_only_on_an_exact_label.)
    assert pu.domain_matches_company("a@autozone.com", "Auto Club") is False


def test_short_brand_matches_only_on_an_exact_label():
    """'Ford Motor Company' sends from ford.com, but the brand is 4 chars and every other test
    here has a >= 5 char floor, so a real contact was dropped silently. The relaxation is exact
    label == first word, which is far stricter than the prefix/substring tests it sits beside."""
    assert pu.domain_matches_company("jsmith@ford.com", "Ford Motor Company") is True

    # The floor's original job still holds. "aa" is not "AAA-The Auto Club" (first word "aaa"),
    # and a short word may not match as a prefix the way a long brand can.
    assert pu.domain_matches_company("bob@aa.com", "AAA-The Auto Club") is False
    assert pu.domain_matches_company("a@autozone.com", "Auto Club") is False
    # A different company that merely STARTS with the same letters must not be claimed.
    assert pu.domain_matches_company("a@ford.com", "Forward Financial Group") is False


def test_acronym_domain_matches_a_long_institutional_name():
    """'National Center for Manufacturing Sciences' mails from ncms.org. Its first word is a
    generic token and the acronym appears nowhere in the string, so every other test missed -
    and NCMS is a live company in the pipeline, so this was dropping real contacts."""
    assert pu.domain_matches_company("a@ncms.org", "National Center for Manufacturing Sciences") is True
    assert pu.domain_matches_company("a@ibm.com", "International Business Machines") is True

    # Stopwords are not carried into an acronym: the candidate is NCMS, never NCFMS.
    assert pu.domain_matches_company("a@ncfms.org", "National Center for Manufacturing Sciences") is False
    # A short name cannot acronym its way into a collision - "General Motors" is 2 words, so gm.com
    # is not accepted on initials alone.
    assert pu.domain_matches_company("a@gm.com", "General Motors") is False
    # And the acronym must be the WHOLE label, not a prefix of a longer unrelated domain.
    assert pu.domain_matches_company("a@ncmsystems.com", "National Center for Manufacturing Sciences") is False


# ---- Carmen Cold follow-up ladder ----

_LADDER_TODAY = date(2026, 9, 12)


def test_carmen_ladder_walks_every_rung_then_stops():
    """The whole point: ONE nudge at the CARMEN_LADDER_DAYS_ENGAGED offset from the day the
    contact landed, then the grace week, then spent. Dates are derived from the constant rather
    than hardcoded, so retuning the cadence is a one-line change instead of a test rewrite.

    The row carries a reply note, which is what puts it on the engaged ladder.
    The anchor stays Date Added because the reply predates it here.
    """
    anchor_date = date(2026, 9, 12)
    anchor = anchor_date.isoformat()
    note = f"[{(anchor_date - timedelta(days=1)).isoformat()}] {pu.INBOUND_REPLY_NOTE_MARKER} - said to circle back"
    (d1,) = (anchor_date + timedelta(days=n) for n in pu.CARMEN_LADDER_DAYS_ENGAGED)

    action, nxt = pu.plan_carmen_followup(anchor, "", _LADDER_TODAY, note)
    assert (action, nxt) == ("schedule", d1)

    # Final (and only) rung advances to the triage date (last rung + grace week) rather than
    # writing nothing - with no date written, the row re-read as its last rung and re-fired.
    terminal = anchor_date + timedelta(days=pu.CARMEN_TERMINAL_GAP_DAYS)
    action, nxt = pu.plan_carmen_followup(anchor, d1.isoformat(), d1, note)
    assert (action, nxt) == ("nudge_1", terminal)

    # Quiet through the grace week, then exhausted - the path that used to be unreachable.
    assert pu.plan_carmen_followup(anchor, terminal.isoformat(), terminal - timedelta(days=1), note) == ("none", None)
    assert pu.plan_carmen_followup(anchor, terminal.isoformat(), terminal, note) == ("exhausted", None)


def test_both_contact_ladders_bump_once_at_day_four_then_run_to_three_weeks():
    """Retuned 2026-09-24: ONE nudge at day 4, and the row is spent 21 days after the anchor.

    Cold and engaged used to differ by a day (2 vs 3). They are now identical - a contact
    mid-thread who has gone quiet for four days is the same case as a stranger who never
    answered, and the split bought nothing measurable. The 21-day total matches the job side's
    FOLLOWUP_BURY_DAYS so a contact and an application leave the board on the same schedule.
    """
    assert pu.CARMEN_LADDER_DAYS_COLD == (4,)
    assert pu.CARMEN_LADDER_DAYS_ENGAGED == (4,)
    assert pu.carmen_terminal_gap(pu.CARMEN_LADDER_DAYS_COLD) == 21
    assert pu.carmen_terminal_gap(pu.CARMEN_LADDER_DAYS_ENGAGED) == pu.FOLLOWUP_BURY_DAYS

    anchor_date = date(2026, 9, 12)
    anchor = anchor_date.isoformat()
    (d1,) = (anchor_date + timedelta(days=n) for n in pu.CARMEN_LADDER_DAYS_COLD)

    terminal = anchor_date + timedelta(days=pu.carmen_terminal_gap(pu.CARMEN_LADDER_DAYS_COLD))
    assert pu.plan_carmen_followup(anchor, d1.isoformat(), d1) == ("nudge_1", terminal)

    # No second nudge: the grace period runs, then the row is spent.
    assert pu.plan_carmen_followup(anchor, terminal.isoformat(), terminal) == ("exhausted", None)


@pytest.mark.parametrize("raw,expected", [
    # The title from the CRM that started this: aggregator filler must not reach a stranger.
    ("Financial Operations Analyst Intermediate /work from home reputed company reputed company/",
     "Financial Operations Analyst Intermediate"),
    ("Financial Operations Analyst, reputed company and Cash Conversion",
     "Financial Operations Analyst"),
    # Req numbers, in the three shapes the boards use.
    ("Account Receivable Compliance Analyst (3114)", "Account Receivable Compliance Analyst"),
    ("Production Support Analyst - #26343", "Production Support Analyst"),
    ("Finance Manager REQ 99283", "Finance Manager"),
    # Work style and schedule tails.
    ("Business Systems Analyst  - Remote", "Business Systems Analyst"),
    ("Salesforce Technical Administrator (Hybrid)", "Salesforce Technical Administrator"),
    ("Patient Financial Services Analyst, FT, Days, - Remote", "Patient Financial Services Analyst"),
    ("CBO Business Operation Analyst - Full Time Days - Hybrid (Michigan Residents)",
     "CBO Business Operation Analyst"),
    # Location tails, including the board restating the employer inside the title.
    ("Salesforce Administrator at Bedrock Management Services LLC Detroit, MI",
     "Salesforce Administrator"),
    ("Wealth Planner - Farmington Hills, MI", "Wealth Planner"),
    ("Import/Export & Warehouse Operations Specialist – Detroit, MI (On-site)",
     "Import/Export & Warehouse Operations Specialist"),
    # Salary bait.
    ("Remote Customer Success Associate 60k 80k FinTech 23", "Remote Customer Success Associate"),
    # ALREADY CLEAN - these must come out byte-identical. The comma in "Analyst, Financial
    # Operations" is load-bearing, and "(Investment Team)" is a real qualifier, not a location.
    ("Analyst, Financial Operations", "Analyst, Financial Operations"),
    ("Financial Analyst (Investment Team)", "Financial Analyst (Investment Team)"),
    ("Strategic Finance Analyst II (Revenue)", "Strategic Finance Analyst II (Revenue)"),
    ("Customs Analyst - Import/Export Operations Analyst",
     "Customs Analyst - Import/Export Operations Analyst"),
    ("Data Analyst", "Data Analyst"),
    ("", ""),
])
def test_sanitize_job_title_peels_board_noise_without_touching_real_titles(raw, expected):
    assert pu.sanitize_job_title(raw) == expected


@pytest.mark.parametrize("raw", [
    "AlixPartners", "Blue Cross Blue Shield of Michigan", "Stripe", "Plante Moran",
    "Compu-Vision - Northeast",   # a company name that landed in the Role column
    "N/A", "TBD", "", "   ", "-",
])
def test_is_clean_job_title_rejects_anything_that_is_not_a_role(raw):
    """These are all real Role-column values. None of them may be interpolated into a sentence
    that auto-sends - "the Stripe role at Stripe" is how a system announces itself as a bot."""
    assert pu.is_clean_job_title(raw) is False


@pytest.mark.parametrize("raw", [
    "Data Analyst", "Analyst, Financial Operations", "Salesforce Tech Admin – Hybrid Role",
    "Financial Operations Analyst Intermediate /work from home reputed company reputed company/",
    "Client Operations Specialist - Livonia", "Salesforce BA: Process Improvement & UAT Support",
])
def test_is_clean_job_title_accepts_real_roles_including_ones_needing_cleanup(raw):
    assert pu.is_clean_job_title(raw) is True


def test_a_linkedin_touch_reanchors_without_faking_a_reply():
    """THE POINT of a separate marker: /linkedin records that KEVIN reached out on another
    channel. It must move the anchor - the contact was just contacted, so an email bump two days
    later reads as pestering - while leaving the row on the COLD track. Reusing the inbound marker
    here would promote the row and count a reply that never happened."""
    added = date(2026, 9, 1)
    touched = date(2026, 9, 6)
    note = f"[{touched.isoformat()}] {pu.LINKEDIN_TOUCH_NOTE_MARKER} - connect/DM sent by hand."

    plan = pu.plan_carmen_ladder(added.isoformat(), "", touched, note)
    assert plan.anchor == touched, "the ladder spaces off the real last contact"
    assert plan.replied is False, "a touch Kevin sent is NOT a reply"
    assert plan.ladder == pu.CARMEN_LADDER_DAYS_COLD, "and must not promote the track"
    assert pu.carmen_linkedin_anchor(note) == touched
    assert pu.carmen_reply_anchor(note) is None, "it must never read as an inbound reply"


def test_a_real_reply_still_outranks_a_linkedin_touch():
    """Both markers on one row: the anchor is the latest of the two, but the TRACK is decided by
    the reply alone - so a later LinkedIn touch re-spaces the ladder without demoting an engaged
    contact back to cold."""
    replied = date(2026, 9, 6)
    touched = date(2026, 9, 10)
    note = (f"[{replied.isoformat()}] {pu.INBOUND_REPLY_NOTE_MARKER} - asked for a call\n"
            f"[{touched.isoformat()}] {pu.LINKEDIN_TOUCH_NOTE_MARKER} - connect/DM sent by hand.")

    plan = pu.plan_carmen_ladder("2026-09-01", "", touched, note)
    assert plan.anchor == touched
    assert plan.replied is True and plan.ladder == pu.CARMEN_LADDER_DAYS_ENGAGED


def test_a_reply_promotes_a_cold_row_to_the_engaged_ladder():
    """The promotion is automatic and needs nothing set by hand: once a reply note exists the row
    switches ladders, and the anchor moves to the reply date."""
    added = date(2026, 9, 1)
    replied = date(2026, 9, 6)
    note = f"[{replied.isoformat()}] {pu.INBOUND_REPLY_NOTE_MARKER} - asked for a call"

    plan = pu.plan_carmen_ladder(added.isoformat(), "", replied, note)
    assert plan.replied is True
    assert plan.ladder == pu.CARMEN_LADDER_DAYS_ENGAGED
    assert plan.anchor == replied
    assert plan.next_date == replied + timedelta(days=pu.CARMEN_LADDER_DAYS_ENGAGED[0])

    # Same row without the note stays cold.
    cold = pu.plan_carmen_ladder(added.isoformat(), "", replied)
    assert cold.replied is False
    assert cold.ladder == pu.CARMEN_LADDER_DAYS_COLD


def test_legacy_mid_ladder_cold_row_gets_its_last_nudge_not_a_kill():
    """MIGRATION. A cold row already scheduled at the old ladder's day-21 nudge must not read as
    exhausted against the shorter cold ladder and be killed on the first pass after deploy."""
    anchor = date(2026, 9, 1)
    legacy = anchor + timedelta(days=pu.CARMEN_LADDER_DAYS_ENGAGED[-1])
    cold_terminal = anchor + timedelta(days=pu.carmen_terminal_gap(pu.CARMEN_LADDER_DAYS_COLD))

    action, nxt = pu.plan_carmen_followup(anchor.isoformat(), legacy.isoformat(), legacy)
    assert (action, nxt) == ("nudge_1", cold_terminal)

    # And it still terminates rather than looping.
    assert pu.plan_carmen_followup(anchor.isoformat(), cold_terminal.isoformat(), cold_terminal) == ("exhausted", None)

    # The OLD TERMINAL gap is a finished ghost, not a row owed a nudge: it must stay exhausted,
    # or dead rows resurrect on every pass and never reach Killed.
    old_terminal = anchor + timedelta(days=pu.CARMEN_TERMINAL_GAP_DAYS)
    assert pu.plan_carmen_followup(anchor.isoformat(), old_terminal.isoformat(), old_terminal) == ("exhausted", None)


def test_carmen_status_marker_counts_total_contacts_not_rungs():
    """Day 0 is the original email, so the single nudge on either ladder reads '1 of 2'."""
    cold, engaged = pu.CARMEN_LADDER_DAYS_COLD, pu.CARMEN_LADDER_DAYS_ENGAGED
    assert pu.carmen_status_marker("nudge_1", False, cold) == "COLD · 1 of 2"
    assert pu.carmen_status_marker("nudge_1", True, engaged) == "WARM · 1 of 2"
    assert pu.carmen_status_marker("schedule", False, cold) == "NEW · unsent"
    assert pu.carmen_status_marker("exhausted", False, cold) == "COLD · spent"
    assert pu.carmen_status_marker("exhausted", True, engaged) == "WARM · spent"
    assert pu.carmen_status_marker("none", False, cold) is None


def test_carmen_marker_cell_preserves_hand_typed_context():
    """Column E is Kevin's 'Context / Priority'. The marker is prepended, never destructive, and
    a previous marker is replaced rather than stacked."""
    assert pu.carmen_marker_cell("", "COLD · 1 of 3") == "COLD · 1 of 3"
    assert pu.carmen_marker_cell("referred by Dana", "COLD · 1 of 3") == "COLD · 1 of 3 | referred by Dana"
    assert pu.carmen_marker_cell("COLD · 1 of 3", "COLD · 2 of 3") == "COLD · 2 of 3"
    assert pu.carmen_marker_cell("COLD · 1 of 3 | referred by Dana", "COLD · 2 of 3") == "COLD · 2 of 3 | referred by Dana"
    # A bare priority integer is context, not a marker, and survives.
    assert pu.carmen_marker_cell("8", "COLD · 1 of 3") == "COLD · 1 of 3 | 8"
    # Nothing to write leaves the cell alone.
    assert pu.carmen_marker_cell("referred by Dana", None) == "referred by Dana"


def test_carmen_ladder_starts_a_manually_moved_row_from_today():
    """A row dragged into Carmen Cold by hand carries a stale Date Added and no follow-up date.
    It must enter the ladder on the next pass, not be skipped and not fire immediately."""
    action, nxt = pu.plan_carmen_followup("2026-01-04", "", _LADDER_TODAY)
    assert action == "schedule"
    assert nxt == _LADDER_TODAY + timedelta(days=pu.CARMEN_LADDER_DAYS_COLD[0])

    # Same for a row with no Date Added at all.
    action, nxt = pu.plan_carmen_followup("", "", _LADDER_TODAY)
    assert action == "schedule"
    assert nxt == _LADDER_TODAY + timedelta(days=pu.CARMEN_LADDER_DAYS_COLD[0])


def test_carmen_ladder_is_quiet_until_due_and_after_exhaustion():
    assert pu.plan_carmen_followup("2026-09-12", "2026-09-30", _LADDER_TODAY) == ("none", None)
    # Past the last rung: the ladder-written triage date reads as exhausted.
    assert pu.plan_carmen_followup("2026-09-12", "2026-10-10", date(2026, 10, 10))[0] == "exhausted"
    # Past the ladder entirely (a gap the ladder never writes, on an anchor over 30 days old):
    # this used to read as exhausted too, and now revives. The staleness rule deliberately
    # treats a date the ladder did not write as a bench/stalled row, not a finished ghost.
    plan = pu.plan_carmen_ladder("2026-09-12", "2026-10-20", date(2026, 10, 20))
    assert (plan.action, plan.revived) == ("schedule", True)


def test_carmen_ladder_triages_a_finished_ghost_even_when_the_run_is_late():
    """A ladder-written triage date is trusted however old the anchor is - otherwise a sequencer
    run that slipped past day 30 would revive the ghost instead of killing it, and it would loop."""
    anchor = date(2026, 9, 1)
    terminal = anchor + timedelta(days=pu.CARMEN_TERMINAL_GAP_DAYS)
    late = anchor + timedelta(days=pu.CARMEN_STALE_ANCHOR_DAYS + 10)
    plan = pu.plan_carmen_ladder(anchor.isoformat(), terminal.isoformat(), late)
    assert (plan.action, plan.revived) == ("exhausted", False)


# ---- Stale-anchor revival (CARMEN_STALE_ANCHOR_DAYS) ----

def test_stale_bench_contact_with_a_set_date_starts_at_rung_1_not_exhausted():
    """The bug this rule prevents: a Carmen Warm contact dragged into Carmen Cold carries a
    ~210-day-old Last Contact Date, so the old gap read as long past the last rung and the contact
    was killed on the first pass without a single nudge."""
    today = date(2026, 9, 12)
    old = (today - timedelta(days=210)).isoformat()
    plan = pu.plan_carmen_ladder(old, today.isoformat(), today)
    assert plan.action == "schedule"
    assert plan.next_date == today + timedelta(days=pu.CARMEN_LADDER_DAYS_COLD[0])
    assert plan.revived is True
    assert plan.anchor == today
    # Two-value wrapper agrees and keeps its contract.
    assert pu.plan_carmen_followup(old, today.isoformat(), today) == ("schedule", plan.next_date)


def test_revived_row_climbs_the_ladder_once_its_restart_note_is_recorded():
    """Revival is compute-only for Date Added, so the restart has to be persisted as a note -
    otherwise the next pass revives again and the row never gets past "schedule"."""
    today = date(2026, 9, 12)
    old = (today - timedelta(days=210)).isoformat()
    first = today + timedelta(days=pu.CARMEN_LADDER_DAYS_COLD[0])
    note = f"[2026-01-01] Met at conference\n[{today.isoformat()}] {pu.LADDER_RESTART_NOTE_MARKER} (revived)"

    plan = pu.plan_carmen_ladder(old, first.isoformat(), first, note=note)
    assert (plan.action, plan.revived, plan.anchor) == ("nudge_1", False, today)


def test_old_dates_that_look_ladder_shaped_still_revive():
    """Bench dates 210 and 200 days back sit 10 days apart - a rung-2 gap. Trusting that as a live
    ladder position would nudge twice and kill the contact within three days of promotion."""
    today = date(2026, 9, 12)
    plan = pu.plan_carmen_ladder((today - timedelta(days=210)).isoformat(),
                                 (today - timedelta(days=200)).isoformat(), today)
    assert (plan.action, plan.revived) == ("schedule", True)


def test_stale_anchor_waits_for_a_hand_set_future_date():
    """"hold", not "none": both are no-ops, but a hand-dated row the ladder is not driving is
    worth marking on the sheet, where it would otherwise look like a row the sequencer forgot.
    The ordinary quiet between rungs stays "none" (see test_recent_anchor_is_not_revived) so it
    does not re-stamp Column E every morning and bury the real rung."""
    today = date(2026, 9, 12)
    old = (today - timedelta(days=210)).isoformat()
    plan = pu.plan_carmen_ladder(old, (today + timedelta(days=60)).isoformat(), today)
    assert (plan.action, plan.revived) == ("hold", False)
    assert pu.carmen_status_marker(plan.action, plan.replied, plan.ladder) == "HOLD · dated"


def test_recent_anchor_is_not_revived():
    today = date(2026, 9, 12)
    recent = today - timedelta(days=10)
    plan = pu.plan_carmen_ladder(recent.isoformat(), (recent + timedelta(days=11)).isoformat(), today)
    assert plan.revived is False and plan.anchor == recent
    assert plan.action == "none"  # rung 2 not due until day 11


def test_blank_anchor_still_falls_back_to_today():
    today = date(2026, 9, 12)
    plan = pu.plan_carmen_ladder("", "", today)
    assert plan.action == "schedule"
    assert plan.next_date == today + timedelta(days=pu.CARMEN_LADDER_DAYS_COLD[0])
    assert plan.anchor == today
    # Flagged as a revival so the caller records the restart: a blank Date Added otherwise
    # re-anchors on a new "today" every pass and the row never reaches rung 1.
    assert plan.revived is True


# ---- Reply re-anchoring (carmen_reply_anchor) ----

def _reply(day):
    return f"[{day}] {pu.INBOUND_REPLY_NOTE_MARKER} (they wrote to Kevin, not a send). Subject: hi."


def test_reply_anchor_with_no_note_is_none():
    assert pu.carmen_reply_anchor("") is None
    assert pu.carmen_reply_anchor(None) is None
    assert pu.carmen_reply_anchor("[2026-09-01] Promoted to Carmen Hot.") is None


def test_reply_anchor_reads_one_reply():
    assert pu.carmen_reply_anchor(_reply("2026-09-01")) == date(2026, 9, 1)


def test_reply_anchor_latest_of_several_wins():
    note = "\n".join([_reply("2026-09-05"), "[2026-09-06] called", _reply("2026-09-10"), _reply("2026-08-01")])
    assert pu.carmen_reply_anchor(note) == date(2026, 9, 10)


def test_reply_anchor_skips_malformed_dates():
    assert pu.carmen_reply_anchor(_reply("2026-13-45")) is None
    assert pu.carmen_reply_anchor(_reply("2026-02-30") + "\n" + _reply("2026-09-02")) == date(2026, 9, 2)


def test_reply_older_than_date_added_leaves_the_anchor_alone():
    today = date(2026, 9, 12)
    # Scheduled exactly one engaged rung after Date Added, so the row reads as sitting on rung 1
    # whatever that rung's offset currently is.
    scheduled = date(2026, 9, 8) + timedelta(days=pu.CARMEN_LADDER_DAYS_ENGAGED[0])
    plan = pu.plan_carmen_ladder("2026-09-08", scheduled.isoformat(), scheduled,
                                 note=_reply("2026-08-20"))
    assert plan.anchor == date(2026, 9, 8)
    assert plan.action == "nudge_1"


def test_reply_newer_than_date_added_restarts_the_ladder_from_the_reply():
    """A live conversation must not die on the same clock as a ghost."""
    today = date(2026, 9, 12)
    reply_day = date(2026, 9, 8)
    # Date Added is 22 days before the reply; the reply handler set Next Followup one engaged
    # rung past the reply, which is where the ladder picks the row up.
    rung = pu.CARMEN_LADDER_DAYS_ENGAGED[0]
    plan = pu.plan_carmen_ladder("2026-08-17", (reply_day + timedelta(days=rung)).isoformat(),
                                 reply_day + timedelta(days=rung),
                                 note=_reply(reply_day.isoformat()))
    assert plan.anchor == reply_day
    assert (plan.action, plan.revived) == ("nudge_1", False)


def test_stale_reply_anchor_is_revived_too():
    """The staleness rule applies to whichever anchor wins."""
    today = date(2026, 9, 12)
    plan = pu.plan_carmen_ladder("2026-01-01", "", today, note=_reply("2026-06-01"))
    assert (plan.action, plan.revived, plan.anchor) == ("schedule", True, today)


def test_carmen_ladder_tolerates_a_late_sequencer_run():
    """The nightly job only advances rows on days it actually runs, so a date a couple of days
    past its rung must still read as that rung instead of skipping ahead."""
    action, _ = pu.plan_carmen_followup("2026-09-12", "2026-09-15", date(2026, 9, 17))
    assert action == "nudge_1"


# ---- Untouched "Matched" row expiry ----

def test_expired_matched_row_only_retires_untouched_pipeline_output():
    """A row Kevin never engaged with ages out at 30d; anything he touched never does."""
    today = date(2026, 9, 12)
    assert pu.is_expired_matched_row("Matched", "2026-08-13", today) is True   # 30d
    assert pu.is_expired_matched_row("Matched", "2026-08-14", today) is False  # 29d
    for engaged in ("Applied", "Replied", "Screening", "Interviewing", "Offer"):
        assert pu.is_expired_matched_row(engaged, "2026-01-01", today) is False, engaged


def test_expired_matched_row_never_fires_without_a_parseable_date():
    """A stale row beats silently retiring one whose Date Added simply failed to parse."""
    today = date(2026, 9, 12)
    assert pu.is_expired_matched_row("Matched", "", today) is False
    assert pu.is_expired_matched_row("Matched", "1970-01-01", today) is False
    assert pu.is_expired_matched_row("Matched", "not-a-date", today) is False


# ---- Sent-mail contact-email back-fill ----

def test_is_guessed_contact_email_flags_pipeline_placeholders():
    """Every address resolve_target_email() can invent must read as a guess, tagged or not."""
    for guess in (
        "operations@affirm.com",
        "bizops@ally.com",
        "wealthops@recoursecommunicationsinc.com",
        "compliance@mersino.com",
        "operations@intactinsurancespecialtysolutions.com [⚠️ Fallback Email]",
        "",
        None,
    ):
        assert pu.is_guessed_contact_email(guess) is True, guess


def test_is_guessed_contact_email_protects_real_people():
    """A human address - however it got there - is never a placeholder to overwrite."""
    for real in ("eina.assali@affirm.com", "jeremy@mainfinancialgroup.com", "sshruti@hcltech.com"):
        assert pu.is_guessed_contact_email(real) is False, real


def test_resolve_sent_email_backfill_promotes_the_real_address():
    """The address actually emailed replaces the guess on that company's job row."""
    rows = [{"sheet_uuid": "u-1", "company": "HCLTech", "email": "operations@hcltech.com"}]
    assert pu.resolve_sent_email_backfill("Soumya <sshruti@hcltech.com>", rows) == ("u-1", "sshruti@hcltech.com")


def test_resolve_sent_email_backfill_ignores_role_mailboxes():
    """Mail sent TO a generic inbox is not evidence of a real contact."""
    rows = [{"sheet_uuid": "u-1", "company": "HCLTech", "email": "operations@hcltech.com"}]
    assert pu.resolve_sent_email_backfill("operations@hcltech.com", rows) is None


def test_resolve_sent_email_backfill_never_clobbers_a_real_address():
    """Idempotence: a row already carrying a person is left alone on the next scan."""
    rows = [{"sheet_uuid": "u-1", "company": "HCLTech", "email": "sshruti@hcltech.com"}]
    assert pu.resolve_sent_email_backfill("Someone <other.person@hcltech.com>", rows) is None


def test_resolve_sent_email_backfill_requires_a_domain_company_match():
    """A personal address at an unrelated domain must not land on someone else's row."""
    rows = [{"sheet_uuid": "u-1", "company": "HCLTech", "email": "operations@hcltech.com"}]
    assert pu.resolve_sent_email_backfill("Recruiter <jeremy@totallyunrelated.com>", rows) is None
    assert pu.resolve_sent_email_backfill("Friend <someone@gmail.com>", rows) is None


def test_resolve_sent_email_backfill_picks_the_matching_company_row():
    """With several guessed rows live, only the domain-matching one is updated."""
    rows = [
        {"sheet_uuid": "u-affirm", "company": "Affirm", "email": "operations@affirm.com"},
        {"sheet_uuid": "u-hcl", "company": "HCLTech", "email": "operations@hcltech.com"},
    ]
    assert pu.resolve_sent_email_backfill("Eina <eina.assali@affirm.com>", rows) == ("u-affirm", "eina.assali@affirm.com")


def test_resolve_sent_email_backfill_skips_rows_without_a_uuid():
    """No UUID means no addressable row - nothing to update."""
    rows = [{"sheet_uuid": "", "company": "HCLTech", "email": "operations@hcltech.com"}]
    assert pu.resolve_sent_email_backfill("Soumya <sshruti@hcltech.com>", rows) is None


# ---- Manual job ingest (/job + bookmarklet) ----

# The URL Kevin actually copies out of the address bar while browsing job search results:
# the posting id rides in currentJobId, not the path, and a long tracking tail follows it.
SEARCH_RESULTS_URL = (
    "https://www.linkedin.com/jobs/search-results/?currentJobId=4461280495"
    "&eBP=NON_CHARGEABLE_CHANNEL&refId=oYcnP6aWR%2B5Ez3mob"
)
PERMALINK_URL = "https://www.linkedin.com/jobs/view/4461280495/"


def test_extract_linkedin_job_id_reads_both_url_shapes():
    assert pu.extract_linkedin_job_id(SEARCH_RESULTS_URL) == "4461280495"
    assert pu.extract_linkedin_job_id(PERMALINK_URL) == "4461280495"
    assert pu.extract_linkedin_job_id("https://example.com/careers/123") is None
    assert pu.extract_linkedin_job_id("") is None
    assert pu.extract_linkedin_job_id(None) is None


def test_canonical_linkedin_job_url_strips_tracking_tail():
    """Both shapes of the same posting must collapse to one URL, or the dedup key is per-visit."""
    assert pu.canonical_linkedin_job_url(SEARCH_RESULTS_URL) == PERMALINK_URL
    assert pu.canonical_linkedin_job_url(PERMALINK_URL) == PERMALINK_URL


def test_canonical_linkedin_job_url_passes_through_non_linkedin():
    assert pu.canonical_linkedin_job_url("https://boards.greenhouse.io/acme/jobs/1") == \
        "https://boards.greenhouse.io/acme/jobs/1"


def test_is_linkedin_job_url_is_not_broken_by_the_camelcase_param():
    """Regression: lowercasing the whole URL before extraction makes currentJobId unmatchable,
    which silently disabled the scraper for exactly the URL Kevin pastes most."""
    assert pu.is_linkedin_job_url(SEARCH_RESULTS_URL) is True
    assert pu.is_linkedin_job_url(SEARCH_RESULTS_URL.lower()) is True
    assert pu.is_linkedin_job_url(PERMALINK_URL) is True
    assert pu.is_linkedin_job_url("https://example.com/careers/123") is False
    assert pu.is_linkedin_job_url("https://www.linkedin.com/feed/") is False


def test_parse_job_command_bare_url_defers_title_to_the_scraper():
    assert pu.parse_job_command(f"/job {PERMALINK_URL}") == (None, None, PERMALINK_URL)
    assert pu.parse_job_command(f"/j {PERMALINK_URL}") == (None, None, PERMALINK_URL)


def test_parse_job_command_explicit_form_is_the_auth_wall_fallback():
    assert pu.parse_job_command(
        "/job Foreign Exchange Ops Analyst 2 @ Huntington National Bank https://x.co/1"
    ) == ("Foreign Exchange Ops Analyst 2", "Huntington National Bank", "https://x.co/1")


def test_parse_job_command_explicit_form_without_a_url():
    assert pu.parse_job_command("/job Analyst @ Ally") == ("Analyst", "Ally", "")


def test_parse_job_command_rejects_unusable_input():
    assert pu.parse_job_command("/job") is None
    assert pu.parse_job_command("/job    ") is None
    assert pu.parse_job_command("") is None
    assert pu.parse_job_command(None) is None


def test_parse_job_page_html_prefers_json_ld():
    body = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"JobPosting","title":"FX Ops Analyst 2",'
        '"hiringOrganization":{"name":"Huntington National Bank"},'
        '"description":"<p>Settle trades.</p><ul><li>Reconcile</li></ul>"}'
        '</script></head><body></body></html>'
    )
    title, company, description = pu.parse_job_page_html(body)
    assert title == "FX Ops Analyst 2"
    assert company == "Huntington National Bank"
    assert "Settle trades." in description
    assert "Reconcile" in description
    assert "<p>" not in description  # markup flattened, not passed through to Gemini


def test_parse_job_page_html_falls_back_to_the_title_tag():
    body = "<html><head><title>Ally hiring Associate Analyst in Detroit, MI | LinkedIn</title></head></html>"
    title, company, _ = pu.parse_job_page_html(body)
    assert title == "Associate Analyst"
    assert company == "Ally"


def test_parse_job_page_html_returns_empty_on_an_auth_wall():
    """An auth wall is the NORMAL datacenter-IP outcome, not an error - it must come back empty
    so the caller asks Kevin to type the title rather than filing a 'Manual Ingest' row."""
    assert pu.parse_job_page_html("<html><body>Sign in to continue</body></html>") == ("", "", "")
    assert pu.parse_job_page_html("") == ("", "", "")
    assert pu.parse_job_page_html(None) == ("", "", "")


def test_strip_html_to_text_keeps_bullet_structure():
    out = pu.strip_html_to_text("<p>Duties:</p><ul><li>Reconcile trades</li><li>SWIFT</li></ul>")
    assert "Duties:" in out
    assert "- Reconcile trades" in out
    assert "- SWIFT" in out
    assert "<" not in out


def test_strip_html_to_text_decodes_entities():
    assert "Smith & Sons" in pu.strip_html_to_text("<p>Smith &amp; Sons</p>")


def test_build_ingest_job_dict_matches_the_feed_job_shape():
    """process_single_candidate() must not be able to tell a pasted job from a sourced one."""
    job = pu.build_ingest_job_dict("FX Ops Analyst", "Huntington", "Settle trades", SEARCH_RESULTS_URL)
    for key in (
        "job_id", "employer_name", "job_title", "job_description", "job_apply_link",
        "job_city", "job_state", "job_is_remote", "job_posted_at_datetime_utc",
    ):
        assert key in job
    assert job["employer_name"] == "Huntington"
    assert job["job_title"] == "FX Ops Analyst"
    assert job["job_apply_link"] == PERMALINK_URL  # stored canonical, not the tracking URL


def test_build_ingest_job_dict_id_is_attributable_and_not_ats():
    job = pu.build_ingest_job_dict("FX Ops", "Huntington", "d", PERMALINK_URL)
    assert pu.derive_job_source(job["job_id"]) == "manual_ingest"
    # The Clavicular +30 gate keys on ATS prefixes; a pasted job must not slip through it.
    assert not job["job_id"].startswith(("gh_", "lever_", "ashby_"))


def test_build_ingest_job_dict_id_is_stable_across_url_shapes():
    """Re-pasting the same posting from a fresh search must dedup, not write a second row."""
    a = pu.build_ingest_job_dict("FX Ops", "Huntington", "d", SEARCH_RESULTS_URL)
    b = pu.build_ingest_job_dict("FX Ops", "Huntington", "d", PERMALINK_URL)
    assert a["job_id"] == b["job_id"]


def test_build_ingest_job_dict_falls_back_when_fields_are_blank():
    job = pu.build_ingest_job_dict("", "", "", "")
    assert job["employer_name"] == "Manual Ingest"
    assert job["job_title"] == "Manually Ingested Role"


def test_parse_job_page_html_reads_the_guest_endpoint_h2_title():
    """Regression, caught against the live endpoint: the jobs-guest page - the only surface that
    answers a server-side fetch - puts the job title in an <h2 class="...topcard__title">, not an
    <h1>. Matching only <h1> filed every scraped job as "Manually Ingested Role"."""
    body = (
        '<a class="topcard__link"><h2 class="top-card-layout__title font-sans text-lg font-bold '
        'topcard__title">Foreign Exchange Ops Analyst 2</h2></a>'
        '<div class="topcard__flavor-row"><span class="topcard__flavor">'
        '<a class="topcard__org-name-link topcard__flavor--black-link" href="/company/x">'
        'Huntington National Bank</a></span></div>'
    )
    title, company, _ = pu.parse_job_page_html(body)
    assert title == "Foreign Exchange Ops Analyst 2"
    assert company == "Huntington National Bank"


# ---- Non-LinkedIn job pages (employer careers sites) ----

# The employer-hosted page behind LinkedIn's Apply button. Unlike LinkedIn it answers a plain
# GET with a full schema.org JobPosting, so it is the BETTER source when Kevin has this link.
CAREERS_BASE = (
    "https://huntington-careers.com/search/jobdetails/foreign-exchange-ops-analyst-2/"
    "d9b4805d-d019-42f1-a408-c824ed2c36bb"
)
CAREERS_URL = CAREERS_BASE + (
    "?utm_source=linkedin&utm_medium=paid_job_board&utm_campaign=linkedin_paid"
    "&source=LinkedIn_Corporate_Page"
)


def test_strip_tracking_params_drops_campaign_noise():
    assert pu.strip_tracking_params(CAREERS_URL) == CAREERS_BASE


def test_strip_tracking_params_keeps_functional_params():
    """Only arrival-tracking is noise - a param that identifies the posting must survive."""
    assert pu.strip_tracking_params("https://x.co/job?jobId=99&utm_source=li") == \
        "https://x.co/job?jobId=99"


def test_strip_tracking_params_passes_through_bare_urls():
    assert pu.strip_tracking_params(CAREERS_BASE) == CAREERS_BASE
    assert pu.strip_tracking_params("") == ""
    assert pu.strip_tracking_params(None) == ""


def test_canonical_job_url_handles_both_linkedin_and_careers_pages():
    assert pu.canonical_job_url(SEARCH_RESULTS_URL) == PERMALINK_URL
    assert pu.canonical_job_url(CAREERS_URL) == CAREERS_BASE


def test_ingest_id_is_stable_across_ad_sources():
    """The same careers posting reached from a LinkedIn ad and an Indeed ad is ONE job - without
    stripping tracking params it would write two Tetiana Cold rows for the same role."""
    via_linkedin = pu.build_ingest_job_dict("t", "c", "d", CAREERS_BASE + "?utm_source=linkedin")
    via_indeed = pu.build_ingest_job_dict("t", "c", "d", CAREERS_BASE + "?utm_source=indeed")
    assert via_linkedin["job_id"] == via_indeed["job_id"]


def test_ingest_stores_the_clean_apply_link():
    job = pu.build_ingest_job_dict("t", "c", "d", CAREERS_URL)
    assert job["job_apply_link"] == CAREERS_BASE
    assert "utm_" not in job["job_apply_link"]


def test_parse_job_page_html_reads_a_generic_careers_page_json_ld():
    """No LinkedIn markup anywhere - this is the shape Workday/iCIMS/Phenom emit."""
    body = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org/","@type":"JobPosting",'
        '"title":"Foreign Exchange Ops Analyst 2",'
        '"hiringOrganization":{"@type":"Organization","name":"Huntington"},'
        '"description":"<p>Supports daily FX settlement.</p>"}'
        '</script></head><body></body></html>'
    )
    title, company, description = pu.parse_job_page_html(body)
    assert title == "Foreign Exchange Ops Analyst 2"
    assert company == "Huntington"
    assert "FX settlement" in description


def test_parse_job_page_html_reads_a_pipe_delimited_title_tag():
    """Careers-page convention: "Role | Location | Employer" - first segment is the role,
    last is the employer, and anything between is a location to discard."""
    body = "<html><head><title>Foreign Exchange Ops Analyst 2 | Multiple Locations | Huntington</title></head></html>"
    title, company, _ = pu.parse_job_page_html(body)
    assert title == "Foreign Exchange Ops Analyst 2"
    assert company == "Huntington"


def test_parse_job_page_html_pipe_fallback_needs_two_segments():
    """A single-segment title is just a page name - guessing an employer from it would be wrong."""
    title, company, _ = pu.parse_job_page_html("<html><head><title>Careers</title></head></html>")
    assert title == ""
    assert company == ""


def test_linkedin_title_shape_still_wins_over_the_pipe_fallback():
    body = "<html><head><title>Ally hiring Associate Analyst in Detroit, MI | LinkedIn</title></head></html>"
    title, company, _ = pu.parse_job_page_html(body)
    assert title == "Associate Analyst"
    assert company == "Ally"


# ---- Email waterfall: no-name path uses domain-search ----

def _hunter_stub(monkeypatch, calls):
    """Records which Hunter endpoint was hit and returns a hit from each."""
    def _get(url, params=None, timeout=None, headers=None):
        calls.append("domain-search" if "domain-search" in url else "email-finder")
        res = types.SimpleNamespace()
        if "domain-search" in url:
            res.json = lambda: {"data": {"emails": [
                {"value": "careers@ups.com", "type": "generic", "confidence": 90}]}}
        else:
            res.json = lambda: {"data": {"email": "sarah.chen@ups.com"}}
        return res
    monkeypatch.setenv("HUNTER_API_KEY", "k")
    monkeypatch.delenv("PROSPEO_API_KEY", raising=False)
    monkeypatch.delenv("GETPROSPECT_API_KEY", raising=False)
    monkeypatch.setattr(pu.requests, "get", _get)


def test_waterfall_uses_domain_search_when_no_real_name_is_known(monkeypatch):
    """A placeholder is not a person. All three finders take a first/last name, so "Operations
    Lead" guaranteed three misses and a fallback guess - the failure that made /eh look dead."""
    for placeholder in ("Operations Lead", "Hiring Manager", "Operations", ""):
        calls = []
        _hunter_stub(monkeypatch, calls)
        found = pu.resolve_email_waterfall(placeholder, "UPS", domain_hint="ups.com")
        assert calls == ["domain-search"], f"{placeholder!r} should not hit email-finder"
        assert found == "careers@ups.com"


def test_waterfall_still_uses_email_finder_for_a_real_name(monkeypatch):
    calls = []
    _hunter_stub(monkeypatch, calls)

    found = pu.resolve_email_waterfall("Sarah Chen", "UPS", domain_hint="ups.com")

    assert calls == ["email-finder"]
    assert found == "sarah.chen@ups.com"


def test_domain_search_prefers_a_generic_mailbox_over_a_personal_one(monkeypatch):
    """A role mailbox is the safer target when nobody specific has been identified."""
    def _get(url, params=None, timeout=None, headers=None):
        res = types.SimpleNamespace()
        res.json = lambda: {"data": {"emails": [
            {"value": "j.smith@ups.com", "type": "personal", "confidence": 99},
            {"value": "careers@ups.com", "type": "generic", "confidence": 50}]}}
        return res
    monkeypatch.setenv("HUNTER_API_KEY", "k")
    monkeypatch.setattr(pu.requests, "get", _get)

    assert pu._hunter_domain_search("ups.com") == "careers@ups.com"


# ---- JD vocabulary extraction ----

_SURETY_JD = """Hybrid Operations & Analytics Associate - Surety. You will reconcile bordereaux
and premium bookings, build reporting in Power BI, maintain the policy administration system,
partner with brokers and drive process improvement across the surety portfolio. Requirements:
strong analytical skills, 2+ years of experience, bachelors degree preferred. We are an equal
opportunity employer and all applicants will receive consideration without regard to race."""


def test_extract_jd_terms_surfaces_domain_vocabulary():
    """The whole point: terms Kevin's 10-word core_skills bank cannot see."""
    terms = pu.extract_jd_terms(_SURETY_JD)
    assert "surety" in terms
    assert "bordereaux" in terms
    assert "premium booking" in terms, "phrasal ops vocabulary must survive as a bigram"


def test_extract_jd_terms_keeps_protected_phrases_intact():
    terms = pu.extract_jd_terms(_SURETY_JD)
    assert "power bi" in terms, "'power bi' must not degrade into 'power' + 'bi'"
    assert "policy administration" in terms


def test_extract_jd_terms_drops_boilerplate():
    """A list padded with EEO and benefits language is useless for resume decisions."""
    terms = pu.extract_jd_terms(_SURETY_JD)
    for junk in ("experience", "requirement", "opportunity", "employer", "degree",
                 "applicant", "skill", "year", "strong", "preferred"):
        assert junk not in terms, f"boilerplate term {junk!r} leaked into the vocabulary"


def test_extract_jd_terms_bigrams_never_span_a_dropped_stopword():
    """'reporting in Power BI' must not yield the phantom bigram 'reporting power'."""
    terms = pu.extract_jd_terms("Build reporting in Power BI dashboards.")
    assert "reporting power" not in terms


def test_extract_jd_terms_counts_each_term_once_per_document():
    """Document frequency is the signal; one shouty JD must not outvote nine others."""
    terms = pu.extract_jd_terms("Reconciliation reconciliation RECONCILIATION reconciliations.")
    assert terms.count("reconciliation") == 1


def test_extract_jd_terms_singularizes_plurals():
    """'reconciliations' and 'reconciliation' must aggregate as one term."""
    assert "reconciliation" in pu.extract_jd_terms("Owns daily reconciliations for the desk.")
    assert "policy" in pu.extract_jd_terms("Reviews policies before binding.")


def test_extract_jd_terms_handles_empty_input():
    assert pu.extract_jd_terms("") == []
    assert pu.extract_jd_terms(None) == []


def test_extract_jd_terms_drops_generic_action_verbs():
    """Regression: 'build', 'drive', 'system' and 'improvement' appear in nearly every posting, so
    they outranked the domain nouns in /gaps and told Kevin nothing about what to write.
    """
    terms = pu.extract_jd_terms(
        "Build reporting, drive process improvement, manage the system, support the team."
    )
    for generic in ("build", "drive", "system", "improvement", "manage", "support"):
        assert generic not in terms, f"generic verb {generic!r} must not rank as vocabulary"


def test_extract_jd_terms_keeps_phrases_built_from_stopworded_halves():
    """'process' and 'system' are stopwords, but 'process improvement' and 'policy administration'
    are real vocabulary - the protected-phrase pass must run before tokenization."""
    terms = pu.extract_jd_terms(
        "Drive process improvement and process automation in the policy administration system."
    )
    assert "process improvement" in terms
    assert "process automation" in terms
    assert "policy administration" in terms


# ---- Dead job link detection ----

def test_classify_404_is_dead():
    """Huntington (Paradox) serves 404 on a pulled req - measured live 2026-09-21."""
    assert pu.classify_job_link("https://huntington-careers.com/x", 404, None, "")[0] == "dead"
    assert pu.classify_job_link("https://x.com/j", 410, None, "")[0] == "dead"


def test_classify_reads_the_page_text_when_the_code_is_200():
    """Some hosts serve the retirement notice behind a 200."""
    verdict, reason = pu.classify_job_link(
        "https://co.com/careers/job/5", 200, "https://co.com/careers/job/5",
        "<h1>This job is no longer available.</h1>")
    assert verdict == "dead" and "no longer available" in reason


def test_classify_never_calls_a_fetch_failure_dead():
    """A site being briefly unreachable is not a retired posting - calling it dead would bury
    rows during any outage."""
    assert pu.classify_job_link("https://x.com/j", None, None, "", fetch_error=TimeoutError())[0] == "unknown"
    assert pu.classify_job_link("https://x.com/j", 503, None, "")[0] == "unknown"


def test_classify_treats_opaque_hosts_as_unknown():
    """LinkedIn answers a server-side GET with an auth wall, so its 200 carries no information."""
    assert pu.classify_job_link("https://www.linkedin.com/jobs/view/1", 200, None, "<div/>")[0] == "unknown"
    assert pu.classify_job_link("https://x.myworkdayjobs.com/j/1", 200, None, "<div/>")[0] == "unknown"


def test_classify_still_trusts_a_404_from_an_opaque_host():
    """Opaque means its 200 is meaningless, not that it lies about 404."""
    assert pu.classify_job_link("https://www.linkedin.com/jobs/view/1", 404, None, "")[0] == "dead"


def test_classify_redirect_to_careers_root_is_dead():
    verdict, _ = pu.classify_job_link(
        "https://co.com/careers/job/5", 200, "https://co.com/careers", "<html>ok</html>")
    assert verdict == "dead"


def test_classify_live_posting_is_alive():
    verdict, _ = pu.classify_job_link(
        "https://co.com/careers/job/5", 200, "https://co.com/careers/job/5",
        "<p>Apply now. Responsibilities include reconciliation.</p>")
    assert verdict == "alive"


def test_only_matched_rows_may_auto_retire():
    """An APPLIED row is a live thread - the posting coming down is not a rejection."""
    assert pu.may_auto_retire("Matched") is True
    assert pu.may_auto_retire("matched") is True
    assert pu.may_auto_retire("Applied") is False
    assert pu.may_auto_retire("Interviewing") is False
    assert pu.may_auto_retire("") is False


def test_edit_ids_route_to_the_pool_they_name():
    """/edit writes into a bank by slot code, so a prefix collision silently corrupts the wrong
    pool. R0-R3 was added 2026-09-23 for reactivation and must not shadow the existing prefixes."""
    cases = {
        "C0": "cold_ops",
        "W5": "warm_alumni",
        "B1": "followup_bumps",
        "R0": "reactivation",
        "R3": "reactivation",
        "r2": "reactivation",          # the pattern is case-insensitive
    }
    for slot, expected_pool in cases.items():
        target = m.resolve_edit_target(slot)
        assert target is not None, f"{slot} did not resolve"
        path, pool_key, idx = target
        assert pool_key == expected_pool, f"{slot} routed to {pool_key}, expected {expected_pool}"
        assert path == m.OUTREACH_TEMPLATES_PATH
        assert idx == int(slot[1:])

    assert m.resolve_edit_target("Z0") is None
    # Every outreach pool /edit can reach must have a lint kind, or an edit ships unchecked.
    for pool_key in ("cold_ops", "warm_alumni", "followup_bumps", "reactivation"):
        assert pool_key in m._EDIT_LINT_KINDS, f"{pool_key} is editable but never linted"


def test_reactivation_fallback_bank_matches_the_shipped_one():
    """_FALLBACK_OUTREACH_TEMPLATES is what ships when the JSON fails to load. A pool present in
    one and missing from the other is the silent-zero failure this repo keeps hitting."""
    shipped = _load_bank("outreach_templates.json")
    for pool_key in shipped:
        assert pool_key in m._FALLBACK_OUTREACH_TEMPLATES, f"{pool_key} has no in-code fallback"
    fallback = m._FALLBACK_OUTREACH_TEMPLATES["reactivation"][0]
    rendered = m.interpolate_template(fallback, name="", company=_LINT_COMPANY)
    assert "I was at Signal through the summer." in rendered
    assert pu.lint_outreach_template(rendered, "email") == []


# ---- Per-command help (/cmd/) ----

def test_trailing_slash_help_never_shadows_a_real_command():
    """`/w/` explains, `/w` runs. lookup_command_help() sits ABOVE every handler in
    process_webhook_payload_async, so if it ever answered a real invocation it would silently
    disable that command. Anything without a trailing slash must return None."""
    import command_help as ch
    live = ["/t", "/w", "/e", "/eh", "/draft", "/sendall", "/help", "/edit", "/x", "/apply",
            "/promote", "/cv", "/letter", "/inbox", "/hot", "/brief", "/n", "/f"]
    for cmd in live:
        assert ch.lookup_command_help(cmd) is None, f"{cmd} was swallowed by the help handler"
        # With arguments it is unambiguously an invocation, trailing slash or not.
        assert ch.lookup_command_help(f"{cmd} some args") is None
        assert ch.lookup_command_help(f"{cmd} https://example.com/jobs/1/") is None

    # Degenerate inputs must not be read as help requests either.
    for junk in ("", "   ", "/", "//", "not a command", "plain text/"):
        assert ch.lookup_command_help(junk) is None, f"{junk!r} was read as a help request"


def test_trailing_slash_help_answers_and_resolves_aliases():
    import command_help as ch
    assert "Warm radar" in ch.lookup_command_help("/w/")
    assert ch.lookup_command_help("/W/") == ch.lookup_command_help("/w/"), "should be case-insensitive"

    # An alias resolves to its canonical entry and says so, rather than dead-ending.
    resume = ch.lookup_command_help("/resume/")
    assert "/cv" in resume and "same as" in resume

    # An unknown command still gets an answer pointing at /help, never silence.
    unknown = ch.lookup_command_help("/nosuchcommand/")
    assert "/help" in unknown


def test_every_help_entry_has_prose_and_an_example():
    """An entry that is a bare restatement of the command name is not help. This exists because
    the /help wall was already confusing - a thin entry here would be the same failure again."""
    import command_help as ch
    for cmd, (summary, example) in ch.HELP.items():
        assert cmd.startswith("/"), f"{cmd} is not a command"
        assert len(summary.split()) >= 8, f"{cmd} summary is too thin to help"
        assert example.strip(), f"{cmd} has no example"
    for alias, canonical in ch.ALIASES.items():
        assert canonical in ch.HELP, f"alias {alias} points at {canonical}, which has no entry"


def test_help_covers_the_commands_kevin_actually_applies_with():
    """/e and /letter are the go-to pair, and /edit is the one with the confusing slot codes.
    These three must always have an entry - they are why this module exists."""
    import command_help as ch
    for cmd in ("/e", "/eh", "/letter", "/cv", "/edit", "/w", "/t"):
        assert cmd in ch.HELP, f"{cmd} lost its help entry"
    # The /edit entry must name every live slot bank, since that is the actual confusion.
    edit_help = ch.lookup_command_help("/edit/")
    for code in ("C0-C7", "W0-W5", "B0-B1", "R0-R3", "L0-L9"):
        assert code in edit_help, f"/edit help does not mention {code}"


def test_no_cold_ops_entry_is_malformed_with_an_empty_their_desk():
    """cold_ops[2] shipped "{their_desk}, so I would love 15 minutes" to Koch.

    their_desk is EMPTY on every automated send (generate_cold_email passes ""), so
    interpolate_template supplied its fallback and the email read "Given how much of this sits
    under you, so I would love 15 minutes" - not a sentence. The pass-2 path was broken the same
    way, because their_desk renders a SUBORDINATE clause by contract: "Since employee care runs on
    third-party administrators, so I would love 15 minutes". So this asserts BOTH renderings, not
    just the fallback one - fixing only the fallback would have left the filled path ungrammatical.
    """
    for idx, template in enumerate(_load_bank("outreach_templates.json")["cold_ops"]):
        for label, desk in (
            ("empty", ""),
            ("pass-2 filled", "Since employee care runs on third-party administrators and HRIS records"),
        ):
            rendered = m.sanitize_text(m.interpolate_template(
                template, name="", company=_LINT_COMPANY, job_title=_LINT_TITLE, their_desk=desk))
            ctx = f"cold_ops[{idx}] ({label})"
            assert "{" not in rendered, f"{ctx} left a placeholder uninterpolated"
            # The exact sentence that shipped.
            assert ", so I would love" not in rendered, ctx
            for sentence in re.split(r"(?<=[.!?])\s+", rendered):
                assert not re.match(r"^(Given|Since|Because|Although|While)\b[^,]*,\s*(so|but|and|yet)\b",
                                    sentence.strip(), re.IGNORECASE), f"{ctx}: {sentence.strip()!r}"
            assert pu.lint_outreach_template(rendered, "email") == [], ctx


def test_the_linter_catches_a_subordinate_clause_coordinated_with_a_conjunction():
    """The guard behind the test above. Without this rule the collision is invisible to the suite:
    linting the RAW template only sees "{their_desk}, so ...", and the placeholder hides it."""
    assert any("subordinate" in v for v in pu.lint_outreach_template(
        "Given how much of this sits under you, so I would love 15 minutes.", "email"))
    assert any("subordinate" in v for v in pu.lint_outreach_template(
        "Since the queue sits with you, but I would love 15 minutes.", "email"))
    # A main clause with a legitimate result clause is correct English and must stay clean - this
    # is cold_ops' own second sentence, which a looser rule flagged.
    assert pu.lint_outreach_template(
        "My background is in client intake and onboarding paperwork, keeping account records "
        "accurate so nothing stalls downstream.", "email") == []
    assert pu.lint_outreach_template(
        "I saw you were hiring for this role, and I would love 15 minutes of your time.", "email") == []
    # A correct subordinate clause followed by a later, legitimate coordination.
    assert pu.lint_outreach_template(
        "Given how much of this sits under you, I would love 15 minutes, and I can work "
        "around your schedule.", "email") == []


# --- Inbound sender screen (2026-09-26) -----------------------------------------------------------
# The 8 senders that reached Kevin's phone as "Unverified Reply" between 2026-09-24 and 09-26.
_SCREEN_ATS = ("myworkday.com", "greenhouse.io", "successfactors.com")
_TELEGRAM_JUNK_SENDERS = [
    ("invoice+statements@mail.anthropic.com", "automated sender"),
    ("community@legal.io", "automated sender"),
    ("discover@services.discover.com", "automated sender"),
    ("discover@card-e.em.discover.com", "relay subdomain"),
    ("support@email.career.io", "automated sender"),
    ("system@successfactors.com", "automated ATS mail"),
    ("indeedapply@indeed.com", "automated sender"),
    ("ejko.fa.sender.2@workflow.mail.us2.cloud.oracle.com", "automated sender"),
]


@pytest.mark.parametrize("sender,reason", _TELEGRAM_JUNK_SENDERS)
def test_every_junk_sender_off_kevins_phone_is_screened(sender, reason):
    assert pu.inbound_sender_screen_reason(sender, _SCREEN_ATS) == reason


def test_a_cold_recruiter_with_no_interview_vocabulary_is_not_screened():
    """The case Kevin refused to lose. If a rule screens her, the rule is wrong."""
    for sender in ("Sarah Chen <sarah.chen@sanctuarywealth.com>", "astemler@nextpathcp.com",
                   "jane@gmail.com", "careers@acme.com", "recruiting@acme.com", "hr@acme.com"):
        assert pu.inbound_sender_screen_reason(sender, _SCREEN_ATS) == "", sender


def test_an_ats_robot_passes_only_with_a_hiring_verdict():
    """Workday sends real invitations and rejections from noreply@; it also sends account setup."""
    assert pu.inbound_sender_screen_reason("noreply@myworkday.com", _SCREEN_ATS, hiring_verdict=True) == ""
    assert pu.inbound_sender_screen_reason("noreply@myworkday.com", _SCREEN_ATS) == "automated ATS mail"
    # A named person at an ATS domain is never screened.
    assert pu.inbound_sender_screen_reason("pat@greenhouse.io", _SCREEN_ATS) == ""


def test_plus_addressing_is_stripped_before_the_localpart_check():
    assert pu.is_automated_sender("invoice+statements@anthropic.com") is True


def test_a_generic_domain_label_cannot_match_a_company_that_ends_in_it():
    """'CRM Match: Discover @ G-TECH Services' - label 'services' was a substring of the company."""
    assert pu.domain_matches_company("discover@services.discover.com", "G-TECH Services") is False
    assert pu.domain_matches_company("a@solutions.com", "Acme Solutions") is False
    assert pu.domain_matches_company("a@signaladvisors.com", "Signal Advisors") is True
    assert pu.domain_matches_company("a@ford.com", "Ford Motor Company") is True
    assert pu.domain_matches_company("a@intactinsurance.com", "Intact Services USA LLC") is True
    assert pu.domain_matches_company("a@ncms.org", "National Center for Manufacturing Sciences") is True
    assert pu.company_domain_of("discover@services.discover.com") == "discover.com"


def test_classify_jobleads_offline_page_is_dead_even_on_a_200():
    """JobLeads serves "taken offline" under a 404 today. If they switch to a 200, the phrase is
    the only thing standing between a dead posting and an 'alive' verdict."""
    verdict, reason = pu.classify_job_link(
        "https://www.jobleads.com/us/job/ops-specialist--x", 200,
        "https://www.jobleads.com/us/job/ops-specialist--x",
        "<p>Unfortunately, this job has recently been taken offline.</p>")
    assert verdict == "dead" and "taken offline" in reason


def test_classify_202_is_opaque_never_dead():
    """career.io answers with a 202 JS shell - measured 2026-09-26. Permanent, not transient."""
    for url in ("https://career.io/job/associate-ops-analyst-x", "https://co.com/careers/job/5"):
        verdict, reason = pu.classify_job_link(url, 202, url, "<div id=root></div>")
        assert verdict == "unknown", url
        assert pu.is_opaque_link_reason(reason), reason


def test_classify_opaque_host_auth_wall_is_opaque_not_transient():
    """Indeed 401 / ZipRecruiter 403 (measured) are a blind spot that tomorrow will not fix."""
    verdict, reason = pu.classify_job_link("https://www.indeed.com/viewjob?jk=1", 401, None, "")
    assert verdict == "unknown" and pu.is_opaque_link_reason(reason)
    verdict, reason = pu.classify_job_link("https://www.linkedin.com/jobs/view/1", 200, None, "<div/>")
    assert verdict == "unknown" and pu.is_opaque_link_reason(reason)


def test_classify_transient_misses_are_not_opaque():
    for args, kw in (((None, None, ""), {"fetch_error": TimeoutError()}),
                     ((503, None, ""), {}), ((403, None, ""), {})):
        verdict, reason = pu.classify_job_link("https://co.com/j", *args, **kw)
        assert verdict == "unknown" and not pu.is_opaque_link_reason(reason), reason


def test_classify_opaque_hosts_never_dead_on_a_plain_200():
    for url in ("https://www.linkedin.com/jobs/view/4412864916",
                "https://www.indeed.com/viewjob?jk=abc", "https://www.ziprecruiter.com/c/x/Job/y"):
        assert pu.classify_job_link(url, 200, "https://www.linkedin.com/", "<p>Sign in</p>")[0] != "dead", url


def test_split_link_check_budget_never_starves_a_tab():
    """THE BUG: one global counter let a 50-row Tetiana Cold eat all 40 and leave TW/CL at zero."""
    assert pu.split_link_check_budget([50, 9, 3], 40) == [28, 9, 3]
    assert pu.split_link_check_budget([50, 50, 50], 40) == [14, 13, 13]
    assert pu.split_link_check_budget([22, 0, 0], 40) == [22, 0, 0]
    assert pu.split_link_check_budget([5, 5, 5], 0) == [0, 0, 0]
    assert sum(pu.split_link_check_budget([100, 100, 1], 40)) == 40


# ==============================================================================
# Public dashboard shaping
# ==============================================================================

def test_weekly_volume_series_starts_at_first_observed_week_and_zero_fills_after():
    rows = [("2026-09-02", "ai_screened", 3), ("2026-09-20", "ai_screened", 2),
            ("2026-09-21", "listing_discovered", 5)]
    series = pu.weekly_volume_series(rows, 12, date(2026, 9, 26))
    assert [w["week"] for w in series] == ["2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21"]
    assert series[1] == {"week": "2026-09-07", "ai_screened": 0, "listing_discovered": 0}
    assert series[2]["ai_screened"] == 2  # Sunday 09-20 belongs to the week of Monday 09-14


def test_weekly_volume_series_is_empty_without_rows():
    assert pu.weekly_volume_series([], 12, date(2026, 9, 26)) == []


def test_funnel_rates_are_none_without_applications():
    assert pu.funnel_rates(0, 0, 0, 0) == {"reply_rate_pct": None, "interview_rate_pct": None,
                                        "offer_rate_pct": None}
    assert pu.funnel_rates(50, 2, 0, 0)["reply_rate_pct"] == 4.0


def test_shape_public_dashboard_keeps_failed_reads_as_none():
    shaped = pu.shape_public_dashboard({"today": date(2026, 9, 26)})
    for key in ("roles_screened", "applications_sent", "live_conversations", "dead_links",
                "median_fit_score", "drafting", "funnel"):
        assert shaped[key] is None, key
    assert shaped["timeline"] == []


def test_shape_public_dashboard_counts_dead_links_this_week_and_retired():
    today = date(2026, 9, 26)
    dead = [(None,) * 6 + (1, "2026-09-25 08:00:00"), (None,) * 6 + (0, "2026-09-01 08:00:00")]
    shaped = pu.shape_public_dashboard({"today": today, "dead_links": dead})
    assert shaped["dead_links"] == {"detected": 2, "retired": 1, "this_week": 1}
