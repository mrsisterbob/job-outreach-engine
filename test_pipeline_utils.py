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
             "doing well. Would you be open to a brief 15-minute call?\n\nBest regards,\nKevin Miller")
    violations = " | ".join(pu.lint_outreach_template(stiff, "email"))
    assert "Best regards" in violations
    assert "alignment" in violations
    assert "Hi there" in violations
    assert "15-minute" in violations
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
    assert any("over the 75-word" in v for v in pu.lint_outreach_template(long_email, "email"))
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
    assert len(outreach["warm_alumni"]) == 2
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
    # The card never knows the recipient's name, so the old "Hi there," default is gone.
    for template in _load_bank("outreach_templates.json")["cold_ops"]:
        assert m.interpolate_template(template, name="", company=_LINT_COMPANY, job_title=_LINT_TITLE).startswith("Hi,")
        assert m.interpolate_template(template, name="Dana", company=_LINT_COMPANY, job_title=_LINT_TITLE).startswith("Hi Dana,")


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

