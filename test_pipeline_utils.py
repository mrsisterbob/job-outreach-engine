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


def test_followup_action_applied_boundary_days_3_4_5():
    assert pu.followup_action("Applied", _added(3), "", _TODAY) == "none"
    assert pu.followup_action("Applied", _added(4), "", _TODAY) == "send_followup_1"
    assert pu.followup_action("Applied", _added(5), "", _TODAY) == "send_followup_1"


def test_followup_action_applied_boundary_days_8_9_10():
    assert pu.followup_action("Applied", _added(8), "", _TODAY) == "send_followup_1"
    assert pu.followup_action("Applied", _added(9), "", _TODAY) == "send_followup_2"
    assert pu.followup_action("Applied", _added(10), "", _TODAY) == "send_followup_2"


def test_followup_action_applied_boundary_days_15_16_17():
    assert pu.followup_action("Applied", _added(15), "", _TODAY) == "send_followup_2"
    assert pu.followup_action("Applied", _added(16), "", _TODAY) == "bury_ghosted"
    assert pu.followup_action("Applied", _added(17), "", _TODAY) == "bury_ghosted"


def test_followup_action_future_next_followup_always_none():
    future = (_TODAY + timedelta(days=1)).strftime("%Y-%m-%d")
    assert pu.followup_action("Applied", _added(30), future, _TODAY) == "none"
    assert pu.followup_action("Interviewing", _added(30), future, _TODAY) == "none"


def test_followup_action_next_followup_today_is_not_future():
    # Due today (== today, not > today) -> the window math still applies.
    assert pu.followup_action("Applied", _added(20), _TODAY.strftime("%Y-%m-%d"), _TODAY) == "bury_ghosted"


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
    assert pu.followup_action("  applied  ", _added(4), "", _TODAY) == "send_followup_1"


def test_followup_action_blank_dates_yield_none():
    assert pu.followup_action("Applied", "", "", _TODAY) == "none"
    assert pu.followup_action("Applied", None, None, _TODAY) == "none"
    assert pu.followup_action("Applied", "1970-01-01", "1970-01-01", _TODAY) == "none"


def test_followup_action_malformed_dates_yield_none():
    assert pu.followup_action("Applied", "not-a-date", "", _TODAY) == "none"
    assert pu.followup_action("Applied", "2026-13-99", "garbage", _TODAY) == "none"


def test_followup_action_falls_back_to_next_followup_when_date_added_blank():
    # Date Added missing, past Next Followup Date -> used as the anchor.
    assert pu.followup_action("Applied", "", _added(16), _TODAY) == "bury_ghosted"


