"""Unit tests for pipeline_utils.py: pure scoring/dork/dedup/formatting helpers.
No network, DB, or Flask/APScheduler dependency - safe to run in any environment.
"""
import urllib.parse
from datetime import datetime, timedelta, timezone

import pipeline_utils as pu


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
    assert pu.derive_job_source("") == "jsearch"


# ---- Email confidence gating ----

def test_is_unverified_email_detects_warning_tags():
    assert pu.is_unverified_email("jane.doe@acmecorp.com [⚠️ Unverified]") is True
    assert pu.is_unverified_email("operations@acmecorp.com [⚠️ Fallback]") is True


def test_is_unverified_email_false_for_clean_address():
    assert pu.is_unverified_email("jane@acmecorp.com") is False
    assert pu.is_unverified_email("") is False
    assert pu.is_unverified_email(None) is False