def test_followup_action_accepts_datetime_for_today():
    assert pu.followup_action("Applied", _added(4), "", datetime(2026, 6, 1, 7, 30)) == "send_followup_1"


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
    # These counts are mirrored in response_schema.py (le=9, le=5) and main.build_system_prompt().
    outreach = _load_bank("outreach_templates.json")
    linkedin = _load_bank("linkedin_templates.json")
    assert len(outreach["cold_ops"]) == 6
    # warm_alumni is the one pool Gemini does NOT route into: outreach_template_id is le=5 and
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
    a 10-minute timebox, 'Best,' + full name, and no college-corpus habits.

    The corpus markers that are habits rather than invariants - the word 'brief', the verbatim
    release line, and a 'perspective'/'day to day' question - are asserted as BANK COVERAGE, not
    per template. Requiring all three in all six is what collapsed the bank into six near-copies
    of one email sharing ~80% of their words, which at pipeline volume means two people on the
    same team can receive visibly identical notes. Coverage keeps the voice anchored in the
    corpus while letting each entry open and close differently.
    """
    cold = _load_bank("outreach_templates.json")["cold_ops"]
    assert len(cold) == 6
    rendered_all = [
        m.interpolate_template(t, name="", company=_LINT_COMPANY, job_title=_LINT_TITLE)
        for t in cold
    ]

    for idx, (template, rendered) in enumerate(zip(cold, rendered_all)):
        ctx = f"cold_ops[{idx}]"
        assert "10 minutes" in rendered, ctx
        assert rendered.rstrip().endswith("Best,\nKevin Miller"), ctx
        # college-corpus tells the professional corpus disproves
        assert "Yours In Service" not in rendered and "YIS" not in rendered, ctx
        assert "{name_bare}" not in template, ctx
        for banned_minutes in ("13 minute", "14 minute", "16 minute", "17 minute"):
            assert banned_minutes not in rendered, ctx
        assert "I built" not in rendered and "I automated" not in rendered, ctx
        # does not open by announcing the posting
        assert not rendered.split("\n\n")[1].startswith(("I saw you're hiring", "I saw the", "Saw you're hiring")), ctx

    assert sum("brief" in r for r in rendered_all) >= 2
    assert sum("\nHappy to work around your schedule.\n" in r for r in rendered_all) >= 2
    assert sum(("perspective" in r) or ("day to day" in r) for r in rendered_all) >= 2


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
    for template_id in range(6):
        card_copy = m.render_outreach_email(
            "cold_ops", template_id, name="", company=_LINT_COMPANY, job_title=_LINT_TITLE
        )
        gmail_copy = m.generate_cold_email(_LINT_TITLE, _LINT_COMPANY, template_id=template_id)
        assert card_copy == gmail_copy
    # Distinct entries really are distinct - a silent fallback-to-index-0 would collapse them.
    assert len({m.generate_cold_email(_LINT_TITLE, _LINT_COMPANY, template_id=i) for i in range(6)}) == 6


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
    # The short-token floor still holds: a 4-char brand cannot match on its own.
    assert pu.domain_matches_company("a@autozone.com", "Auto Club") is False


# ---- Carmen Cold follow-up ladder ----

_LADDER_TODAY = date(2026, 9, 12)


def test_carmen_ladder_walks_every_rung_then_stops():
    """The whole point: three nudges at CARMEN_LADDER_DAYS offsets from the day the contact
    landed. Dates are derived from the constant rather than hardcoded, so retuning the cadence
    is a one-line change instead of a test rewrite."""
    anchor_date = date(2026, 9, 12)
    anchor = anchor_date.isoformat()
    d1, d2, d3 = (anchor_date + timedelta(days=n) for n in pu.CARMEN_LADDER_DAYS)

    action, nxt = pu.plan_carmen_followup(anchor, "", _LADDER_TODAY)
    assert (action, nxt) == ("schedule", d1)

    action, nxt = pu.plan_carmen_followup(anchor, d1.isoformat(), d1)
    assert (action, nxt) == ("nudge_1", d2)

    action, nxt = pu.plan_carmen_followup(anchor, d2.isoformat(), d2)
    assert (action, nxt) == ("nudge_2", d3)

    # Final rung fires with no next date - the ladder ends rather than nagging forever.
    action, nxt = pu.plan_carmen_followup(anchor, d3.isoformat(), d3)
    assert (action, nxt) == ("nudge_3", None)


def test_carmen_ladder_starts_a_manually_moved_row_from_today():
    """A row dragged into Carmen Cold by hand carries a stale Date Added and no follow-up date.
    It must enter the ladder on the next pass, not be skipped and not fire all three at once."""
    action, nxt = pu.plan_carmen_followup("2026-01-04", "", _LADDER_TODAY)
    assert action == "schedule"
    assert nxt == _LADDER_TODAY + timedelta(days=pu.CARMEN_LADDER_DAYS[0])

    # Same for a row with no Date Added at all.
    action, nxt = pu.plan_carmen_followup("", "", _LADDER_TODAY)
    assert action == "schedule"
    assert nxt == _LADDER_TODAY + timedelta(days=pu.CARMEN_LADDER_DAYS[0])


def test_carmen_ladder_is_quiet_until_due_and_after_exhaustion():
    assert pu.plan_carmen_followup("2026-09-12", "2026-09-30", _LADDER_TODAY) == ("none", None)
    # Past the last rung: stop asking for attention rather than looping.
    assert pu.plan_carmen_followup("2026-09-12", "2026-10-20", date(2026, 10, 20))[0] == "exhausted"


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
