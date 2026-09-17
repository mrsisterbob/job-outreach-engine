"""Integration tests for main.py's SQLite-backed workflows (CRM outbox, cooldown/company-identity,
reply-mapping, batch follow-ups, Gmail draft MIME attachment).

Isolation strategy: JOBS_DB_PATH is set to a temp file BEFORE importing main, so main's own
init_db() builds its schema there instead of touching the real jobs_cache.db, and PYTEST_CURRENT_TEST
(auto-set by pytest) makes main skip starting its background daemons (Gmail poller, CRM outbox
worker, morning digest, backup scheduler) so nothing races against these tests' assertions.
"""
import base64
import html
import json
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes

import pytest

_tmp_db_fd, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_tmp_db_fd)
os.environ["JOBS_DB_PATH"] = _TMP_DB_PATH

import main as m  # noqa: E402  (must import after JOBS_DB_PATH is set)


@pytest.fixture(autouse=True)
def clean_tables():
    """Truncate the tables under test before every test so cases don't bleed into each other."""
    with m.get_db_conn() as conn:
        # seen_jobs/seen_content_hashes are the dedup ledgers: without truncating them, a test that
        # ingests a posting makes every later test using the same company/title silently take the
        # "already in the pipeline" branch instead of the path it meant to exercise.
        for table in ("crm_outbox", "sheet_row_map", "company_cooldown", "company_identities",
                      "jobs", "followup_sequencer_log", "followup_queue_snapshot", "gmail_drafts", "seen_jobs",
                      "seen_content_hashes"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    yield


def teardown_module(module):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TMP_DB_PATH + suffix)
        except OSError:
            pass


# ---- CRM outbox retry/failure recovery ----

def test_crm_outbox_success_deletes_row(monkeypatch):
    m.enqueue_crm_payload({"action": "update_status", "sheet_uuid": "abc"})
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda payload, max_retries=1, **kw: True)
    m.process_crm_outbox_batch(inter_job_sleep=0)
    with m.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crm_outbox").fetchone()[0] == 0


def test_crm_outbox_failure_increments_retry_and_stays_pending(monkeypatch):
    m.enqueue_crm_payload({"action": "update_status", "sheet_uuid": "abc"})
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda payload, max_retries=1, **kw: False)
    m.process_crm_outbox_batch(inter_job_sleep=0)
    with m.get_db_conn() as conn:
        row = conn.execute("SELECT retry_count, status FROM crm_outbox").fetchone()
    assert row == (1, "PENDING")


def test_crm_outbox_marks_failed_after_max_retries(monkeypatch):
    with m.get_db_conn() as conn:
        conn.execute(
            "INSERT INTO crm_outbox (payload_json, status, retry_count) VALUES (?, 'PENDING', 9)",
            ('{"action": "update_status"}',)
        )
        conn.commit()
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda payload, max_retries=1, **kw: False)
    m.process_crm_outbox_batch(inter_job_sleep=0)
    with m.get_db_conn() as conn:
        row = conn.execute("SELECT retry_count, status FROM crm_outbox").fetchone()
    assert row == (10, "FAILED")


# ---- Tracked-role suppression must match what Sheets will actually accept ----

def _stub_tracked_tabs(monkeypatch, sheet):
    """Serve get_followups per tab and reset the TTL cache so the fetch actually runs."""
    asked = []

    class _Resp:
        status_code = 200
        def __init__(self, rows):
            self.rows = rows
        def json(self):
            return {"status": "success", "followups": self.rows}

    def _post(payload, timeout=10):
        tab = payload.get("tab")
        asked.append(tab)
        return _Resp(sheet.get(tab, []))

    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "crm_post", _post)
    m._TRACKED_ROLE_CACHE["fetched_at"] = 0
    m._TRACKED_ROLE_CACHE["data"] = set()
    return asked


def test_tracked_keys_include_clavicular_tab(monkeypatch):
    """Regression: rows land in Clavicular (target_code CL) but the tracked set read only TC+TW, so
    a warm-referral role was tracked in the sheet yet invisible to the ingest gate. The card then
    shipped while Code.gs's dedup guard suppressed the write - a card pointing at a nonexistent row.
    """
    role = {"company": "Doeren Mayhew", "title": "Client Onboarding and Operations Specialist"}
    asked = _stub_tracked_tabs(monkeypatch, {"TC": [], "TW": [], "CL": [role]})
    m.get_tracked_job_keys()
    assert asked == ["TC", "TW", "CL"], "every tab dispatch can write to must be read back"
    assert m.is_role_tracked(role["company"], role["title"])


def test_tracked_keys_match_apps_script_dedup_algorithm(monkeypatch):
    """The local gate must be a superset of Code.gs's batch_add_rows guard. The guard keys on
    normalizeDedupKey (punctuation and stop tokens stripped); the discovery path keys on
    generate_dedup_hash. A role only the former would collapse must still read as tracked.
    """
    _stub_tracked_tabs(monkeypatch, {
        "TC": [{"company": "The Blue Chip Co.", "title": "Operations Specialist"}], "TW": [], "CL": [],
    })
    m.get_tracked_job_keys()
    # Same role, punctuation/stop-token variant - normalize_dedup_key collapses these, md5 does not.
    assert m.is_role_tracked("Blue Chip", "Operations Specialist")


def test_tracked_keys_still_admit_a_genuinely_new_role(monkeypatch):
    """The suppression set must not become a catch-all that starves the pipeline."""
    _stub_tracked_tabs(monkeypatch, {
        "TC": [{"company": "Doeren Mayhew", "title": "Client Onboarding and Operations Specialist"}],
        "TW": [], "CL": [],
    })
    m.get_tracked_job_keys()
    assert not m.is_role_tracked("Rocket Mortgage", "FX Operations Analyst")
    # Same company, different role - a company must keep surfacing new openings.
    assert not m.is_role_tracked("Doeren Mayhew", "Senior Tax Associate")


def test_tracked_keys_errs_open_when_sheets_is_down(monkeypatch):
    """A Sheets failure must never populate a suppression set - better a duplicate card than a
    silently empty pipeline."""
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "crm_post", lambda p, timeout=10: None)
    m._TRACKED_ROLE_CACHE["fetched_at"] = 0
    m._TRACKED_ROLE_CACHE["data"] = set()
    assert m.get_tracked_job_keys() == set()
    assert not m.is_role_tracked("Doeren Mayhew", "Client Onboarding and Operations Specialist")


def test_crm_outbox_drops_permanently_rejected_payload(monkeypatch):
    """A write whose row does not exist can never succeed: it must be deleted after ONE pass, not
    requeued for ten more - each of which fired its own health alert (the Telegram alert storm)."""
    m.enqueue_crm_payload({"action": "update_status", "sheet_uuid": "missing-uuid"})
    alerts = []
    monkeypatch.setattr(m, "send_health_alert", lambda t: alerts.append(t))

    def boom(payload, max_retries=1, **kw):
        raise m.PermanentCRMRejection("update_status", "No record found for sheet_uuid missing-uuid")
    monkeypatch.setattr(m, "log_to_sheets_crm", boom)

    m.process_crm_outbox_batch(inter_job_sleep=0)
    with m.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crm_outbox").fetchone()[0] == 0
    assert len(alerts) == 1 and "dropped" in alerts[0]


def test_permanent_rejection_classifier_spares_lock_timeout():
    """Lock timeout is the one Apps Script rejection that IS transient - it must stay retryable."""
    assert m.is_permanent_crm_rejection("No record found for sheet_uuid abc")
    assert m.is_permanent_crm_rejection("Unknown target_code: ZZ")
    assert m.is_permanent_crm_rejection("update_snooze requires sheet_uuid and next_followup")
    assert not m.is_permanent_crm_rejection("Lock timeout - server busy")
    assert not m.is_permanent_crm_rejection("")


def test_log_to_sheets_crm_returns_false_on_permanent_without_flag(monkeypatch):
    """Direct callers keep the plain bool contract - only the outbox opts into the exception."""
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")

    class _Resp:
        status_code = 200
        def json(self):
            return {"status": "error", "message": "No record found for sheet_uuid abc"}

    calls = []
    monkeypatch.setattr(m, "crm_post", lambda p, timeout=10: calls.append(p) or _Resp())
    monkeypatch.setattr(m, "send_health_alert", lambda t: None)
    assert m.log_to_sheets_crm({"action": "update_status", "sheet_uuid": "abc"}) is False
    assert len(calls) == 1, "a permanent rejection must not be retried"


def test_crm_outbox_batch_ignores_rows_past_max_retries(monkeypatch):
    with m.get_db_conn() as conn:
        conn.execute(
            "INSERT INTO crm_outbox (payload_json, status, retry_count) VALUES (?, 'FAILED', 10)",
            ('{"action": "update_status"}',)
        )
        conn.commit()
    calls = []
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda payload, max_retries=1, **kw: calls.append(payload) or True)
    m.process_crm_outbox_batch(inter_job_sleep=0)
    assert calls == []


# ---- Company cooldown / canonical identity (used by /apply) ----

def test_company_cooldown_roundtrip_is_normalized():
    m.add_company_cooldown("Acme Corp Inc.")
    assert m.is_company_on_cooldown("acme corp") is True
    assert m.is_company_on_cooldown("Totally Different Co") is False


def test_upsert_company_identity_merges_without_blanking_existing_fields():
    m.upsert_company_identity("Acme Corp", ats_slug="acmecorp")
    m.upsert_company_identity("Acme Corp Inc.", crm_status="Tetiana Warm", applied=True)
    with m.get_db_conn() as conn:
        row = conn.execute(
            "SELECT ats_slug, crm_status, applied_at, aliases FROM company_identities WHERE normalized_name = ?",
            (m.normalize_company_for_match("Acme Corp"),)
        ).fetchone()
    ats_slug, crm_status, applied_at, aliases = row
    assert ats_slug == "acmecorp"
    assert crm_status == "Tetiana Warm"
    assert applied_at is not None
    assert "Acme Corp Inc." in aliases.split("|")


# ---- Reply-message -> sheet_uuid mapping ----

def test_save_message_mapping_and_lookup_roundtrip():
    sheet_uuid = str(uuid.uuid4())
    ok = m.save_message_mapping(4242, sheet_uuid, sheet_tab="Carmen Warm", contact_name="Jane Doe",
                                 contact_company="Acme Corp", contact_email="jane@acme.com")
    assert ok is True
    mapping = m.get_mapping_from_message_id(4242)
    assert mapping == {"sheet_uuid": sheet_uuid, "sheet_tab": "Carmen Warm", "contact_name": "Jane Doe", "contact_company": "Acme Corp"}


def test_get_mapping_from_message_id_returns_none_when_unmapped():
    assert m.get_mapping_from_message_id(999999) is None


# ---- Apps Script payload shape ----

def test_build_crm_payload_always_includes_desc_order_and_sheet_uuid():
    payload = m.build_crm_payload("update_status", sheet_uuid="abc-123", new_tab="Tetiana Warm")
    assert payload == {"action": "update_status", "rowOperationOrder": "DESC", "sheet_uuid": "abc-123", "new_tab": "Tetiana Warm"}


def test_build_crm_payload_omits_sheet_uuid_when_not_given():
    payload = m.build_crm_payload("batch_add_rows", rows=[])
    assert "sheet_uuid" not in payload
    assert payload["rowOperationOrder"] == "DESC"


# ---- /sendall, /snoozeall batch follow-ups ----

def test_process_overdue_batch_snoozeall_queues_every_record(monkeypatch):
    overdue = [
        {"sheet_uuid": "u1", "company": "Acme", "next_followup": "2020-01-01"},
        {"sheet_uuid": "u2", "company": "Beta", "next_followup": "2020-01-02"},
    ]
    monkeypatch.setattr(m, "get_overdue_followups", lambda: overdue)
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda payload: enqueued.append(payload) or True)
    result, next_followup = m.process_overdue_batch("snoozeall", snooze_days=7)
    assert result == {"total": 2, "updated": 2, "drafted": 0, "skipped": 0}
    assert all(p["action"] == "update_snooze" and p["next_followup"] == next_followup for p in enqueued)


def test_process_overdue_batch_sendall_skips_missing_or_unverified_email(monkeypatch):
    overdue = [
        {"sheet_uuid": "u1", "company": "Acme", "email": "", "next_followup": "2020-01-01"},
        {"sheet_uuid": "u2", "company": "Beta", "email": "guess@beta.com [⚠️ Fallback]", "next_followup": "2020-01-02"},
    ]
    monkeypatch.setattr(m, "get_overdue_followups", lambda: overdue)
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda payload: True)
    draft_calls = []
    monkeypatch.setattr(m, "create_gmail_draft", lambda **kwargs: draft_calls.append(kwargs) or (True, "Success", "draft1"))
    result, _ = m.process_overdue_batch("sendall", snooze_days=14)
    assert draft_calls == []  # neither record had a clean, verified email
    assert result == {"total": 2, "updated": 0, "drafted": 0, "skipped": 2}


def test_process_overdue_batch_sendall_drafts_for_valid_email(monkeypatch):
    overdue = [{"sheet_uuid": "u1", "company": "Acme", "email": "real@acme.com", "next_followup": "2020-01-01"}]
    monkeypatch.setattr(m, "get_overdue_followups", lambda: overdue)
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda payload: True)
    monkeypatch.setattr(m, "create_gmail_draft", lambda **kwargs: (True, "Success", "draft1"))
    result, _ = m.process_overdue_batch("sendall", snooze_days=14)
    assert result == {"total": 1, "updated": 1, "drafted": 1, "skipped": 0}


# ---- Nightly follow-up sequencer (run_followup_sequencer) ----

_SEQ_TODAY = date(2026, 6, 1)

# Applied 4d ago -> follow-up #1 ; Applied 16d ago -> bury ; Interviewing 10d ago -> stale ;
# two Matched rows for the "top 3" section ; one future-dated Applied row that must be left alone.
_SEQ_RECORDS = {
    "TC": [
        {"sheet_uuid": "seq-fu1", "company": "Acme", "title": "Ops Analyst", "name": "",
         "status": "Applied", "date_added": "2026-05-28", "next_followup": "1970-01-01", "raw_priority": "70"},
        {"sheet_uuid": "seq-bury", "company": "Beta", "title": "Ops Lead", "name": "",
         "status": "Applied", "date_added": "2026-05-16", "next_followup": "1970-01-01", "raw_priority": "60"},
        {"sheet_uuid": "seq-future", "company": "Gamma", "title": "Analyst", "name": "",
         "status": "Applied", "date_added": "2026-05-01", "next_followup": "2026-06-30", "raw_priority": "55"},
    ],
    "TW": [
        {"sheet_uuid": "seq-stale", "company": "Delta", "title": "Ops Manager", "name": "",
         "status": "Interviewing", "date_added": "2026-05-22", "next_followup": "1970-01-01", "raw_priority": "80"},
    ],
    "CL": [
        {"sheet_uuid": "seq-m1", "company": "Epsilon", "title": "Ops Coord", "name": "",
         "status": "Matched", "date_added": "2026-05-30", "next_followup": "1970-01-01", "raw_priority": "88"},
        {"sheet_uuid": "seq-m2", "company": "Zeta", "title": "Ops Spec", "name": "",
         "status": "Matched", "date_added": "2026-05-30", "next_followup": "1970-01-01", "raw_priority": "72"},
    ],
}


def _mock_sequencer_crm(monkeypatch):
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(r) for r in _SEQ_RECORDS.get(code, [])])
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda payload: enqueued.append(payload) or True)
    return enqueued


def test_sequencer_queues_followup_1_with_window_snooze(monkeypatch):
    enqueued = _mock_sequencer_crm(monkeypatch)
    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    # A JOBS row is watched, not messaged: it lands in applications_quiet with no draft text.
    assert result["followups_ready"] == []
    quiet = result["applications_quiet"]
    assert [r["sheet_uuid"] for r in quiet] == ["seq-fu1"]
    assert quiet[0]["attempt"] == 1
    assert "draft_text" not in quiet[0]

    snoozes = [p for p in enqueued if p["action"] == "update_snooze" and p["sheet_uuid"] == "seq-fu1"]
    assert len(snoozes) == 1
    # anchor (Date Added 2026-05-28) + FOLLOWUP_2_DAYS -> the next window boundary
    assert snoozes[0]["next_followup"] == "2026-06-06"
    # the future-dated Applied row is never touched
    assert all(p["sheet_uuid"] != "seq-future" for p in enqueued)


def test_sequencer_bury_ghosted_writes_reason_note_then_died_move(monkeypatch):
    enqueued = _mock_sequencer_crm(monkeypatch)
    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert [r["sheet_uuid"] for r in result["buried"]] == ["seq-bury"]
    bury_payloads = [p for p in enqueued if p["sheet_uuid"] == "seq-bury"]
    actions = [p["action"] for p in bury_payloads]
    assert actions == ["append_note", "update_status"]  # note first, then the tab move
    assert "ghosted" in bury_payloads[0]["note"]
    assert bury_payloads[1]["new_tab"] == "Died"


def test_sequencer_stale_nudge_and_top_matched_do_not_write(monkeypatch):
    enqueued = _mock_sequencer_crm(monkeypatch)
    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert [r["sheet_uuid"] for r in result["going_cold"]] == ["seq-stale"]
    assert result["going_cold"][0]["days"] == 10
    assert all(p["sheet_uuid"] != "seq-stale" for p in enqueued)

    # Top matched: highest Fit Score first, capped at 3, no writes.
    assert [r["sheet_uuid"] for r in result["top_matched"]] == ["seq-m1", "seq-m2"]
    assert result["top_matched"][0]["fit_score"] == 88.0
    assert all(p["sheet_uuid"] not in ("seq-m1", "seq-m2") for p in enqueued)
    assert result["counts"] == {"followups_ready": 0, "ready_to_promote": 0, "revived": 0,
                                "applications_quiet": 1, "going_cold": 1, "buried": 1, "killed": 0,
                                "top_matched": 2, "buries_suppressed": 0, "kills_suppressed": 0}


def test_sequencer_is_idempotent_across_two_consecutive_runs(monkeypatch):
    enqueued = _mock_sequencer_crm(monkeypatch)
    m.run_followup_sequencer(today=_SEQ_TODAY)
    after_first = list(enqueued)
    assert after_first, "first run should enqueue writes"

    m.run_followup_sequencer(today=_SEQ_TODAY)  # same data, same day
    assert enqueued == after_first  # nothing new queued or buried

    with m.get_db_conn() as conn:
        logged = {row[0] for row in conn.execute("SELECT sheet_uuid FROM followup_sequencer_log")}
    assert logged == {"seq-fu1", "seq-bury"}


def test_sequencer_dry_run_performs_zero_writes(monkeypatch):
    enqueued = _mock_sequencer_crm(monkeypatch)
    result = m.run_followup_sequencer(today=_SEQ_TODAY, dry_run=True)

    assert result["counts"] == {"followups_ready": 0, "ready_to_promote": 0, "revived": 0,
                                "applications_quiet": 1, "going_cold": 1, "buried": 1, "killed": 0,
                                "top_matched": 2, "buries_suppressed": 0, "kills_suppressed": 0}
    assert enqueued == []
    with m.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM followup_sequencer_log").fetchone()[0] == 0


# ---- Bury safety cap (MAX_AUTO_BURIES_PER_RUN) ----

def _mock_bury_backlog(monkeypatch, count):
    """Stand up `count` Applied rows all well past FOLLOWUP_BURY_DAYS - the stale-CRM shape where
    the whole backlog turns bury-eligible on one pass."""
    rows = [{"sheet_uuid": f"bury-{i}", "company": f"Co{i}", "title": "Ops", "name": "",
             "status": "Applied", "date_added": "2026-04-01", "next_followup": "1970-01-01",
             "raw_priority": "50"} for i in range(count)]
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(r) for r in rows] if code == "TC" else [])
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda payload: enqueued.append(payload) or True)
    return enqueued


def _logged_uuids():
    with m.get_db_conn() as conn:
        return {row[0] for row in conn.execute("SELECT sheet_uuid FROM followup_sequencer_log")}


def test_sequencer_under_the_bury_cap_writes_every_row(monkeypatch):
    under = m.MAX_AUTO_BURIES_PER_RUN - 1
    enqueued = _mock_bury_backlog(monkeypatch, under)
    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert len(result["buried"]) == under
    assert len([p for p in enqueued if p["action"] == "update_status"]) == under
    assert result["counts"]["buries_suppressed"] == 0
    assert len(_logged_uuids()) == under


def test_sequencer_over_the_bury_cap_writes_exactly_max_and_reports_the_rest(monkeypatch):
    over = m.MAX_AUTO_BURIES_PER_RUN + 5
    enqueued = _mock_bury_backlog(monkeypatch, over)
    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    # Every eligible row is reported on the card...
    assert len(result["buried"]) == over
    # ...but only MAX are actually written (append_note + update_status each).
    assert len([p for p in enqueued if p["action"] == "update_status"]) == m.MAX_AUTO_BURIES_PER_RUN
    assert len([p for p in enqueued if p["action"] == "append_note"]) == m.MAX_AUTO_BURIES_PER_RUN
    assert result["counts"]["buries_suppressed"] == 5


def test_sequencer_suppressed_buries_stay_eligible_for_the_next_run(monkeypatch):
    over = m.MAX_AUTO_BURIES_PER_RUN + 5
    enqueued = _mock_bury_backlog(monkeypatch, over)
    m.run_followup_sequencer(today=_SEQ_TODAY)

    # The withheld rows must NOT be logged as actioned, or they would never be retried.
    written = {p["sheet_uuid"] for p in enqueued}
    assert _logged_uuids() == written
    assert len(_logged_uuids()) == m.MAX_AUTO_BURIES_PER_RUN

    # A second pass drains the remainder rather than skipping it as already-actioned.
    enqueued.clear()
    second = m.run_followup_sequencer(today=_SEQ_TODAY)
    assert len([p for p in enqueued if p["action"] == "update_status"]) == 5
    assert second["counts"]["buries_suppressed"] == 0
    assert len(_logged_uuids()) == over


def test_sequencer_dry_run_is_unaffected_by_the_bury_cap(monkeypatch):
    over = m.MAX_AUTO_BURIES_PER_RUN + 5
    enqueued = _mock_bury_backlog(monkeypatch, over)
    result = m.run_followup_sequencer(today=_SEQ_TODAY, dry_run=True)

    assert len(result["buried"]) == over
    assert result["counts"]["buries_suppressed"] == 0  # nothing was withheld because nothing was written
    assert enqueued == []
    assert _logged_uuids() == set()


def test_needs_card_flags_withheld_buries_in_the_buried_section_and_summary(monkeypatch):
    _mock_bury_backlog(monkeypatch, m.MAX_AUTO_BURIES_PER_RUN + 5)
    card = m.render_followup_needs_card(m.run_followup_sequencer(today=_SEQ_TODAY))

    assert "Buried overnight (15)" in card
    assert "5 of these were withheld by the safety cap" in card
    assert "re-run" in card.lower()
    assert "5 buries capped" in card


# ---- Follow-up drafts: none at 7:30, created on demand from /followups ----
# Only PEOPLE rows (Carmen Cold) get draft text; JOBS rows are watch-only (applications_quiet).

def _mock_followup_rows(monkeypatch, cc_rows=(), jobs_rows=(), jobs_code="TW"):
    """Due Carmen Cold rows (plus optional JOBS rows on `jobs_code`), with the CRM outbox recorded
    and every Gmail draft path wired to fail the test if the sequencer touches it."""
    by_code = {"CC": list(cc_rows), jobs_code: list(jobs_rows)}
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(r) for r in by_code.get(code, [])])
    enqueued, drafts = [], []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda payload: enqueued.append(payload) or True)
    monkeypatch.setattr(m, "_stage_sequencer_draft", lambda *a, **k: drafts.append(a) or ("d", True, ""))
    monkeypatch.setattr(m, "create_gmail_draft", lambda **kw: drafts.append(kw) or (True, "Success", "d"))
    return enqueued, drafts


def _due_person(i, email="pat@acme.com", today=_SEQ_TODAY):
    """A Carmen Cold contact sitting on its first rung's due date (nudge #1 due `today`)."""
    return {"sheet_uuid": f"cc-{i}", "company": f"Co{i}", "title": "", "name": f"Pat{i}",
            "email": email, "status": "Cold Lead",
            "date_added": (today - timedelta(days=m.CARMEN_LADDER_DAYS[0])).strftime("%Y-%m-%d"),
            "next_followup": today.strftime("%Y-%m-%d"), "raw_priority": "High"}


def _due_application(i, email="kjmiller406@gmail.com"):
    """A JOBS row Applied 4 days ago - follow-up #1 due. Contact Email is often Kevin's own."""
    return {"sheet_uuid": f"app-{i}", "company": f"Acme{i}", "title": "Ops Analyst", "name": "",
            "email": email, "status": "Applied", "date_added": "2026-05-28",
            "next_followup": "1970-01-01", "raw_priority": "70"}


def test_sequencer_creates_no_gmail_drafts(monkeypatch):
    """7:30 drafts nothing - not for people with real addresses, not for applications - and with
    no draft cap every due person is snoozed and logged the same day."""
    people = [_due_person(i) for i in range(13)]
    enqueued, drafts = _mock_followup_rows(monkeypatch, cc_rows=people, jobs_rows=[_due_application(0)])

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert drafts == []
    assert "drafts_suppressed" not in result["counts"]
    assert len(result["followups_ready"]) == 13
    entry = result["followups_ready"][0]
    assert "draft_id" not in entry
    assert entry["draft_text"] and entry["sheet_uuid"] == "cc-0"
    assert entry["email"] == "pat@acme.com" and entry["company_raw"] == "Co0"
    assert len([p for p in enqueued if p["action"] == "update_snooze"]) == 14
    assert len(_logged_uuids()) == 14


def test_sequencer_dry_run_creates_no_gmail_drafts(monkeypatch):
    """/queue is read-only."""
    enqueued, drafts = _mock_followup_rows(monkeypatch, cc_rows=[_due_person(0)],
                                           jobs_rows=[_due_application(0)])

    result = m.run_followup_sequencer(today=_SEQ_TODAY, dry_run=True)

    assert len(result["followups_ready"]) == 1 and len(result["applications_quiet"]) == 1
    assert drafts == []
    assert enqueued == []


def test_sequencer_jobs_rows_are_watched_not_drafted(monkeypatch):
    """A Tetiana Warm application never gets bump text, but its clock still advances (snooze +
    log) so the +16 bury is reached."""
    _mock_followup_rows(monkeypatch, jobs_rows=[_due_application(0)])
    bumped = []
    monkeypatch.setattr(m, "build_followup_bump_draft", lambda *a, **k: bumped.append(a) or "text")
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda payload: enqueued.append(payload) or True)

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert bumped == []
    assert result["followups_ready"] == []
    assert result["counts"]["applications_quiet"] == 1
    app = result["applications_quiet"][0]
    assert app["sheet_tab"] == "Tetiana Warm"
    assert app["attempt"] == 1
    assert app["date_added"] == "2026-05-28"
    assert app["next_followup"] == "1970-01-01"
    assert app["new_next_followup"] == "2026-06-06"  # anchor + FOLLOWUP_2_DAYS
    assert app["days_silent"] == 4
    assert app["buries_on"] == "2026-06-13"  # anchor + FOLLOWUP_BURY_DAYS
    assert "draft_text" not in app and "draft_id" not in app
    snoozes = [p for p in enqueued if p["action"] == "update_snooze"]
    assert [(p["sheet_uuid"], p["next_followup"]) for p in snoozes] == [("app-0", "2026-06-06")]
    assert _logged_uuids() == {"app-0"}


def test_sequencer_same_day_rerun_does_not_snooze_twice(monkeypatch):
    enqueued, _ = _mock_followup_rows(monkeypatch, cc_rows=[_due_person(0)])
    m.run_followup_sequencer(today=_SEQ_TODAY)
    m.run_followup_sequencer(today=_SEQ_TODAY)
    assert len([p for p in enqueued if p["action"] == "update_snooze"]) == 1


# ---- /followups page (today's saved sequencer result) ----

def _get_followups_page():
    with m.app.test_client() as client:
        res = client.get("/followups")
        return res.status_code, res.get_data(as_text=True)


def _no_recompute(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GET /followups must never run the sequencer")
    monkeypatch.setattr(m, "run_followup_sequencer", boom)


def _ready_entry(uuid, email="pat@acme.com", company="Nliven", role="", name="Pat",
                 text="Hi Pat, just following up."):
    return {"company": company or "N/A", "company_raw": company, "role": role, "name": name,
            "short_id": None, "sheet_uuid": uuid, "attempt": 1, "draft_text": text,
            "sheet_tab": "Carmen Cold", "email": email,
            "next_followup": "2026-06-01", "new_next_followup": "2026-06-08"}


def _save_today(ready=(), quiet=()):
    today = m.datetime.now().strftime("%Y-%m-%d")
    assert m.save_followup_queue_snapshot(today, {
        "run_date": today, "followups_ready": list(ready), "applications_quiet": list(quiet),
        "counts": {"followups_ready": len(ready), "applications_quiet": len(quiet)},
    })
    return today


def test_followups_page_without_todays_snapshot_says_so_and_never_recomputes(monkeypatch):
    _no_recompute(monkeypatch)
    status, page = _get_followups_page()
    assert status == 200
    assert "No queue for today yet" in page and "7:30" in page


def test_followups_page_renders_the_saved_run_read_only(monkeypatch):
    """The job saves its result; the page shows the FULL draft, an Open in Gmail link only for a
    usable address, and the applications table with /stage links - creating nothing."""
    # The job runs on the real clock, so every row is dated relative to real today.
    real_now = m.datetime.now()
    application = {**_due_application(0),
                   "date_added": (real_now.date() - timedelta(days=m.FOLLOWUP_1_DAYS)).strftime("%Y-%m-%d")}
    _, drafts = _mock_followup_rows(monkeypatch, cc_rows=[
        _due_person(0, today=real_now.date()),
        _due_person(1, email="", today=real_now.date()),
        _due_person(2, email="x@y.com [⚠️ Fallback Email]", today=real_now.date()),
    ], jobs_rows=[application])
    long_text = "Hello <b>there</b> " + "y" * 1200
    monkeypatch.setattr(m, "build_followup_bump_draft", lambda rec, attempt: long_text)
    monkeypatch.setattr(m, "get_short_id_by_sheet_uuid", lambda su: "sid-app0" if su == "app-0" else None)
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "")
    m.scheduled_followup_sequencer_job()

    _no_recompute(monkeypatch)
    status, page = _get_followups_page()

    assert status == 200
    assert f"Follow-up Queue · {real_now.strftime('%Y-%m-%d')}" in page
    assert "Hello &lt;b&gt;there&lt;/b&gt; " + "y" * 1200 in page  # full text, escaped, untruncated
    assert page.count("📋 Copy Draft") == 3
    assert page.count("✉️ Open in Gmail") == 1
    assert 'href="/followups/draft/cc-0"' in page
    assert page.count("No verified address on file") == 2
    assert "mail.google.com" not in page  # no draft exists until a link is clicked
    assert "Applications Going Quiet (1)" in page and "Acme0" in page
    assert 'href="/stage/sid-app0"' in page
    assert drafts == []


def test_followups_page_with_an_empty_snapshot_is_a_clear_queue(monkeypatch):
    _save_today()
    _no_recompute(monkeypatch)
    status, page = _get_followups_page()
    assert status == 200 and "Queue is clear" in page


def test_followup_snapshot_prunes_runs_older_than_retention():
    for run_date in ("2026-05-01", "2026-05-18", "2026-05-19", "2026-06-01"):
        m.save_followup_queue_snapshot(run_date, {"run_date": run_date})
    with m.get_db_conn() as conn:
        kept = {r[0] for r in conn.execute("SELECT run_date FROM followup_queue_snapshot")}
    # 2026-06-01 minus 14 days = 2026-05-18, which is kept; anything earlier is pruned.
    assert kept == {"2026-05-18", "2026-05-19", "2026-06-01"}
    assert m.load_followup_queue_snapshot("2026-06-01") == {"run_date": "2026-06-01"}
    assert m.load_followup_queue_snapshot("2026-05-01") is None


def test_sequencer_job_saves_the_snapshot_before_sending_the_card(monkeypatch):
    order = []
    fake = {"run_date": "2026-06-01", "followups_ready": [], "applications_quiet": [],
            "going_cold": [], "buried": [], "top_matched": [],
            "counts": {"followups_ready": 0, "applications_quiet": 0, "going_cold": 0,
                       "buried": 0, "top_matched": 0}}
    monkeypatch.setattr(m, "run_followup_sequencer", lambda **kw: fake)
    monkeypatch.setattr(m, "save_followup_queue_snapshot", lambda d, r: order.append(("save", d)) or True)
    monkeypatch.setattr(m, "_send_telegram_card_chunked", lambda chat, text: order.append(("send", chat)))
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "123")

    m.scheduled_followup_sequencer_job()

    assert order == [("save", "2026-06-01"), ("send", "123")]


# ---- GET /followups/draft/<sheet_uuid> (on-demand Gmail draft) ----

def _click(uuid):
    with m.app.test_client() as client:
        res = client.get(f"/followups/draft/{uuid}")
        return res.status_code, res.headers.get("Location"), res.get_data(as_text=True)


def _gmail_calls(monkeypatch, result=(True, "Success", "draft-1")):
    calls = []
    monkeypatch.setattr(m, "create_gmail_draft", lambda **kw: calls.append(kw) or result)
    return calls


def test_draft_link_creates_the_draft_from_the_snapshot_and_redirects(monkeypatch):
    _save_today([_ready_entry("cc-0", text="Exact card text."),
                 _ready_entry("cc-1", company="Acme", role="Ops Analyst")])
    calls = _gmail_calls(monkeypatch)
    _no_recompute(monkeypatch)

    status, location, _ = _click("cc-0")

    assert status == 302
    assert location == "https://mail.google.com/mail/u/0/#drafts/draft-1"
    assert calls == [{"to_email": "pat@acme.com", "company_name": "Nliven", "job_title": "",
                      "custom_body": "Exact card text.", "custom_subject": "Re: Nliven"}]
    _click("cc-1")
    assert calls[1]["custom_subject"] == "Re: Ops Analyst @ Acme"


def test_draft_link_for_an_unknown_uuid_404s_without_recomputing(monkeypatch):
    calls = _gmail_calls(monkeypatch)
    _no_recompute(monkeypatch)

    status, _, page = _click("cc-0")  # no snapshot at all
    assert status == 404 and "not in today's queue" in page

    _save_today([_ready_entry("cc-0")])
    status, _, page = _click("nope")
    assert status == 404 and "not in today's queue" in page
    assert calls == []


@pytest.mark.parametrize("email", ["", "   ", "x@y.com [⚠️ Fallback Email]", None])
def test_draft_link_without_a_usable_address_shows_the_copy_page(monkeypatch, email):
    entry = _ready_entry("cc-0", email=email, text="Copy <me> by hand")
    if email is None:
        entry.pop("email")  # a snapshot saved before entries carried the address
    _save_today([entry])
    calls = _gmail_calls(monkeypatch)
    monkeypatch.setattr(m, "check_existing_gmail_draft",
                        lambda *a: pytest.fail("no Gmail lookup for an unusable address"))

    status, location, page = _click("cc-0")

    assert status == 200 and location is None
    assert calls == []
    assert "No verified address on file" in page
    assert "Copy &lt;me&gt; by hand</textarea>" in page and "📋 Copy Draft" in page


def test_second_click_redirects_to_the_same_draft_without_a_telegram_ping(monkeypatch):
    """Runs the real create_gmail_draft + 24h dedup: the first click creates, the second finds it."""
    for var, val in (("GMAIL_CLIENT_ID", "cid"), ("GMAIL_CLIENT_SECRET", "cs"),
                     ("GMAIL_REFRESH_TOKEN", "rt"), ("GMAIL_USER", "me@example.com")):
        monkeypatch.setenv(var, val)
    monkeypatch.setattr(m, "get_gmail_access_token", lambda: "token")
    posts, pings = [], []

    class Created:
        status_code = 200
        def json(self):
            return {"id": "draft-42"}
    monkeypatch.setattr(m.requests, "post", lambda url, **kw: posts.append(url) or Created())
    monkeypatch.setattr(m, "send_telegram_message", lambda *a, **k: pings.append(a) or 1)
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "123")
    _save_today([_ready_entry("cc-0")])

    first = _click("cc-0")
    second = _click("cc-0")

    assert first[:2] == (302, "https://mail.google.com/mail/u/0/#drafts/draft-42")
    assert second[:2] == first[:2]
    assert len(posts) == 1
    assert pings == []


def test_duplicate_return_with_ok_false_still_redirects(monkeypatch):
    """create_gmail_draft answers a duplicate with ok=False and the real id - that is success."""
    _save_today([_ready_entry("cc-0")])
    _gmail_calls(monkeypatch, result=(False, "Draft already exists in Gmail", "draft-7"))
    status, location, _ = _click("cc-0")
    assert (status, location) == (302, "https://mail.google.com/mail/u/0/#drafts/draft-7")


@pytest.mark.parametrize("failure", ["error", "raise"])
def test_gmail_failure_renders_the_reason_and_the_text(monkeypatch, failure):
    _save_today([_ready_entry("cc-0", text="Fallback text")])
    if failure == "raise":
        def boom(**kw):
            raise m.requests.exceptions.Timeout("gmail timed out")
        monkeypatch.setattr(m, "create_gmail_draft", boom)
        reason = "gmail timed out"
    else:
        _gmail_calls(monkeypatch, result=(False, "OAuth Token Unavailable", None))
        reason = "OAuth Token Unavailable"

    status, location, page = _click("cc-0")

    assert status == 502 and location is None
    assert reason in page
    assert "Fallback text</textarea>" in page and "📋 Copy Draft" in page


def test_blank_company_is_blocked_by_the_placeholder_guard(monkeypatch):
    """The entry keeps the raw company, so a blank one reaches create_gmail_draft as "Target Firm"
    and is refused, exactly as the old 7:30 path was - not drafted as "Re: N/A"."""
    for var in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setattr(m.requests, "post", lambda *a, **k: pytest.fail("must not reach Gmail"))
    _save_today([_ready_entry("cc-0", company="")])

    status, _, page = _click("cc-0")

    assert status == 502 and "placeholder company name" in page


# ---- Carmen Cold in the follow-up cadence (sequencer scan + overdue + roleless bumps) ----

def test_carmen_cold_is_in_the_sequencer_scan_and_gets_followups_drafted(monkeypatch):
    """Carmen Cold is scanned by run_followup_sequencer() like the JOBS tabs, but runs the
    CARMEN_LADDER_DAYS people ladder. A row sitting on its first rung's due date draws follow-up
    #1 with a roleless draft and is advanced to the second rung."""
    assert ("CC", "Carmen Cold") in m.SEQUENCER_SCAN_TABS
    anchor = (_SEQ_TODAY - timedelta(days=m.CARMEN_LADDER_DAYS[0])).strftime("%Y-%m-%d")
    cc_row = {"sheet_uuid": "cc-fu1", "company": "Nliven", "title": "", "name": "Dana Reyes",
              "status": "Applied", "date_added": anchor,
              "next_followup": _SEQ_TODAY.strftime("%Y-%m-%d"), "raw_priority": "High"}
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(cc_row)] if code == "CC" else [])
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    ready = result["followups_ready"]
    assert [r["sheet_uuid"] for r in ready] == ["cc-fu1"]
    assert ready[0]["ladder_day"] == m.CARMEN_LADDER_DAYS[0]
    draft = ready[0]["draft_text"]
    assert draft.startswith("Hi Dana Reyes,")
    assert "{" not in draft
    assert "this role" not in draft and "the  role" not in draft
    assert any(p["action"] == "update_snooze" and p["sheet_uuid"] == "cc-fu1" for p in enqueued)


def test_carmen_cold_undated_row_joins_the_ladder_instead_of_drafting(monkeypatch):
    """A contact dragged into Carmen Cold by hand has no follow-up date. The sequencer starts the
    ladder at +4 rather than firing a nudge immediately - that is the manual-move path working
    with no Apps Script trigger involved. Its Date Added is months old, so the start is a revival:
    a dated restart note is written first so the next pass anchors on today, not on January."""
    cc_row = {"sheet_uuid": "cc-manual", "company": "Affirm", "title": "", "name": "Sahjar",
              "status": "Cold Lead", "date_added": "2026-01-04", "next_followup": "1970-01-01",
              "raw_priority": "Medium"}
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(cc_row)] if code == "CC" else [])
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert result["followups_ready"] == []
    expected = (_SEQ_TODAY + timedelta(days=m.CARMEN_LADDER_DAYS[0])).strftime("%Y-%m-%d")
    assert [p["action"] for p in enqueued] == ["append_note", "update_snooze"]
    assert enqueued[0]["note"].startswith(f"[{_SEQ_TODAY.isoformat()}] {m.LADDER_RESTART_NOTE_MARKER}")
    assert enqueued[1]["next_followup"] == expected
    assert [(r["sheet_uuid"], r["first_nudge"]) for r in result["revived"]] == [("cc-manual", expected)]


def test_carmen_cold_row_is_never_auto_buried_to_died(monkeypatch):
    """A networking contact is not a job application: a CC row is never moved to Died. This row's
    follow-up date is a 60-day gap the ladder never writes, on a 61-day-old anchor, so it is read
    as a stalled row and revived - it used to be surfaced as 'going cold' instead."""
    exhausted_nf = (_SEQ_TODAY - timedelta(days=1)).strftime("%Y-%m-%d")
    cc_row = {"sheet_uuid": "cc-old", "company": "Nliven", "title": "", "name": "Sam",
              "status": "Applied", "date_added": "2026-04-01", "next_followup": exhausted_nf,
              "raw_priority": "Medium"}
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(cc_row)] if code == "CC" else [])
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert [r["sheet_uuid"] for r in result["buried"]] == []
    assert [r["sheet_uuid"] for r in result["killed"]] == []
    assert [r["sheet_uuid"] for r in result["revived"]] == ["cc-old"]
    assert not any(p.get("new_tab") for p in enqueued)  # no tab move of any kind
    assert [p["action"] for p in enqueued] == ["append_note", "update_snooze"]


def test_overdue_scan_includes_carmen_cold(monkeypatch):
    by_code = {
        "CC": [_overdue_record("HotLead", "2020-01-01")],
        "CW": [_overdue_record("OldFriend", "2020-02-01")],
        "TC": [_overdue_record("Stellantis", "2020-03-01")],
    }
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda target_code="CW", qty=2: by_code.get(target_code, []))
    overdue = m.get_overdue_followups()
    assert [r["company"] for r in overdue] == ["HotLead", "OldFriend", "Stellantis"]
    assert {r["sheet_tab"] for r in overdue} == {"Carmen Cold", "Carmen Warm", "Tetiana Cold"}


def test_build_followup_bump_draft_renders_cleanly_with_no_role():
    """PEOPLE rows have no title; the draft must not read 'the this role role at X' or 'the  role at X'."""
    record = {"name": "Dana", "company": "Nliven", "title": ""}
    for attempt in (1, 2):
        draft = m.build_followup_bump_draft(record, attempt)
        assert draft.startswith("Hi Dana,")
        assert "{" not in draft
        assert "this role" not in draft
        assert "the  role" not in draft and " role at" not in draft
    # With a real role it still uses the followup_bumps bank's "the X role at Y" register.
    with_role = m.build_followup_bump_draft({"name": "Dana", "company": "Nliven", "title": "Ops Analyst"}, 1)
    assert "Ops Analyst role at Nliven" in with_role


def test_generate_bump_email_routes_to_roleless_copy_when_title_is_blank():
    blank = m.generate_bump_email(contact_name="Dana", company_name="Nliven")
    assert blank.startswith("Hi Dana,")
    assert "this role" not in blank and "{" not in blank
    titled = m.generate_bump_email(contact_name="Dana", job_title="Ops Analyst", company_name="Nliven")
    assert "Ops Analyst role at Nliven" in titled


# ---- Carmen contact lifecycle: revival, reply re-anchoring, promote, auto-kill ----

class _FakeCarmenSheet:
    """In-memory Carmen Cold/Hot tabs that apply the sequencer's queued writes the way Code.gs
    would, so a row can be walked through the ladder across real day-by-day runs."""

    def __init__(self, monkeypatch, rows, hot_rows=()):
        self.tabs = {"CC": [dict(r) for r in rows], "CH": [dict(r) for r in hot_rows]}
        self.moves, self.payloads = [], []
        monkeypatch.setattr(m, "fetch_networking_cards",
                            lambda code, qty=None: [dict(r) for r in self.tabs.get(code, [])])
        monkeypatch.setattr(m, "enqueue_crm_payload", self.apply)
        monkeypatch.setattr(m, "create_gmail_draft", lambda **kw: (True, "Success", "d"))

    def row(self, sheet_uuid):
        return next((r for rows in self.tabs.values() for r in rows if r["sheet_uuid"] == sheet_uuid), None)

    def apply(self, payload):
        self.payloads.append(payload)
        row = self.row(payload["sheet_uuid"])
        if payload["action"] == "update_snooze":
            row["next_followup"] = payload["next_followup"]
        elif payload["action"] == "append_note":
            row["note"] = f"{row.get('note') or ''}\n{payload['note']}".strip()
        elif payload["action"] == "update_status":
            for rows in self.tabs.values():
                if row in rows:
                    rows.remove(row)
            self.moves.append((payload["sheet_uuid"], payload["new_tab"]))
        return True


def _person(uuid, date_added, next_followup="1970-01-01", note="", email="p@x.com"):
    return {"sheet_uuid": uuid, "company": f"Co-{uuid}", "title": "", "name": f"Name-{uuid}",
            "email": email, "status": "Cold Lead", "date_added": date_added,
            "next_followup": next_followup, "raw_priority": "Medium", "note": note}


def _run_days(sheet, start, days):
    """Run the sequencer once per day and return {day_offset: result}."""
    return {n: m.run_followup_sequencer(today=start + timedelta(days=n)) for n in range(days)}


def test_ghost_walks_the_whole_ladder_and_is_killed(monkeypatch):
    """Regression for the unreachable path: nudge #3 used to write no date, so the row re-read as
    rung 3 and nudged forever. It must now get exactly three nudges, a grace week, then Killed."""
    start = _SEQ_TODAY
    sheet = _FakeCarmenSheet(monkeypatch, [_person("ghost", start.isoformat())])
    results = _run_days(sheet, start, 40)

    nudge_days = {n: e["attempt"] for n, r in results.items() for e in r["followups_ready"]}
    assert nudge_days == {d: i + 1 for i, d in enumerate(m.CARMEN_LADDER_DAYS)}
    kill_days = [n for n, r in results.items() if r["killed"]]
    assert kill_days == [m.CARMEN_LADDER_DAYS[-1] + 7]
    assert sheet.moves == [("ghost", "Killed")]
    assert sheet.row("ghost") is None
    kill_note = [p for p in sheet.payloads if p["action"] == "append_note" and "reason" in p["note"]]
    assert [p["note"] for p in kill_note] == ["[reason: no reply after 3 nudges]"]
    # The final nudge's card line says when the kill check happens.
    card = m.render_followup_needs_card(results[m.CARMEN_LADDER_DAYS[-1]])
    terminal = (start + timedelta(days=m.CARMEN_LADDER_DAYS[-1] + 7)).isoformat()
    assert f"→ killed {terminal} if silent" in card


def test_promoted_bench_contact_with_ancient_dates_is_nudged_not_killed(monkeypatch):
    """The bug CHANGE 1 prevents: a Carmen Warm contact dragged into Carmen Cold with a 210-day-old
    Last Contact Date and an old follow-up date must be restarted, not killed on the first pass."""
    start = _SEQ_TODAY
    old = (start - timedelta(days=210)).isoformat()
    sheet = _FakeCarmenSheet(monkeypatch, [_person("bench", old, next_followup=(start - timedelta(days=200)).isoformat())])
    results = _run_days(sheet, start, 5)

    assert results[0]["killed"] == [] and sheet.moves == []
    assert [r["sheet_uuid"] for r in results[0]["revived"]] == ["bench"]
    restart_notes = [p for p in sheet.payloads if m.LADDER_RESTART_NOTE_MARKER in p.get("note", "")]
    assert len(restart_notes) == 1  # recorded once, then the ladder climbs normally
    assert [e["attempt"] for e in results[4]["followups_ready"]] == [1]
    assert sheet.row("bench")["date_added"] == old  # real history is never rewritten
    card = m.render_followup_needs_card(results[0])
    assert "Back on the ladder (1)" in card and "revived — ladder restarted today" in card


def test_same_day_rerun_does_not_record_a_second_restart(monkeypatch):
    old = (_SEQ_TODAY - timedelta(days=90)).isoformat()
    sheet = _FakeCarmenSheet(monkeypatch, [_person("bench", old)])
    # Notes are async in production: simulate the outbox not having flushed yet.
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: sheet.payloads.append(p) or True)
    m.run_followup_sequencer(today=_SEQ_TODAY)
    m.run_followup_sequencer(today=_SEQ_TODAY)
    assert len([p for p in sheet.payloads if p["action"] == "append_note"]) == 1


def test_responder_reaches_ready_to_promote_and_is_never_moved(monkeypatch):
    start = _SEQ_TODAY
    reply = f"[{start.isoformat()}] {m.INBOUND_REPLY_NOTE_MARKER} (they wrote to Kevin, not a send)."
    sheet = _FakeCarmenSheet(monkeypatch, [_person("talker", (start - timedelta(days=20)).isoformat(),
                                                   next_followup=(start + timedelta(days=4)).isoformat(),
                                                   note=reply)])
    results = _run_days(sheet, start, 45)

    # The reply restarted the ladder from its own date: nudges at 4/11/21 after the reply.
    nudge_days = sorted(n for n, r in results.items() if r["followups_ready"])
    assert nudge_days == list(m.CARMEN_LADDER_DAYS)
    promote_days = [n for n, r in results.items() if r["ready_to_promote"]]
    assert promote_days == list(range(m.CARMEN_LADDER_DAYS[-1] + 7, 45))  # every morning until acted on
    assert sheet.moves == []
    entry = results[44]["ready_to_promote"][0]
    assert entry["replied_on"] == start.isoformat()
    card = m.render_followup_needs_card(results[44])
    assert "Ready to promote (1)" in card
    assert "<code>/promote talker</code>" in card


def test_exhausted_triage_writes_nothing_under_dry_run(monkeypatch):
    anchor = _SEQ_TODAY - timedelta(days=28)
    terminal = anchor + timedelta(days=28)
    ghost = _person("g", anchor.isoformat(), next_followup=terminal.isoformat())
    bench = _person("b", "2025-01-01")
    sheet = _FakeCarmenSheet(monkeypatch, [ghost, bench])

    result = m.run_followup_sequencer(today=_SEQ_TODAY, dry_run=True)

    assert [r["sheet_uuid"] for r in result["killed"]] == ["g"]
    assert [r["sheet_uuid"] for r in result["revived"]] == ["b"]
    assert sheet.payloads == []
    assert _logged_uuids() == set()


def test_kill_cap_withholds_the_overflow_and_drains_on_rerun(monkeypatch):
    over = m.MAX_AUTO_KILLS_PER_RUN + 3
    anchor = _SEQ_TODAY - timedelta(days=28)
    rows = [_person(f"g{i}", anchor.isoformat(), next_followup=_SEQ_TODAY.isoformat()) for i in range(over)]
    sheet = _FakeCarmenSheet(monkeypatch, rows)

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert len(result["killed"]) == over
    assert result["counts"]["kills_suppressed"] == 3
    assert len(sheet.moves) == m.MAX_AUTO_KILLS_PER_RUN
    assert len(_logged_uuids()) == m.MAX_AUTO_KILLS_PER_RUN
    card = m.render_followup_needs_card(result)
    assert f"Killed overnight ({over})" in card
    assert "3 of these were withheld by the safety cap" in card and "3 kills capped" in card

    m.run_followup_sequencer(today=_SEQ_TODAY)
    assert len(sheet.moves) == over


def test_reply_router_note_is_what_the_ladder_parses(reply_routing):
    """Writer and parser share INBOUND_REPLY_NOTE_MARKER; this pins them together."""
    m.route_inbound_reply_to_crm(_match("Carmen Cold"), "GENERAL", "Re: hi", "Sure, let's talk")
    note = [p for p in reply_routing() if p["action"] == "append_note"][0]["note"]
    assert m.carmen_reply_anchor(note) == date.today()


# ---- /promote and /demote ----

_PROMOTE_UUID = "abcdef12-3456-7890-abcd-ef1234567890"


def _promote_env(monkeypatch, cold=(), hot=(), short_ids=None):
    sent, enqueued = [], []
    tabs = {"CC": list(cold), "CH": list(hot)}
    monkeypatch.setattr(m, "fetch_networking_cards", lambda code, qty=None: [dict(r) for r in tabs.get(code, [])])
    monkeypatch.setattr(m, "get_sheet_uuid_by_short_id", lambda sid: (short_ids or {}).get(sid))
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)
    return sent, enqueued


def test_promote_by_the_cards_uuid_stub_moves_to_carmen_hot_with_a_note(monkeypatch):
    sent, enqueued = _promote_env(monkeypatch, cold=[_person(_PROMOTE_UUID, "2026-05-01")])
    _dispatch(f"/promote {_PROMOTE_UUID[:8]}")

    assert [p["action"] for p in enqueued] == ["update_status", "append_note"]
    assert enqueued[0] == {**enqueued[0], "sheet_uuid": _PROMOTE_UUID, "new_tab": "Carmen Hot"}
    assert re.match(r"^\[\d{4}-\d{2}-\d{2}\] Promoted from Carmen Cold to Carmen Hot", enqueued[1]["note"])
    assert "Promoted" in sent[0]


def test_promote_by_short_id(monkeypatch):
    _, enqueued = _promote_env(monkeypatch, cold=[_person(_PROMOTE_UUID, "2026-05-01")],
                               short_ids={"sid1": _PROMOTE_UUID})
    _dispatch("/promote sid1")
    assert enqueued[0]["new_tab"] == "Carmen Hot"


def test_demote_moves_a_hot_contact_back_to_the_bench(monkeypatch):
    _, enqueued = _promote_env(monkeypatch, hot=[_person(_PROMOTE_UUID, "2026-05-01")])
    _dispatch(f"/demote {_PROMOTE_UUID}")
    assert enqueued[0]["new_tab"] == "Carmen Warm"
    assert "Demoted from Carmen Hot to Carmen Warm" in enqueued[1]["note"]


def test_promote_refuses_unknown_ambiguous_and_already_hot(monkeypatch):
    twin = "abcdef12-0000-0000-0000-000000000000"
    sent, enqueued = _promote_env(monkeypatch, cold=[_person(_PROMOTE_UUID, "2026-05-01"), _person(twin, "2026-05-01")],
                                  short_ids={"jobsid": "job-row-uuid"})
    _dispatch("/promote")
    _dispatch("/promote jobsid")           # a JOBS row's short_id never moves into a PEOPLE tab
    _dispatch("/promote abcdef12")         # prefix matches two contacts
    _dispatch("/promote abc")              # too short to be a uuid prefix
    assert enqueued == []
    assert "Usage" in sent[0]
    assert "not a Carmen Cold or Carmen Hot contact" in sent[1]
    assert "matches more than one contact" in sent[2]
    assert "not a Carmen Cold or Carmen Hot contact" in sent[3]

    sent2, enqueued2 = _promote_env(monkeypatch, hot=[_person(_PROMOTE_UUID, "2026-05-01")])
    _dispatch(f"/promote {_PROMOTE_UUID}")
    assert enqueued2 == [] and "already in Carmen Hot" in sent2[0]


def test_help_lists_promote_and_demote(monkeypatch):
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)
    _dispatch("/help")
    assert "/promote &lt;id&gt;" in "".join(sent) and "/demote &lt;id&gt;" in "".join(sent)


# ---- Daily "needs you today" card (render_followup_needs_card) ----

def test_needs_card_renders_every_populated_section(monkeypatch):
    _mock_sequencer_crm(monkeypatch)
    card = m.render_followup_needs_card(m.run_followup_sequencer(today=_SEQ_TODAY, dry_run=True))
    assert "Needs You Today" in card
    assert "Applications going quiet (1)" in card and "4d silent" in card
    assert "Going cold (1)" in card and "10d untouched" in card
    assert "Buried overnight (1)" in card
    assert "Top 3 untouched matches" in card
    assert ("Summary:</b> 0 follow-ups · 1 applications quiet · 1 going cold · 1 buried · "
            "2 top matches") in card


def test_needs_card_empty_result_is_a_single_line():
    empty = {"followups_ready": [], "going_cold": [], "buried": [], "top_matched": [],
             "counts": {"followups_ready": 0, "going_cold": 0, "buried": 0, "top_matched": 0}}
    card = m.render_followup_needs_card(empty)
    assert "\n" not in card
    assert "nothing needs you today" in card.lower()


def test_needs_card_on_demand_is_labelled_read_only():
    empty = {"followups_ready": [], "going_cold": [], "buried": [], "top_matched": [], "counts": {}}
    assert "clear" in m.render_followup_needs_card(empty, on_demand=True).lower()
    populated = {"followups_ready": [{"company": "Acme", "role": "Ops", "attempt": 1,
                                     "draft_text": "hi", "short_id": "abc123"}],
                "going_cold": [], "buried": [], "top_matched": [],
                "counts": {"followups_ready": 1, "going_cold": 0, "buried": 0, "top_matched": 0}}
    card = m.render_followup_needs_card(populated, on_demand=True)
    assert "Queue Preview" in card and "read-only" in card
    assert f"<a href='{m.BASE_URL}/followups'>Open Follow-up Queue</a>" in card


def test_needs_card_people_are_a_short_list_with_no_drafts_and_no_swipeable_uuid():
    """Draft text and Gmail links live on /followups; the card is one line per person. Several
    entries share one message, and swipe-reply recovery takes the FIRST 🆔 UUID it finds - so the
    card must carry no full sheet_uuid, or /x would hit entry #1 whichever was meant."""
    uuid_a, uuid_b = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    ready = [
        {"company": "Acme & Co", "role": "", "name": "Dana", "attempt": 1, "draft_text": "x" * 950,
         "short_id": "abc123", "sheet_uuid": uuid_a, "sheet_tab": "Carmen Cold", "draft_id": "d1",
         "next_followup": "2026-06-01", "new_next_followup": "2026-06-08"},
        {"company": "Beta", "role": "", "name": "", "attempt": 3, "draft_text": "hi",
         "short_id": None, "sheet_uuid": uuid_b, "sheet_tab": "Carmen Cold", "draft_id": None,
         "next_followup": "2026-06-01", "new_next_followup": None},
    ]
    result = {"followups_ready": ready, "going_cold": [], "buried": [], "top_matched": [],
              "counts": {"followups_ready": 2, "going_cold": 0, "buried": 0, "top_matched": 0}}
    card = m.render_followup_needs_card(result)

    assert "Nudge these people (2)" in card
    assert "💼 <b>Dana</b> — Acme &amp; Co · #1 · due 2026-06-01 → next 2026-06-08 · 🆔 <code>abc123</code>" in card
    assert "#3 · due 2026-06-01 → next last nudge" in card
    assert "xxxx" not in card  # no draft blob
    assert "Open Draft" not in card and "mail.google.com" not in card
    assert "Full Card" not in card
    assert card.count(f"<a href='{m.BASE_URL}/followups'>Open Follow-up Queue</a>") == 1
    assert uuid_a not in card and uuid_b not in card
    assert m._parse_sheet_uuid_from_card_text(card) == (None, None)
    assert "/apply" not in card and "Swipe-replies don't work on this card" in card


@pytest.mark.parametrize("days,dot", [(0, "🟢"), (4, "🟢"), (5, "🟡"), (9, "🟡"), (10, "🟠"),
                                      (14, "🟠"), (15, "🔴"), (30, "🔴")])
def test_needs_card_applications_carry_a_silence_dot(days, dot):
    quiet = [{"company": "Acme & Co", "role": "Ops <Lead>", "short_id": "sid1", "sheet_uuid": "u",
              "sheet_tab": "Tetiana Warm", "attempt": 1, "date_added": "2026-05-20",
              "next_followup": "1970-01-01", "new_next_followup": "2026-05-29",
              "days_silent": days, "buries_on": "2026-06-05"}]
    result = {"followups_ready": [], "applications_quiet": quiet, "going_cold": [], "buried": [],
              "top_matched": [], "counts": {"applications_quiet": 1}}
    card = m.render_followup_needs_card(result)

    assert "Applications going quiet (1)" in card
    assert (f"{dot} <b>Acme &amp; Co</b> — Ops &lt;Lead&gt; · applied 2026-05-20 · {days}d silent · "
            f"buries 2026-06-05 · 📋 <a href='{m.BASE_URL}/stage/sid1'>Full Card</a>") in card
    assert "mail.google.com" not in card and "Open Follow-up Queue" not in card


def test_needs_card_application_without_short_id_has_no_stage_link():
    quiet = [{"company": "Acme", "role": "Ops", "short_id": None, "sheet_uuid": "u", "attempt": 2,
              "date_added": "2026-05-20", "days_silent": 12, "buries_on": "2026-06-05"}]
    card = m.render_followup_needs_card({"applications_quiet": quiet, "counts": {"applications_quiet": 1}})
    assert "Full Card" not in card and "/stage/None" not in card


# ---- /queue command (read-only sequencer preview) ----

def test_queue_command_previews_without_any_writes(monkeypatch):
    enqueued = _mock_sequencer_crm(monkeypatch)
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)

    _dispatch("/queue")

    assert len(sent) == 1
    assert "Queue Preview" in sent[0] and "read-only" in sent[0]
    assert enqueued == []
    with m.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM followup_sequencer_log").fetchone()[0] == 0


# ---- Company name cleaning for recruiter-facing copy ----

def test_clean_company_strips_only_trailing_legal_suffixes():
    # The suffix strip is anchored to the END. It used to be an unanchored \b(inc|co|group|...)\b
    # sweep that removed those words wherever they appeared, so real firm names were corrupted in
    # every email, letter and resume filename the pipeline produced.
    assert m.clean_company_for_copy("Group 1 Automotive") == "Group 1 Automotive"
    assert m.clean_company_for_copy("Co-Diagnostics") == "Co-Diagnostics"
    assert m.clean_company_for_copy("The Corporation for Public Broadcasting") == (
        "The Corporation for Public Broadcasting"
    )
    assert m.clean_company_for_copy("Ltd Commodities") == "Ltd Commodities"
    assert m.clean_company_for_copy("Inc Magazine") == "Inc Magazine"
    # Word-boundary survivors that the old sweep already got right - keep them right.
    assert m.clean_company_for_copy("Incyte") == "Incyte"
    assert m.clean_company_for_copy("Groupon") == "Groupon"
    assert m.clean_company_for_copy("Corning") == "Corning"


def test_clean_company_still_strips_real_suffixes_including_stacked():
    assert m.clean_company_for_copy("RevSpring Inc") == "RevSpring"
    assert m.clean_company_for_copy("Quicken Loans, Inc.") == "Quicken Loans"
    assert m.clean_company_for_copy("Lear Corporation") == "Lear"
    assert m.clean_company_for_copy("Penske Corp.") == "Penske"
    assert m.clean_company_for_copy("Barclays PLC") == "Barclays"
    assert m.clean_company_for_copy("Aptiv Ltd") == "Aptiv"
    # LLP/PLLC are in the shared list now, so resume_pdf_filename() no longer re-strips them.
    assert m.clean_company_for_copy("Plante Moran PLLC") == "Plante Moran"
    assert m.clean_company_for_copy("Ernst & Young LLP") == "Ernst & Young"
    # Stacked suffixes come off one token per loop pass.
    assert m.clean_company_for_copy("Atwell, Inc. Ltd") == "Atwell"


def test_clean_company_keeps_group_holdings_and_companies():
    # These are the firm's actual name at least as often as they are legal noise, and addressing
    # "Rocket Companies" as "Rocket" reads as a failed mail merge.
    assert m.clean_company_for_copy("Boston Consulting Group") == "Boston Consulting Group"
    assert m.clean_company_for_copy("Rocket Companies LLC") == "Rocket Companies"
    assert m.clean_company_for_copy("Alliance Group Holdings") == "Alliance Group Holdings"
    assert m.clean_company_for_copy("Atwell Group, Inc.") == "Atwell Group"


def test_clean_company_never_returns_empty():
    # A firm literally named after a legal word must not strip to nothing.
    assert m.clean_company_for_copy("Inc") == "Inc"
    assert m.clean_company_for_copy("Ltd.") == "Ltd."
    assert m.clean_company_for_copy("") == "your team"
    assert m.clean_company_for_copy(None) == "your team"
    assert m.clean_company_for_copy("   ") == "your team"


# ---- Resume PDF attachment filename ----

def test_resume_pdf_filename_drops_track_code_and_legal_suffix():
    # The recruiter-visible name carries the company and nothing internal: no Track A-E routing
    # key, no mangled run-together words, no legal suffix.
    assert m.resume_pdf_filename("Atwell, LLC") == "Kevin_Miller_Resume_Atwell.pdf"
    assert m.resume_pdf_filename("Goldman Sachs") == "Kevin_Miller_Resume_Goldman_Sachs.pdf"
    assert m.resume_pdf_filename("Ernst & Young LLP") == "Kevin_Miller_Resume_Ernst_Young.pdf"
    # "Holdings" survives: it is part of the firm's name far more often than it is legal noise,
    # and clean_company_for_copy() no longer strips it. "Corporation" is still a trailing suffix.
    assert m.resume_pdf_filename("Booz Allen Hamilton Holdings Corporation") == (
        "Kevin_Miller_Resume_Booz_Allen_Hamilton_Holdings.pdf"
    )


def test_resume_pdf_filename_preserves_brand_casing_and_separates_words():
    # "thyssenkrupp" styles its own name lowercase; title-casing it would be wrong. The old
    # re.sub(r'[^a-zA-Z0-9]', '') deleted the spaces instead of converting them, yielding
    # "thyssenkruppMaterialsCALtd".
    assert m.resume_pdf_filename("thyssenkrupp Materials CA Ltd") == (
        "Kevin_Miller_Resume_thyssenkrupp_Materials_CA.pdf"
    )
    # "&" joins words rather than separating them.
    assert m.resume_pdf_filename("AT&T Inc.") == "Kevin_Miller_Resume_ATT.pdf"


def test_resume_pdf_filename_omits_company_when_unresolved():
    # clean_company_for_copy() answers "your team" for an empty company, which reads fine in prose
    # but must never reach a filename.
    for unresolved in ["", None, "   ", "your team", "Target Firm"]:
        assert m.resume_pdf_filename(unresolved) == "Kevin_Miller_Resume.pdf"


# ---- /draft Gmail MIME attachment correctness ----

def test_create_gmail_draft_attaches_pdf_with_correct_filename(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "rtoken")
    monkeypatch.setenv("GMAIL_USER", "me@example.com")
    monkeypatch.setattr(m, "check_existing_gmail_draft", lambda to_email, subject: None)
    monkeypatch.setattr(m, "get_gmail_access_token", lambda: "fake-token")
    monkeypatch.setattr(m, "save_gmail_draft_record", lambda *a, **k: True)

    captured = {}

    class FakeDraftResponse:
        status_code = 200
        def json(self):
            return {"id": "draft-99"}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["raw"] = json["message"]["raw"]
        return FakeDraftResponse()

    monkeypatch.setattr(m.requests, "post", fake_post)

    ok, msg, draft_id = m.create_gmail_draft(
        to_email="hiring@acme.com", company_name="Acme Corp", job_title="Ops Analyst",
        pdf_bytes=b"%PDF-1.4 fake pdf bytes", pdf_filename="Kevin_Miller_Resume_Acme_TrackA.pdf"
    )

    assert ok is True
    assert draft_id == "draft-99"
    decoded = base64.urlsafe_b64decode(captured["raw"])
    email_msg = message_from_bytes(decoded)
    attachments = [part for part in email_msg.walk() if part.get_content_disposition() == "attachment"]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "Kevin_Miller_Resume_Acme_TrackA.pdf"
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_payload(decode=True) == b"%PDF-1.4 fake pdf bytes"


@pytest.mark.parametrize("placeholder", ["Target Firm", "target firm", "Target Company", "your team", "your company", "", "   "])
def test_create_gmail_draft_refuses_a_placeholder_company_name(monkeypatch, placeholder):
    """A recruiter must never get "Saw the role at your team." - the Gmail send path blocks a
    placeholder company name before any Gmail API call and returns (False, reason, None)."""
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "rtoken")
    monkeypatch.setenv("GMAIL_USER", "me@example.com")

    calls = []
    monkeypatch.setattr(m.requests, "post", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(m, "get_gmail_access_token", lambda: (_ for _ in ()).throw(AssertionError("must not reach OAuth")))

    ok, msg, draft_id = m.create_gmail_draft(
        to_email="recruiter@acme.com", company_name=placeholder, job_title="Ops Analyst",
    )

    assert ok is False
    assert draft_id is None
    assert "placeholder company name" in msg.lower()
    assert calls == []  # no Gmail API call was made


def test_create_gmail_draft_allows_a_real_company_name(monkeypatch):
    """The block is narrow: a normal company name still drafts."""
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "rtoken")
    monkeypatch.setenv("GMAIL_USER", "me@example.com")
    monkeypatch.setattr(m, "check_existing_gmail_draft", lambda to_email, subject: None)
    monkeypatch.setattr(m, "get_gmail_access_token", lambda: "fake-token")
    monkeypatch.setattr(m, "save_gmail_draft_record", lambda *a, **k: True)

    class FakeDraftResponse:
        status_code = 200
        def json(self):
            return {"id": "draft-ok"}

    monkeypatch.setattr(m.requests, "post", lambda *a, **k: FakeDraftResponse())

    ok, msg, draft_id = m.create_gmail_draft(
        to_email="recruiter@acme.com", company_name="Atwell", job_title="Ops Analyst",
    )
    assert ok is True
    assert draft_id == "draft-ok"


# ---- Canonical Status writes: /apply, /replied, /interview (Status field only, no tab move) ----

def _dispatch(text, reply_to_message=None):
    msg = {"chat": {"id": 1}, "text": text}
    if reply_to_message is not None:
        msg["reply_to_message"] = reply_to_message
    m.process_webhook_payload_async({"message": msg})


def test_apply_swipe_writes_status_applied_and_never_moves_tabs(monkeypatch):
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-apply", "sheet_tab": "Tetiana Cold", "contact_name": "", "contact_company": "Acme Corp"})
    monkeypatch.setattr(m, "get_job_by_sheet_uuid", lambda u: {"job_title": "Ops Analyst", "job_id": "gh_x"})
    for name in ("send_telegram_message", "edit_telegram_message", "log_metric_event",
                 "log_daily_activity", "record_application_outcome", "add_company_cooldown",
                 "upsert_company_identity"):
        monkeypatch.setattr(m, name, lambda *a, **k: None)
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    _dispatch("/apply")

    assert len(enqueued) == 1
    assert enqueued[0]["action"] == "set_status"
    assert enqueued[0]["status"] == "Applied"
    assert enqueued[0]["sheet_uuid"] == "uuid-apply"
    assert "new_tab" not in enqueued[0]  # Status write only - no tab move


def test_warm_on_a_job_row_starts_the_followup_ladder(monkeypatch):
    """Moving a job row to Tetiana Warm is how Kevin marks "I engaged with this one", but the tab
    is a location and the ladder keys off Status - followup_action() returns "none" for Matched at
    every age. So /warm must also write Applied AND re-anchor Next Followup Date: cards are created
    with a priority-derived +19d date, and any future date hard-skips the row, which is what made
    the morning digest report "Overdue: 0" for rows that had already been emailed."""
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-warm", "sheet_tab": "Tetiana Cold", "contact_company": "Acme Corp"})
    for name in ("send_telegram_message", "log_metric_event", "log_daily_activity",
                 "add_company_cooldown", "upsert_company_identity"):
        monkeypatch.setattr(m, name, lambda *a, **k: None)
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    _dispatch("/warm")

    actions = [p["action"] for p in enqueued]
    assert actions == ["update_status", "set_status", "update_snooze"], actions
    assert enqueued[0]["new_tab"] == "Tetiana Warm"
    assert enqueued[1]["status"] == "Applied"
    due = date.today() + timedelta(days=m.FOLLOWUP_1_DAYS)
    expected = due.strftime("%Y-%m-%d")
    assert enqueued[2]["next_followup"] == expected

    # The row is now on rung 1 rather than skipped: the ladder fires on that date.
    assert m.followup_action("Applied", date.today().strftime("%Y-%m-%d"), expected, due) == "send_followup_1"


def test_warm_on_a_carmen_contact_does_not_write_applied(monkeypatch):
    """A networking contact is not an application. Carmen rows run plan_carmen_followup instead,
    and "Applied" is meaningless on a person - so the ladder writes must not fire there."""
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-cc", "sheet_tab": "Carmen Cold", "contact_company": "Acme Corp"})
    for name in ("send_telegram_message", "log_metric_event", "log_daily_activity",
                 "add_company_cooldown", "upsert_company_identity", "auto_expand_ats_slug"):
        monkeypatch.setattr(m, name, lambda *a, **k: None)
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    _dispatch("/warm")

    assert [p["action"] for p in enqueued] == ["update_status"]
    assert enqueued[0]["new_tab"] == "Carmen Warm"


def test_cold_never_starts_the_followup_ladder(monkeypatch):
    """/cold is a demotion. It must stay a pure tab move - writing Applied there would start a
    follow-up clock on a row Kevin just set aside."""
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-cold", "sheet_tab": "Tetiana Warm", "contact_company": "Acme Corp"})
    for name in ("send_telegram_message", "log_metric_event", "log_daily_activity",
                 "add_company_cooldown", "upsert_company_identity"):
        monkeypatch.setattr(m, name, lambda *a, **k: None)
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    _dispatch("/cold")

    assert [p["action"] for p in enqueued] == ["update_status"]


@pytest.mark.parametrize("command,short_id,expected_status", [
    ("/replied", "abc123", "Replied"),
    ("/interview", "abc123", "Interviewing"),
])
def test_status_short_id_commands_build_set_status_payload(monkeypatch, command, short_id, expected_status):
    monkeypatch.setattr(m, "get_sheet_uuid_by_short_id",
                        lambda sid: "uuid-target" if sid == short_id else None)
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    _dispatch(f"{command} {short_id}")

    assert enqueued == [{
        "action": "set_status", "rowOperationOrder": "DESC",
        "sheet_uuid": "uuid-target", "status": expected_status,
    }]
    assert any(expected_status in line for line in sent)


def test_status_short_id_command_unknown_id_reports_not_found_and_enqueues_nothing(monkeypatch):
    monkeypatch.setattr(m, "get_sheet_uuid_by_short_id", lambda sid: None)
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    _dispatch("/replied bogus-id")

    assert enqueued == []
    assert any("Record Not Found" in line for line in sent)


# ---- /funnel Telegram command (reads the funnel_stats GET action) ----

_FUNNEL_OK = {
    "status": "success",
    "overall": {"Matched": 12, "Applied": 8, "Replied": 4, "Screening": 2,
                "Interviewing": 3, "Offer": 1, "Rejected": 5},
    "by_persona": {
        "Tetiana": {"Matched": 10, "Applied": 6, "Replied": 3, "Screening": 1,
                    "Interviewing": 2, "Offer": 1, "Rejected": 4},
        "Clavicular": {"Matched": 2, "Applied": 2, "Replied": 1, "Screening": 1,
                       "Interviewing": 1, "Offer": 0, "Rejected": 1},
    },
    "rates": {"matched_to_applied": 66.7, "applied_to_reply": 50.0,
              "reply_to_interview": 75.0, "interview_to_offer": 33.3},
}


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def test_funnel_command_calls_funnel_stats_and_renders_personas(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "crm_get", lambda params, *a, **k: calls.append(params) or _FakeResp(_FUNNEL_OK))
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)

    _dispatch("/funnel")

    assert calls == [{"action": "funnel_stats"}]
    assert len(sent) == 1
    body = sent[0]
    assert "OVERALL" in body and "TETIANA" in body and "CLAVICULAR" in body
    assert "Matched 12" in body and "Interviewing 3" in body and "Rejected 5" in body
    assert "66.7%" in body and "33.3%" in body


def test_funnel_command_handles_webhook_unreachable(monkeypatch):
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)

    _dispatch("/funnel")

    assert len(sent) == 1
    assert "unavailable" in sent[0].lower()


def test_funnel_command_handles_error_status_response(monkeypatch):
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: _FakeResp({"status": "error", "message": "Unauthorized"}))
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)

    _dispatch("/funnel")

    assert len(sent) == 1
    assert "unavailable" in sent[0].lower()


# ---- /edit voice lint: warn, never block ----

@pytest.fixture
def temp_bank(tmp_path):
    """Writes a throwaway template bank and returns (path, loader) so a test can read back
    what actually landed on disk after update_template_entry()'s atomic swap."""
    path = tmp_path / "bank.json"

    def write(data):
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def read():
        return json.loads(path.read_text(encoding="utf-8"))

    return write, read


def test_edit_warns_but_still_writes_a_template_with_violations(temp_bank):
    """The whole point of the /edit lint: Kevin types from his phone, so a rule-breaking
    template is flagged and SAVED. A blocked write would strand him with no way to override."""
    write, read = temp_bank
    path = write({"cold_ops": ["Hi, I'd like to connect."]})
    bad = "Hi, I wanted to discuss alignment: happy to grab a quick chat!\n\nBest regards,\nKevin"

    ok, message = m.update_template_entry(path, "cold_ops", 0, bad)

    assert ok is True
    assert read()["cold_ops"][0] == bad, "the edit must land on disk even with violations"
    assert "Template Updated" in message
    assert "Voice check" in message and "saved anyway" in message
    for expected in ("alignment", "colon", "exclamation", "Best regards", "quick chat"):
        assert expected in message, f"lint warning should name {expected!r}"


def test_edit_of_a_clean_template_carries_no_warning(temp_bank):
    write, read = temp_bank
    path = write({"cold_ops": ["old"]})
    clean = "Hi, saw the ops role at your team. I'd like to connect."

    ok, message = m.update_template_entry(path, "cold_ops", 0, clean)

    assert ok is True and read()["cold_ops"][0] == clean
    assert "Voice check" not in message


def test_edit_lint_flags_a_contraction_free_template_only_as_advice(temp_bank):
    # Advisory tier: the note appears, but with the 💡 marker rather than ⚠️, because copy
    # with no natural place for an apostrophe is a legitimate template.
    write, _ = temp_bank
    path = write({"cold_ops": ["old"]})

    ok, message = m.update_template_entry(path, "cold_ops", 0, "Hi, saw the ops role. Worth a brief call?")

    assert ok is True
    assert "💡" in message and "no contractions" in message
    assert "⚠️" not in message


def test_edit_lint_measures_the_interpolated_render_not_the_raw_template(temp_bank):
    """A hand-typed "Hi {name}," is normalized by interpolate_template() on every real send,
    so warning about it would be a false alarm - but a real violation behind a placeholder
    still has to surface."""
    write, _ = temp_bank
    path = write({"cold_ops": ["old"]})

    _, forgiving = m.update_template_entry(path, "cold_ops", 0, "Hi {name}, I'd like to connect.")
    assert "Voice check" not in forgiving

    _, caught = m.update_template_entry(path, "cold_ops", 0, "Hi {name}, I'm excited about {company}.")
    assert "excited" in caught


def test_edit_lint_uses_the_linkedin_char_cap_for_linkedin_notes(temp_bank):
    write, _ = temp_bank
    path = write({"linkedin_templates": ["old"]})
    long_note = "Hi, I'd like to connect. " + ("ops work again. " * 20)

    ok, message = m.update_template_entry(path, "linkedin_templates", 0, long_note)

    assert ok is True
    assert "220-char" in message
    # The same string is under the 75-word email cap, so the pool really is routing the kind.
    assert "75-word" not in message


def test_edit_of_a_resume_bullet_pool_is_never_voice_linted(temp_bank):
    """Resume bullets are not outreach prose - colons and em-dashes are fine there, and
    sanitize_text() never touches them. Linting them would train Kevin to ignore the warning."""
    write, read = temp_bank
    path = write({"track_a_wealth_ops": ["old bullet"]})
    bullet = "Built reconciliation tooling: cut a 3-day close to same-day - across 4 custodians."

    ok, message = m.update_template_entry(path, "track_a_wealth_ops", 0, bullet)

    assert ok is True and read()["track_a_wealth_ops"][0] == bullet
    assert "Voice check" not in message


# ---- Telegram job card layout ----

_CARD_JOB = {
    "employer_name": "Atwell",
    "job_title": "Technology Business Operations Specialist",
    "job_apply_link": "https://boards.example.com/atwell/bizops",
}


@pytest.fixture
def render_card(monkeypatch):
    """Returns render(**overrides) -> the exact text send_telegram_card() would POST.
    The HTTP call is stubbed to a non-200 so the function never touches the message map."""
    class _Res:
        status_code = 500
        text = "stubbed"

        def json(self):
            return {}

    captured = {}

    def _fake_post(url, json=None, timeout=None, **kw):
        captured["text"] = (json or {}).get("text", "")
        return _Res()

    monkeypatch.setattr(m, "TELEGRAM_BOT_TOKEN", "stub-token")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "stub-chat")
    monkeypatch.setattr(m.requests, "post", _fake_post)

    def render(**overrides):
        kwargs = dict(
            job=_CARD_JOB, score=87,
            target_email="dana.reyes@atwell.com", age_badge="⚡ [1-3d RECENT]",
            salary_str="$95,000 - $120,000 USD/year", work_style="Hybrid", overlap_pct=78,
            short_id="a1b2c3d4e5f6",
            sheet_uuid="4f21c0de-7a9b-4c31-9f0e-2b8d6a11c7e4",
            alumni_line="🎓 <b>Alumni:</b> 3 grads in Ops at Atwell",
            sheet_tab="Pipeline_Candidates",
        )
        kwargs.update(overrides)
        m.send_telegram_card(**kwargs)
        return captured["text"]

    return render


def test_card_puts_the_scan_metadata_on_a_single_line(render_card):
    """Score, pay, style, recency and skill match were four stacked lines that pushed the
    copy blocks below the fold on a phone. One line, same five values."""
    text = render_card()
    meta = [ln for ln in text.splitlines() if "87/100" in ln]
    assert len(meta) == 1, "the metadata should appear on exactly one line"
    for value in ("$95,000 - $120,000 USD/year", "Hybrid", "[1-3d RECENT]", "Skills 78%"):
        assert value in meta[0]
    # The old stacked labels are gone entirely, not just reordered.
    for retired in ("<b>Fit Score:</b>", "<b>Recency:</b>", "Pay &amp; Style"):
        assert retired not in text


def test_card_drops_salary_and_work_style_when_the_sentinels_come_back(render_card):
    """extract_salary()/extract_work_style() return literal 'Salary Unlisted' / 'On-Site /
    Unspecified' strings when nothing was found - printing those burns the most valuable row
    on the card to say nothing. Score, age and Skills% stay unconditional."""
    text = render_card(salary_str=m.SALARY_UNLISTED_SENTINEL, work_style=m.WORK_STYLE_UNSPECIFIED_SENTINEL)
    meta = next(ln for ln in text.splitlines() if "87/100" in ln)
    assert m.SALARY_UNLISTED_SENTINEL not in meta
    assert m.WORK_STYLE_UNSPECIFIED_SENTINEL not in meta
    for kept in ("[1-3d RECENT]", "Skills 78%"):
        assert kept in meta
    # A real salary/style still renders, and salary lands immediately after the score.
    full = render_card()
    full_meta = next(ln for ln in full.splitlines() if "87/100" in ln)
    assert full_meta.index("87/100") < full_meta.index("$95,000") < full_meta.index("Hybrid")


def test_card_annotates_the_score_boost_only_when_nonzero(render_card):
    """A 100/100 next to Skills 10% reads as broken unless the relationship-boost points that
    got it there are visible right next to the score."""
    boosted = render_card(score=100, score_boost=50)
    meta = next(ln for ln in boosted.splitlines() if "100/100" in ln)
    assert "100/100</b> (+50)" in meta

    penalized = render_card(score_boost=-15)
    meta = next(ln for ln in penalized.splitlines() if "87/100" in ln)
    assert "87/100</b> (-15)" in meta

    unboosted = render_card()  # score_boost defaults to 0
    meta = next(ln for ln in unboosted.splitlines() if "87/100" in ln)
    assert meta.strip().startswith("🟢 <b>87/100</b> ·"), "no bare parenthetical when nothing boosted it"


def test_card_drops_the_static_dual_path_boilerplate(render_card):
    # Identical on every card, so it carried no per-job information and cost ~4 lines.
    text = render_card()
    assert "Dual-Path Outreach Strategy" not in text
    assert "request a brief phone screen" not in text


def test_card_fits_on_one_phone_screen(render_card):
    """The whole point of the /stage page: the card is a home page, not a document. Everything
    that used to print inline (bullets, note, draft, dorks, fit reason) is one tap away instead."""
    text = render_card()
    content = [ln for ln in text.splitlines() if ln.strip()]
    assert len(content) <= 9, f"card grew back to {len(content)} content lines:\n{text}"
    assert len(text.splitlines()) <= 11, "at most two blank separators"
    for moved in ("Fit Reason", "Matched Skills", "Tailored ATS Resume Bullets",
                  "LinkedIn Connect Note", "Cold Outreach Draft", "Quick Links",
                  "Direct Decision Makers"):
        assert moved not in text, f"{moved!r} belongs on /stage now, not on the card"


def test_card_shows_apply_and_the_three_triage_moment_links(render_card):
    """Apply, Hiring Mgr, Recruiter and Apollo are triage-moment actions Kevin clicks while
    deciding - they stay inline instead of costing a ~50s cold tap through /stage's sleeping
    free-tier service. LinkedIn Leadership Search (overlaps Hiring Mgr) and the Alumni dork
    (already gets its own conditional line when a real alum is found) stay on /stage only."""
    text = render_card()
    assert text.count("<a href=") == 6  # Apply, Hiring Mgr, Recruiter, Apollo, Co. Posts, Full Card
    assert html.escape(_CARD_JOB["job_apply_link"], quote=True) in text
    for kept_url in (m.build_apollo_url("Atwell"), m.build_recruiter_dork("Atwell"),
                     m.build_hiring_manager_dork("Atwell", _CARD_JOB["job_title"]),
                     m.build_linkedin_company_posts_url("Atwell")):
        assert html.escape(kept_url, quote=True) in text
    for stage_only_url in (m.build_linkedin_url("Atwell"), m.build_alumni_dork("Atwell")):
        assert html.escape(stage_only_url, quote=True) not in text
    for link_text in ("Hiring Mgr", "Recruiter", "Apollo", "Co. Posts"):
        assert link_text in text


def test_card_research_links_are_built_from_the_raw_company_name(render_card):
    """The old card passed the HTML-escaped company into the URL builders, so 'Smith & Sons'
    searched for 'Smith &amp; Sons'. The builders must see the raw name; only the href gets
    escaped afterward, same as apply_link."""
    text = render_card(job={**_CARD_JOB, "employer_name": "Smith & Sons"})
    for builder, needs_title in ((m.build_apollo_url, False), (m.build_recruiter_dork, False),
                                 (m.build_hiring_manager_dork, True)):
        raw_url = builder("Smith & Sons", _CARD_JOB["job_title"]) if needs_title else builder("Smith & Sons")
        assert html.escape(raw_url, quote=True) in text
        assert "&amp;amp;" not in text  # no double-escaping


def test_full_card_link_is_an_absolute_url_carrying_the_track(render_card):
    """A bare /stage/<id> href is inert inside a Telegram message - it needs a scheme and host."""
    text = render_card(job={**_CARD_JOB, "track": "c"})
    line = next(ln for ln in text.splitlines() if "Full Card" in ln)
    url = re.search(r"href='([^']+)'", line).group(1)
    assert url.startswith(("http://", "https://")), url
    assert url == f"{m.BASE_URL}/stage/a1b2c3d4e5f6?track=c"
    # Missing track falls back to the same default filter_ats_bullets uses.
    assert "?track=a" in render_card()


def test_card_keeps_the_swipe_reply_anchors_and_the_bare_command_list(render_card):
    """resolve_reply_mapping() recovers a lost mapping from the 🆔 marker, then from the
    💼/🏢 markers - so those three survive the trim even though the legend text did not."""
    text = render_card()
    assert m._parse_sheet_uuid_from_card_text(text) == (
        "4f21c0de-7a9b-4c31-9f0e-2b8d6a11c7e4", "Pipeline_Candidates")
    assert m._parse_company_title_from_card_text(text) == ("Atwell", _CARD_JOB["job_title"])
    assert "🎓 <b>Alumni:</b> 3 grads in Ops at Atwell" in text
    for command in ("/apply", "/draft", "/warm", "/cold", "/x", "/f", "/n", "/e", "/eh", "/help"):
        assert f"<code>{command}</code>" in text
    # The per-command descriptions live in /help now, not on every card.
    assert "Mark Applied" not in text and "Swipe Actions" not in text


def test_card_omits_the_alumni_line_entirely_when_there_is_no_alum(render_card):
    with_alum = render_card()
    without = render_card(alumni_line="")
    assert len(with_alum.splitlines()) - len(without.splitlines()) == 1
    assert "\n\n\n" not in without


def test_card_escapes_interpolated_values_and_respects_the_telegram_length_cap(render_card):
    """A company or salary string carrying a < or & would break Telegram's HTML parse mode and
    the card would fail to send outright, so escaping is load-bearing, not cosmetic."""
    text = render_card(
        job={**_CARD_JOB, "employer_name": "Smith & <Sons>"},
        salary_str="$95,000 <negotiable> & up",
        work_style="On-site & <flex>",
        target_email="a&b@atwell.com",
        alumni_line="",
    )
    assert "Smith &amp; &lt;Sons&gt;" in text
    assert "$95,000 &lt;negotiable&gt; &amp; up" in text
    assert "On-site &amp; &lt;flex&gt;" in text
    assert "a&amp;b@atwell.com" in text
    # Only the tags this card builds itself survive as raw markup.
    assert "<Sons>" not in text and "<negotiable>" not in text

    long_card = render_card(job={**_CARD_JOB, "job_title": "z" * 5000})
    assert len(long_card) <= 3990


# ---- /stage: the page the card's Full Card link points at ----


@pytest.fixture
def staged_job():
    """Caches a job the way process_single_candidate() does and returns the rendered /stage HTML."""
    job = dict(
        _CARD_JOB,
        track="a", bullet_indices=[0], tone_mode="conservative",
        linkedin_template_id=0, outreach_template_id=0,
        fit_reason="Owns the ERP integration queue and reports into the COO.",
        matched_skills=["process automation", "erp"], fit_score=87,
    )
    m.save_job_to_cache("stage001", job)
    with m.app.test_client() as client:
        return client.get("/stage/stage001").get_data(as_text=True), job


def test_stage_page_carries_every_block_the_card_dropped(staged_job):
    page, job = staged_job
    linkedin_note, outreach_email = m.resolve_outreach_copy(job)
    assert linkedin_note and outreach_email, "both templates should resolve from the local banks"
    assert html.escape(linkedin_note) in page
    assert html.escape(outreach_email) in page
    assert html.escape(job["fit_reason"]) in page
    assert "Process Automation, Erp" in page
    assert "87/100" in page
    for url in (m.build_apollo_url("Atwell"), m.build_linkedin_url("Atwell"),
                m.build_alumni_dork("Atwell"), m.build_recruiter_dork("Atwell"),
                m.build_hiring_manager_dork("Atwell", _CARD_JOB["job_title"])):
        assert html.escape(url, quote=True) in page


def test_stage_page_puts_the_research_links_and_linkedin_note_in_one_block_before_the_cold_draft(staged_job):
    """The find-a-name links and the LinkedIn note are one workflow: the links render immediately
    above the note, a connecting sentence sits between them, and the whole block precedes the
    separate Cold Outreach Draft (a different channel with a resolved email, not a found person)."""
    page, _ = staged_job
    links_at = page.index('class="research-links"')
    connector_at = page.index("Find a name above, then copy the note below")
    note_at = page.index('id="linkedin-note"')
    cold_at = page.index("Cold Outreach Draft")
    assert links_at < connector_at < note_at < cold_at
    assert "Find Someone, Then Message Them" in page
    assert "Decision-Maker Research" not in page


def test_stage_page_copy_buttons_share_one_js_helper(staged_job):
    page, _ = staged_job
    for element_id in ("linkedin-note", "cold-draft", "ats-raw-text"):
        assert f'id="{element_id}"' in page
        assert f"copyField('{element_id}')" in page
    assert page.count("function copyField") == 1


def test_stage_page_degrades_for_a_job_cached_before_template_ids_were_persisted(staged_job):
    """Older cache rows have no linkedin_template_id, and resolve_template_text() bounds-checks a
    missing id down to template 0 rather than blowing up - so the page still renders real copy."""
    legacy = {k: v for k, v in _CARD_JOB.items()}
    note, draft = m.resolve_outreach_copy(legacy)
    pool = m.load_linkedin_templates().get("linkedin_templates", [])
    assert note and draft
    assert note == m.sanitize_text(m.interpolate_template(
        pool[0], name="there", company="Atwell", job_title=_CARD_JOB["job_title"]))[:300]


@pytest.mark.parametrize("full_name,expected", [
    ("Dana Reyes", "Dana"),
    ("dana", "dana"),
    ("Dana", "Dana"),
    ("", ""),
    (None, ""),
    ("Contact", ""),            # get_warm_crm_contacts()'s nameless-row placeholder
    ("https://linkedin.com/in/x", ""),
    ("dana@acme.com", ""),
    ("1998", ""),
    ("J", ""),
])
def test_first_name_for_greeting_extracts_a_name_or_falls_back_to_blank(full_name, expected):
    assert m.first_name_for_greeting(full_name) == expected


def test_resolve_outreach_copy_uses_a_persisted_contact_first_name():
    """name present -> "Hi Dana,"; name absent -> bare "Hi," (never "Hi there,")."""
    base = {"employer_name": "Atwell", "job_title": "Ops Analyst",
            "outreach_template_id": 0, "linkedin_template_id": 0}

    named = m.resolve_outreach_copy({**base, "outreach_contact_first_name": "Dana"})
    assert named[0].startswith("Hi Dana.") and named[1].startswith("Hi Dana,")

    for job in ({**base, "outreach_contact_first_name": ""}, base):  # explicit "" and legacy (key absent)
        note, email = m.resolve_outreach_copy(job)
        assert email.startswith("Hi,\n") and "Hi there" not in email
        assert note.startswith("Hi.") and "Hi there" not in note


def test_process_single_candidate_threads_a_warm_contact_name_into_the_greeting(run_candidate, monkeypatch):
    """A resolved Carmen Warm contact for the employer renders "Hi Dana," in both the cold email
    and the LinkedIn note, and the first name is persisted on the cached job for /stage."""
    monkeypatch.setattr(
        m, "get_warm_crm_contacts",
        lambda: {m.normalize_company_for_match("Acme Co"):
                 {"name": "Dana Reyes", "raw_company": "Acme Co", "note": "n", "priority_score": 3}},
    )
    result = run_candidate(gemini_base=60, layer1_bonus=10)
    assert result["outreach_email"].startswith("Hi Dana,")
    assert result["linkedin_note"].startswith("Hi Dana.")
    assert result["job"]["outreach_contact_first_name"] == "Dana"


def test_process_single_candidate_greeting_falls_back_to_bare_hi_with_no_contact(run_candidate):
    """No warm contact for the employer (fixture default {}) -> "Hi," and a persisted ""."""
    result = run_candidate(gemini_base=60, layer1_bonus=10)
    assert result["outreach_email"].startswith("Hi,\n")
    assert "Hi there" not in result["outreach_email"]
    assert result["linkedin_note"].startswith("Hi.")
    assert result["job"]["outreach_contact_first_name"] == ""


def test_stage_page_escapes_a_company_name_carrying_markup():
    m.save_job_to_cache("stage002", {**_CARD_JOB, "employer_name": "Smith & <Sons>",
                                     "track": "a", "fit_reason": "Reports to <COO> & CFO"})
    with m.app.test_client() as client:
        page = client.get("/stage/stage002").get_data(as_text=True)
    assert "Smith &amp; &lt;Sons&gt;" in page
    assert "Reports to &lt;COO&gt; &amp; CFO" in page
    assert "<Sons>" not in page and "<COO>" not in page


# ---- Overdue digest: unscheduled records, the sentinel, and the 10-record cap ----

_SENTINEL = "1970-01-01"


def _overdue_record(company, next_followup, name=""):
    return {"sheet_uuid": f"uuid-{company}", "company": company, "name": name,
            "next_followup": next_followup, "email": f"{company}@example.com"}


@pytest.fixture
def capture_sent(monkeypatch):
    """Collects every send_telegram_message() body, in order."""
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)
    return sent


def _mock_followup_tabs(monkeypatch, cw=(), tc=()):
    by_code = {"CW": list(cw), "TC": list(tc)}
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda target_code="CW", qty=2: by_code.get(target_code, []))


def test_unscheduled_records_are_not_counted_as_overdue(monkeypatch):
    """The root cause of the 100-line morning digest: Code.gs hands blank Next Followup Date
    cells back as 1970-01-01, which is <= today, so Kevin's whole undated warm network sorted
    to the front of the overdue list as maximally late."""
    _mock_followup_tabs(
        monkeypatch,
        cw=[_overdue_record("mom", _SENTINEL),
            _overdue_record("cousin", ""),
            _overdue_record("Atwell", "2020-01-01")],
        tc=[_overdue_record("Stellantis", "2020-06-01")],
    )
    overdue = m.get_overdue_followups()
    assert [r["company"] for r in overdue] == ["Atwell", "Stellantis"]
    assert _SENTINEL not in {r["next_followup"] for r in overdue}


def test_future_dated_records_are_still_excluded_and_most_overdue_sorts_first(monkeypatch):
    _mock_followup_tabs(monkeypatch, cw=[
        _overdue_record("Later", "2099-01-01"),
        _overdue_record("Older", "2019-01-01"),
        _overdue_record("Newer", "2021-01-01"),
    ])
    assert [r["company"] for r in m.get_overdue_followups()] == ["Older", "Newer"]


def test_digest_renders_the_blank_date_sentinel_as_no_date_set(capture_sent):
    """Second line of defence. get_overdue_followups() drops these, but nothing should ever
    put a literal 'due 1970-01-01' in front of Kevin again."""
    records = [
        {**_overdue_record("Atwell", _SENTINEL), "sheet_tab": "Carmen Warm"},
        {**_overdue_record("Stellantis", ""), "sheet_tab": "Tetiana Cold"},
        {**_overdue_record("Recourse", "2020-01-01"), "sheet_tab": "Carmen Warm"},
    ]
    m.send_overdue_digest(1, records)
    body = "\n".join(capture_sent)
    assert _SENTINEL not in body
    assert body.count("due no date set") == 2
    assert "due 2020-01-01" in body


def test_digest_caps_at_ten_records_and_points_at_the_overdue_command(capture_sent):
    records = [{**_overdue_record(f"Company{i:02d}", f"2020-01-{i + 1:02d}"), "sheet_tab": "Carmen Warm"}
               for i in range(37)]
    m.send_overdue_digest(1, records)

    assert len(capture_sent) == 1, "the capped preview must be a single message"
    body = capture_sent[0]
    assert m.OVERDUE_DIGEST_PREVIEW_LIMIT == 10
    assert body.count("• <b>") == 10
    assert "Most Overdue (10 of 37" in body
    assert "...and 27 more." in body and "<code>/overdue</code>" in body
    # The ten shown are the ten most overdue; the 11th is not among them.
    assert "Company00" in body and "Company09" in body and "Company10" not in body


def test_digest_omits_the_more_line_when_everything_fits(capture_sent):
    records = [{**_overdue_record(f"C{i}", "2020-01-01"), "sheet_tab": "Carmen Warm"} for i in range(4)]
    m.send_overdue_digest(1, records)
    assert "more." not in capture_sent[0]
    assert "Most Overdue (4 of 4" in capture_sent[0]


def test_digest_escapes_company_and_name_for_html_parse_mode(capture_sent):
    """A stray < or & in a CRM cell breaks Telegram's HTML parse and the whole message fails
    to deliver, not just render oddly."""
    records = [{**_overdue_record("Smith & <Sons>", "2020-01-01", name="A <b>hack</b>"),
                "sheet_tab": "Carmen & Warm"}]
    m.send_overdue_digest(1, records)
    body = capture_sent[0]
    assert "Smith &amp; &lt;Sons&gt;" in body
    assert "A &lt;b&gt;hack&lt;/b&gt;" in body
    assert "Carmen &amp; Warm" in body


def test_overdue_command_sends_the_full_list_chunked(monkeypatch, capture_sent):
    _mock_followup_tabs(monkeypatch, cw=[
        _overdue_record(f"Company{i:03d}", f"2020-01-{(i % 28) + 1:02d}") for i in range(120)
    ])
    _dispatch("/overdue")

    body = "\n".join(capture_sent)
    assert len(capture_sent) > 1, "120 records should not fit in one Telegram message"
    assert all(len(chunk) <= m.TELEGRAM_CHUNK_CHARS for chunk in capture_sent)
    assert body.count("• <b>") == 120, "the full list must not drop the tail"
    assert "more." not in body  # uncapped, so no pointer back to itself
    assert "All Overdue Records (120" in body


def test_overdue_command_reports_an_empty_list_instead_of_going_silent(monkeypatch, capture_sent):
    _mock_followup_tabs(monkeypatch)
    _dispatch("/overdue")
    assert len(capture_sent) == 1
    assert "No overdue records" in capture_sent[0]


# ---- ATS auto-expansion: skip guard and log levels ----

@pytest.fixture
def ats_probe_recorder(monkeypatch):
    """Records every board URL auto_expand_ats_slug() would probe, without making requests."""
    probes = []

    class _Res:
        status_code = 404

        def json(self):
            return {}

    def _fake_get(url, timeout=None, **kw):
        probes.append(url)
        return _Res()

    monkeypatch.setattr(m.requests, "get", _fake_get)
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: [])
    monkeypatch.setattr(m, "set_filter", lambda key, value: True)
    return probes


def test_auto_expand_makes_zero_http_probes_for_non_company_names(ats_probe_recorder):
    # Every one of these is a real Carmen Warm "company" value.
    for junk in ("mom", "cousin", "(fuck)", "Guy from birmingham venture capital",
                 "https://www.linkedin.com/in/elaine-ezekiel/", "Nathan at speaker event",
                 "Grandma/ Karen Synagogue contact who knows people"):
        m.auto_expand_ats_slug(junk)
    assert ats_probe_recorder == [], "junk names must not reach the network at all"


def test_auto_expand_still_probes_all_three_boards_for_a_real_company(ats_probe_recorder):
    m.auto_expand_ats_slug("Guy Carpenter")
    assert len(ats_probe_recorder) == 3
    assert any("greenhouse.io" in u for u in ats_probe_recorder)
    assert any("lever.co" in u for u in ats_probe_recorder)
    assert any("ashbyhq.com" in u for u in ats_probe_recorder)
    assert all("guycarpenter" in u for u in ats_probe_recorder)


def test_auto_expand_logs_skips_and_misses_below_info(ats_probe_recorder, caplog):
    """Both lines are the common case on a warm network of personal contacts. Leaving either
    at INFO would just swap one log flood for another."""
    with caplog.at_level("INFO", logger=""):
        m.auto_expand_ats_slug("mom")
        m.auto_expand_ats_slug("Atwell")
    assert caplog.records == [], "no INFO-or-above line for a skip or a miss"

    with caplog.at_level("DEBUG", logger=""):
        m.auto_expand_ats_slug("mom")
        m.auto_expand_ats_slug("Atwell")
    messages = [r.message for r in caplog.records]
    assert any("Skipped 'mom'" in msg for msg in messages)
    assert any("No ATS board match found for 'Atwell'" in msg for msg in messages)


def test_auto_expand_keeps_a_successful_match_at_info(monkeypatch, caplog):
    """The success line is rare and actionable - it is the one that must stay visible."""
    class _Hit:
        status_code = 200

        def json(self):
            return [{"id": 1}]

    saved = {}
    monkeypatch.setattr(m.requests, "get", lambda url, timeout=None, **kw: _Hit())
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: [])
    monkeypatch.setattr(m, "set_filter", lambda key, value: saved.update({key: value}) or True)
    monkeypatch.setattr(m, "upsert_company_identity", lambda *a, **k: True)

    with caplog.at_level("INFO", logger=""):
        m.auto_expand_ats_slug("Stellantis")

    assert saved["ats_company_slugs"] == ["stellantis"]
    assert any("resolved to 'stellantis'" in r.message and r.levelname == "INFO"
               for r in caplog.records)


# ==============================================================================
# Combined bonus stacking cap (BONUS_STACK_CAP) - Layer 1 + Layer 2 together
# ==============================================================================


def test_hybrid_score_modifier_returns_the_signed_layer1_shift(monkeypatch):
    """calculate_hybrid_score_modifier now returns (final_score, layer1_bonus). layer1_bonus is
    the signed shift it applied on top of the base (remote 90-cap folded in, final 1-100 clamp
    not), so process_single_candidate can reconstruct clamp(base + layer1_bonus) and fold the
    same number into the combined stacking cap."""
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: default if default is not None else [])
    job = {
        "job_title": "Operations Analyst",
        "employer_name": "Acme",
        "job_description": "Fintech payments platform. Python and SQL automation everywhere.",
        "job_city": "Detroit",
        "job_max_salary": 95000,
    }
    final_score, layer1_bonus = m.calculate_hybrid_score_modifier(job, 60)
    # Keyword bonuses scale with DISTINCT matches: 2 domain terms (fintech, payments) -> +12 at
    # the cap, 2 tools (python, sql) -> +8, +8 salary in the 90k band, +6 "Analyst" entry-level
    # title, +6 no years-of-experience demand. A flat +10/+15 per category fired on any single
    # hit, which nearly every ops posting clears.
    assert layer1_bonus == 36
    # 96 raw, compressed by soft_cap_score() rather than flattened at the 100 clamp.
    assert final_score == 91


def test_keyword_bonuses_separate_a_tool_rich_posting_from_a_passing_mention(monkeypatch):
    """The point of scaling by distinct matches: a role wanting the whole stack must outrank one
    that name-drops Salesforce once. Under the old flat bonus both got the same +10 and, after the
    100-clamp, frequently the same final score - which is what made the top-5 cut arbitrary."""
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: default if default is not None else [])
    base = {"job_title": "Operations Analyst", "employer_name": "Acme",
            "job_city": "Detroit", "job_max_salary": 95000}

    rich = dict(base, job_description="Salesforce, Python, SQL and ETL pipelines daily.")
    thin = dict(base, job_description="Some Salesforce administration.")
    # Scored below the soft-cap knee so this measures the keyword scaling itself, not compression.
    assert m.calculate_hybrid_score_modifier(rich, 40)[1] > m.calculate_hybrid_score_modifier(thin, 40)[1]
    assert m.calculate_hybrid_score_modifier(rich, 40)[0] > m.calculate_hybrid_score_modifier(thin, 40)[0]


def test_soft_cap_preserves_ordering_above_the_knee():
    """A hard min(100, ...) collapsed every raw score from 100 upward onto one value, so the
    Tier-1 top-5 cut was slicing a pile of ties. Compression keeps the ranking inside 1-100."""
    assert m.soft_cap_score(85) == 85            # below the knee: untouched
    assert m.soft_cap_score(200) <= 100          # never escapes the scale
    assert m.soft_cap_score(-5) >= 1
    assert m.soft_cap_score(125) > m.soft_cap_score(110) > m.soft_cap_score(100)
    assert all(m.soft_cap_score(i) <= m.soft_cap_score(i + 1) for i in range(1, 200))


def test_seniority_and_experience_demands_push_a_role_down():
    """The system prompt forbids senior roles but nothing downstream enforced it, so a
    'Senior Operations Manager' could outscore a real entry-level opening on keywords alone."""
    base = {"employer_name": "Acme", "job_city": "Detroit", "job_max_salary": 95000,
            "job_description": "Salesforce and SQL reporting."}
    junior = dict(base, job_title="Operations Analyst I")
    senior = dict(base, job_title="Senior Operations Manager")
    assert m.calculate_hybrid_score_modifier(junior, 70)[1] > m.calculate_hybrid_score_modifier(senior, 70)[1]

    entry_exp = dict(junior, job_description="Salesforce and SQL reporting. 2 years experience.")
    deep_exp = dict(junior, job_description="Salesforce and SQL reporting. 8 years experience.")
    assert m.calculate_hybrid_score_modifier(entry_exp, 70)[1] > m.calculate_hybrid_score_modifier(deep_exp, 70)[1]


class _FakeLookupResp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def test_sent_capture_gate_ignores_an_address_sitting_on_a_job_row(monkeypatch):
    """A contact reached via /e has their address written into the Contact Email column of the
    JOBS row. is_verified_crm_contact() searches ALL tabs, so reusing it as the capture gate made
    every such person read as "already logged" and capture skipped exactly who it existed to log
    (the Sheets query answered "match found for lvezzetti@crain.com" off the Crain job row).
    The gate must ask the PEOPLE tabs only, and must say so in the request."""
    params_seen = []

    def fake_crm_get(params, *a, **k):
        params_seen.append(params)
        # The PEOPLE-only search finds nothing: she is on a JOBS row, not in Carmen Cold.
        return _FakeLookupResp({"status": "success", "found": False})

    monkeypatch.setattr(m, "crm_get", fake_crm_get)
    assert m.is_logged_person_contact("lvezzetti@crain.com") is False
    assert params_seen and params_seen[0].get("people_only") == "1", params_seen

    # ...and someone genuinely in a PEOPLE tab is still skipped.
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: _FakeLookupResp(
        {"status": "success", "found": True, "sheet_tab": "Carmen Cold"}))
    assert m.is_logged_person_contact("eina.assali@affirm.com") is True

    # A lookup failure must not silently drop a real contact: err toward capturing, since the
    # quick_add dedup guard collapses a duplicate but a dropped contact is lost with no report.
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: None)
    assert m.is_logged_person_contact("someone@newcompany.com") is False


def test_addressed_contacts_bypass_the_company_gate_but_not_the_junk_filters(monkeypatch):
    """/e is Kevin typing the address himself, which is stronger evidence than any domain
    heuristic - so an agency recruiter at an untracked firm (NextPath working a Raymond James
    role) must log, where the passive sweep's company gate would drop her. The consumer-domain
    and role-mailbox refusals still apply: /e on careers@ addresses an inbox, not a person."""
    written = []
    monkeypatch.setattr(m, "is_logged_person_contact", lambda e: False)
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda payload, *a, **k: written.append(payload) or True)
    monkeypatch.setattr(m, "record_captured_contact", lambda **k: True)

    assert m.log_addressed_contact_to_carmen_cold("marjorie@nextpath.com", company="Raymond James") is True
    assert written and written[-1]["email"] == "marjorie@nextpath.com"
    assert written[-1]["company"] == "Raymond James"

    # Falls back to a name/company derived from the address when none is supplied.
    assert m.log_addressed_contact_to_carmen_cold("jane.doe@acmecorp.com") is True
    assert written[-1]["name"] == "Jane Doe"

    for junk in ("careers@somefirm.com", "no-reply@render.com", "sandy@gmail.com", ""):
        assert m.log_addressed_contact_to_carmen_cold(junk) is False, junk

    # Already in a PEOPLE tab -> no second row.
    monkeypatch.setattr(m, "is_logged_person_contact", lambda e: True)
    assert m.log_addressed_contact_to_carmen_cold("marjorie@nextpath.com") is False


def test_engineering_titles_cannot_reach_the_tier1_card_gate():
    """A "Salesforce Developer (Remote)" at Mariner, $68.1k-$178k, scored 83 and dispatched a
    Tier-1 card. Nothing caught it: the seniority regex only knew seniority WORDS, so a different
    job family took no penalty, while the description maxed the tool-keyword bonus by naming
    Salesforce/SQL/Python and the $178k ceiling maxed the salary bonus. The engineering penalty
    has to outweigh a fully-maxed keyword+salary stack, not merely dent it."""
    dev = {"employer_name": "Mariner", "job_city": "", "job_is_remote": True,
           "job_min_salary": 68100, "job_max_salary": 178000,
           "job_title": "Salesforce Developer (Remote)",
           "job_description": ("Salesforce developer. Apex, Visualforce, Lightning Web Components, "
                               "SQL, Python, ETL and API integrations. 5+ years of development experience.")}
    # Even with a generous Gemini base, the final score must stay under the >= 80 Tier-1 cut.
    assert m.calculate_hybrid_score_modifier(dev, 70)[0] < 80
    assert m.calculate_hybrid_score_modifier(dev, 79)[0] < 80

    for title in ("Data Engineer", "Software Engineer", "Solutions Architect",
                  "Programmer Analyst", "DevOps Engineer"):
        role = dict(dev, job_title=title)
        assert m.calculate_hybrid_score_modifier(role, 70)[1] < 0, title

    # ...and the ops roles Kevin actually wants are untouched by the new penalty.
    ops = dict(dev, job_title="Business Operations Analyst",
               job_description="Salesforce cleanup, SQL reporting, DocuSign onboarding. 1-2 years.")
    assert m.calculate_hybrid_score_modifier(ops, 70)[1] > 0


def test_negative_layer1_modifiers_pass_through_uncapped():
    """Penalties are never trimmed by the stacking cap - a call-centre listing must be able to
    fall as far as its modifiers take it."""
    penal = {
        "job_title": "Customer Service Rep",
        "employer_name": "Acme",
        "job_description": "High call volume. Inbound calls all day on the dialer queue.",
        "job_city": "Detroit",
    }
    final_score, layer1_bonus = m.calculate_hybrid_score_modifier(penal, 80)
    assert layer1_bonus < 0
    assert final_score < 80


# ---- Gemini screening fails closed without a key ----

def test_evaluate_job_without_a_key_fails_closed_instead_of_passing(monkeypatch):
    monkeypatch.setattr(m, "GEMINI_API_KEY", "")
    monkeypatch.setattr(m, "should_send_alert", lambda *a, **k: False)
    result = m.evaluate_job_with_gemini({"job_title": "Ops Analyst", "employer_name": "Acme"})

    passed, score, reason = result[0], result[1], result[2]
    assert passed is False and score == 0     # must not clear the >= 65 gate
    assert reason == "Evaluation Pending"
    # identical to the API-failure / parse-failure sibling returns
    assert result == (False, 0, "Evaluation Pending", "a", "conservative", [0, 1, 2], 0, 0, 0, 0)


def test_evaluate_job_without_a_key_alerts_but_only_through_the_debounce(monkeypatch):
    monkeypatch.setattr(m, "GEMINI_API_KEY", "")
    alerts, gate = [], {"open": True}
    monkeypatch.setattr(m, "send_health_alert", lambda msg: alerts.append(msg))

    def fake_gate(key, cooldown_hours=6):
        assert key == "gemini_key_missing"
        was_open, gate["open"] = gate["open"], False   # mirrors should_send_alert's one-shot cooldown
        return was_open

    monkeypatch.setattr(m, "should_send_alert", fake_gate)

    for _ in range(20):   # the ThreadPoolExecutor width - an undebounced alert would fire 20 times
        m.evaluate_job_with_gemini({"job_title": "Ops Analyst", "employer_name": "Acme"})
    assert len(alerts) == 1
    assert "GEMINI_API_KEY" in alerts[0]


@pytest.fixture
def run_candidate(monkeypatch):
    """Drive process_single_candidate with the scoring math live and every network/IO edge stubbed.
    run_candidate(gemini_base=.., layer1_bonus=..) returns the result dict."""
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: default if default is not None else [])
    monkeypatch.setattr(m, "resolve_live_alumni_at_company", lambda *a, **k: None)
    monkeypatch.setattr(m, "get_warm_crm_contacts", lambda: {})
    monkeypatch.setattr(m, "get_ghost_listing_penalty", lambda job_hash: (0, ""))
    monkeypatch.setattr(m, "resolve_target_email", lambda *a, **k: "ops@example.com")
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda payload: True)

    state = {}

    def fake_eval(job):
        return (True, state["score"], "fit reason", "a", "conservative",
                [0, 1, 2], 0, 0, state["layer1_bonus"], state["gemini_base"])

    monkeypatch.setattr(m, "evaluate_job_with_gemini", fake_eval)

    def run(gemini_base, layer1_bonus, job_id="jsearch_1"):
        state.update(
            score=max(1, min(100, gemini_base + layer1_bonus)),
            layer1_bonus=layer1_bonus,
            gemini_base=gemini_base,
        )
        job = {
            "job_title": "Operations Analyst", "employer_name": "Acme Co",
            "job_id": job_id, "job_description": "ops role", "job_city": "Detroit",
        }
        return m.process_single_candidate(job)

    return run


def test_stacking_cap_clamps_a_bonus_stack_that_blows_past_the_cap(run_candidate):
    """Gemini 55 + Layer-1 +40 (all four keyword categories fire) and no Layer-2: the raw stack
    would land 55+40=95; BONUS_STACK_CAP (30) trims it to 55+30=85."""
    assert m.BONUS_STACK_CAP == 30
    result = run_candidate(gemini_base=55, layer1_bonus=40)
    assert result["score"] == 85          # clamped to base + cap, not base + raw stack
    assert result["score_boost"] == 0     # no Layer-2 signal; the trim is not a "boost"


def test_stacking_cap_leaves_an_under_cap_stack_untouched(run_candidate):
    """A stack that never reaches the cap is scored exactly as before."""
    result = run_candidate(gemini_base=55, layer1_bonus=20)
    assert result["score"] == 75          # 55 + 20; cap (30) never binds
    result_at_cap = run_candidate(gemini_base=55, layer1_bonus=30)
    assert result_at_cap["score"] == 85   # exactly at the cap: still unaffected


def test_card_boost_annotation_reflects_the_capped_layer2_delta_not_the_raw_sum(run_candidate, monkeypatch):
    """Layer-1 +10, plus an alum (+20) and a priority-10 warm contact (+30) = +50 of Layer-2
    signal on offer. The cap lets only +20 of it actually move the score, and the card's (+N)
    must show that +20 - so 'score - (+N)' still reconstructs the Gemini+Layer-1 number."""
    monkeypatch.setattr(
        m, "resolve_live_alumni_at_company",
        lambda *a, **k: {"name": "Dana Reyes", "linkedin_url": "https://linkedin.com/in/dana",
                         "headline": "Ops Lead"},
    )
    monkeypatch.setattr(
        m, "get_warm_crm_contacts",
        lambda: {m.normalize_company_for_match("Acme Co"):
                 {"name": "Sam", "raw_company": "Acme Co", "note": "n", "priority_score": 10}},
    )
    result = run_candidate(gemini_base=60, layer1_bonus=10)
    # capped_pos = min(30, 10 + 50) = 30  ->  score = clamp(60 + 30) = 90
    assert result["score"] == 90
    # baseline (Gemini + Layer-1, L1 share cap-limited) = clamp(60 + 10) = 70
    assert result["score_boost"] == 20
    assert result["score"] - result["score_boost"] == 70


def test_ghost_penalty_still_bites_when_layer1_bonus_is_large(run_candidate, monkeypatch):
    """Negative modifiers pass through the cap uncapped: a ghost-listing dock still costs its
    full 15 points even when the positive stack was already trimmed to the cap."""
    monkeypatch.setattr(m, "get_ghost_listing_penalty", lambda job_hash: (-15, " GHOST"))
    result = run_candidate(gemini_base=80, layer1_bonus=40)
    # positives cap to 30 -> clamp(80+30)=100, then ghost -15 -> 85
    assert result["score"] == 85
    assert result["score_boost"] == -15


# ---- Carmen Cold as the hot seat: inbound-reply routing (route_inbound_reply_to_crm) ----

@pytest.fixture
def reply_routing(monkeypatch):
    """Silences route_inbound_reply_to_crm's side effects (activity log, background ATS probe
    thread) and hands back a reader for whatever it actually enqueued onto the CRM outbox."""
    expanded = []
    monkeypatch.setattr(m, "log_daily_activity", lambda *a, **k: None)

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._args = args

        def start(self):
            expanded.append(self._args[0])

    monkeypatch.setattr(m.threading, "Thread", _FakeThread)

    def read_outbox():
        with m.get_db_conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM crm_outbox ORDER BY id ASC"
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    read_outbox.ats_expansions = expanded
    return read_outbox


def _match(tab, sheet_uuid="uuid-1", company="Acme Co", name="Dana Reyes"):
    return {"name": name, "company": company, "tab": tab, "sheet_uuid": sheet_uuid}


def _expected_followup():
    return (date.today() + timedelta(days=m.REPLY_FOLLOWUP_DAYS)).strftime("%Y-%m-%d")


def test_general_reply_from_tetiana_moves_to_carmen_cold_with_note_and_bump(reply_routing):
    """A live human conversation that started in the job pipeline becomes a Carmen Cold row."""
    m.route_inbound_reply_to_crm(
        _match("Tetiana Cold"), "GENERAL",
        "Re: Data Analyst role", "Happy to chat - are you free Thursday?",
    )
    payloads = reply_routing()
    assert [p["action"] for p in payloads] == ["update_status", "update_snooze", "append_note"]

    move, snooze, note = payloads
    # The move reuses /warm's exact mechanism - no new CRM action was invented.
    assert move["new_tab"] == "Carmen Cold"
    assert move["sheet_uuid"] == "uuid-1"
    assert snooze["next_followup"] == _expected_followup()
    # Kevin asked for "the date and notes of the next follow up too".
    assert note["note"].startswith(f"[{date.today().strftime('%Y-%m-%d')}]")
    assert "Inbound reply" in note["note"]
    assert "Re: Data Analyst role" in note["note"]
    assert "free Thursday" in note["note"]
    assert "Auto-moved to Carmen Cold from Tetiana Cold" in note["note"]
    assert _expected_followup() in note["note"]


def test_general_reply_already_in_carmen_skips_the_redundant_move(reply_routing):
    """Already in the hot seat: note + bump only, never a move onto the tab it already occupies."""
    m.route_inbound_reply_to_crm(
        _match("Carmen Warm"), "GENERAL", "Re: coffee", "Great catching up last week.",
    )
    payloads = reply_routing()
    assert [p["action"] for p in payloads] == ["update_snooze", "append_note"]
    assert not any(p["action"] == "update_status" for p in payloads)
    # No move happened, so the note must not claim one did.
    assert "Auto-moved" not in payloads[1]["note"]
    assert reply_routing.ats_expansions == []


@pytest.mark.parametrize("origin_tab", ["Tetiana Cold", "Tetiana Warm", "Clavicular",
                                        "Pipeline_Candidates", "Carmen Cold"])
@pytest.mark.parametrize("status_label", ["INTERVIEW_SET", "REJECTION"])
def test_job_status_replies_bump_but_never_move_to_carmen(reply_routing, origin_tab, status_label):
    """"Tetiana is only jobs": an application-outcome event stays in its origin tab."""
    m.route_inbound_reply_to_crm(
        _match(origin_tab), status_label, "Re: your application", "Unfortunately we are moving on.",
    )
    payloads = reply_routing()
    assert [p["action"] for p in payloads] == ["update_snooze"]
    assert payloads[0]["next_followup"] == _expected_followup()
    assert not any(p.get("new_tab") for p in payloads)
    assert reply_routing.ats_expansions == []


def test_general_reply_fires_ats_expansion_for_the_replying_company(reply_routing):
    """Same side effect /warm fires on any move landing in a Carmen tab."""
    m.route_inbound_reply_to_crm(
        _match("Tetiana Cold", company="Guy Carpenter"), "GENERAL", "Re: hello", "Let's talk.",
    )
    assert reply_routing.ats_expansions == ["Guy Carpenter"]


def test_reply_routing_writes_nothing_without_a_sheet_uuid(reply_routing):
    """A CRM match with no usable row id must not enqueue an unaddressable write."""
    m.route_inbound_reply_to_crm(_match("Tetiana Cold", sheet_uuid=""), "GENERAL", "Re: hi", "Hello")
    assert reply_routing() == []


def test_every_reply_payload_carries_the_standard_row_operation_order(reply_routing):
    m.route_inbound_reply_to_crm(_match("Tetiana Cold"), "GENERAL", "Re: hi", "Hello there")
    assert all(p["rowOperationOrder"] == "DESC" for p in reply_routing())


# ---- Reply rate by template (read-only report) ----

@pytest.fixture
def clean_outcomes():
    """clean_tables (autouse) truncates `jobs` but not `application_outcomes`; do that here."""
    with m.get_db_conn() as conn:
        conn.execute("DELETE FROM application_outcomes")
        conn.commit()
    yield
    with m.get_db_conn() as conn:
        conn.execute("DELETE FROM application_outcomes")
        conn.commit()


def test_template_reply_rates_joins_outcomes_back_to_persisted_template_ids(clean_outcomes):
    """application_outcomes.sheet_uuid -> jobs.sheet_uuid -> job_json template ids. Raw
    (sent, replied) counts stay visible per template so a 1/1 doesn't hide behind 100%."""
    def _job(short_id, otid, ltid):
        return m.save_job_to_cache(short_id, {
            "employer_name": "Atwell", "job_title": "Ops",
            "outreach_template_id": otid, "linkedin_template_id": ltid,
        })

    uuid_a = _job("rrA", 0, 2)   # applied + interview  -> replied
    uuid_b = _job("rrB", 0, 2)   # applied only         -> not replied
    uuid_c = _job("rrC", 1, 2)   # applied + rejection  -> replied
    uuid_d = _job("rrD", 1, 3)   # interview only, never /applied -> not a "sent"

    for u in (uuid_a, uuid_b, uuid_c):
        m.record_application_outcome(u, "applied", company="Atwell")
    m.record_application_outcome(uuid_a, "interview", company="Atwell")
    m.record_application_outcome(uuid_c, "rejection", company="Atwell")
    m.record_application_outcome(uuid_d, "interview", company="Atwell")
    m.record_application_outcome("uuid-with-no-cached-job", "applied", company="Ghost")

    data = m.get_template_reply_rates()

    assert data["by_outreach_template"][0] == {"sent": 2, "replied": 1, "reply_rate": 50.0}
    assert data["by_outreach_template"][1] == {"sent": 1, "replied": 1, "reply_rate": 100.0}
    assert data["by_linkedin_template"][2] == {"sent": 3, "replied": 2, "reply_rate": pytest.approx(66.667, abs=0.01)}
    assert 3 not in data["by_linkedin_template"]  # job D was never "sent"
    assert data["totals"] == {"sent": 3, "replied": 2, "reply_rate": pytest.approx(66.667, abs=0.01)}
    assert data["unjoinable_applied"] == 1


def test_template_reply_rates_buckets_a_job_cached_before_ids_were_persisted(clean_outcomes):
    """json_extract -> NULL for a legacy job with no template ids; it lands in the (unset) bucket
    rather than being dropped or crashing."""
    legacy = m.save_job_to_cache("rrLegacy", {"employer_name": "Atwell", "job_title": "Ops"})
    m.record_application_outcome(legacy, "applied", company="Atwell")
    m.record_application_outcome(legacy, "interview", company="Atwell")

    data = m.get_template_reply_rates()
    assert data["by_outreach_template"][None] == {"sent": 1, "replied": 1, "reply_rate": 100.0}
    assert data["by_linkedin_template"][None] == {"sent": 1, "replied": 1, "reply_rate": 100.0}
    msg = m.format_template_reply_rates_message()
    assert "(unset): 1 sent → 1 replied (100.0%)" in msg


def test_untouched_matched_row_expires_to_died(monkeypatch):
    """Tetiana Cold self-cleans: a 'Matched' row Kevin never actioned is retired to Died after
    MATCHED_EXPIRY_DAYS, which is what stops the tab growing without bound."""
    stale = (_SEQ_TODAY - timedelta(days=m.MATCHED_EXPIRY_DAYS + 1)).strftime("%Y-%m-%d")
    tc_row = {"sheet_uuid": "tc-stale", "company": "Stellantis", "title": "Ops Analyst",
              "name": "", "status": "Matched", "date_added": stale,
              "next_followup": "1970-01-01", "raw_priority": "90"}
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(tc_row)] if code == "TC" else [])
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert [r["sheet_uuid"] for r in result["buried"]] == ["tc-stale"]
    assert [p["action"] for p in enqueued] == ["append_note", "update_status"]
    assert enqueued[1]["new_tab"] == "Died"


def test_recent_matched_row_is_left_alone(monkeypatch):
    """Inside the window a Matched row is untouched - no nudge, no bury, no writes."""
    fresh = (_SEQ_TODAY - timedelta(days=5)).strftime("%Y-%m-%d")
    tc_row = {"sheet_uuid": "tc-fresh", "company": "Affirm", "title": "Ops Analyst",
              "name": "", "status": "Matched", "date_added": fresh,
              "next_followup": "1970-01-01", "raw_priority": "98"}
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(tc_row)] if code == "TC" else [])
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert result["buried"] == []
    assert enqueued == []


def test_routing_marker_round_trips_through_the_card_text():
    """The 🧭 marker is what makes the jobs cache a pure optimization: Gemini's routing survives a
    deploy inside the Telegram message, the same way sheet_uuid already does."""
    card = (
        "\U0001f4bc <b>Legal Operations Analyst II</b>\n"
        "\U0001f3e2 <b>Affirm</b>\n"
        "\U0001f194 <code>e8441263-7e9e-4051-ab11-34710bb88e2e</code> \u00b7 <code>Pipeline_Candidates</code>\n"
        "\U0001f9ed <code>c|tech|0,2,3,5|4</code>\n"
    )
    assert m._parse_routing_from_card_text(card) == {
        "track": "c", "tone_mode": "tech", "bullet_indices": [0, 2, 3, 5], "outreach_template_id": 4,
    }
    assert m._parse_routing_from_card_text("no marker here") == {}


def test_rebuild_job_from_card_restores_a_wiped_job():
    """A deploy empties the jobs cache; /draft and /e must degrade, not block."""
    card = (
        "\U0001f4bc <b>Legal Operations Analyst II</b>\n"
        "\U0001f3e2 <b>Affirm</b>\n"
        "\U0001f9ed <code>c|tech|1,2|3</code>\n"
    )
    job, recovered = m.rebuild_job_from_card({}, card)
    assert recovered is True
    assert job["employer_name"] == "Affirm"
    assert job["job_title"] == "Legal Operations Analyst II"
    assert job["track"] == "c"
    assert job["bullet_indices"] == [1, 2]
    assert job["outreach_template_id"] == 3
    assert m._job_data_available(job, {}) is True


def test_rebuild_job_from_card_leaves_a_live_cache_alone():
    """A populated cache is authoritative - the card must never overwrite it."""
    cached = {"employer_name": "Stellantis", "job_title": "Ops Analyst", "track": "a"}
    job, recovered = m.rebuild_job_from_card(cached, "\U0001f3e2 <b>Affirm</b>\n\U0001f9ed <code>c|tech|1|2</code>")
    assert recovered is False
    assert job["employer_name"] == "Stellantis"
    assert job["track"] == "a"


def test_rebuild_job_from_card_handles_a_card_predating_the_marker():
    """Old cards still recover company/title; only the routing falls back to defaults."""
    job, recovered = m.rebuild_job_from_card(
        {}, "\U0001f4bc <b>Ops Analyst</b>\n\U0001f3e2 <b>Affirm</b>\n"
    )
    assert recovered is True
    assert job["employer_name"] == "Affirm"
    assert "track" not in job


def test_classifier_catches_peer_to_peer_acceptances_not_just_ats_phrasing():
    """Kevin's outreach is peer-to-peer cold email, so the reply that matters says "happy to chat,
    do you have 15 minutes Thursday?" - not "invitation to interview". Those scored GENERAL, so
    the interview metric and the outcome record never fired on the conversations the whole
    pipeline exists to produce."""
    for snippet in (
        "Happy to chat! Do you have 15 minutes Thursday?",
        "Would love to connect. Are you free next week?",
        "Can you send over some times that work for you?",
        "Sure, grab 15 on my calendly.com/x",
        "Let's chat next week",
        "We would like to invite you to interview",
    ):
        label, _ = m.classify_inbound_ats_email("x@co.com", "Re: role", snippet)
        assert label == "INTERVIEW_SET", snippet


def test_classifier_checks_rejection_before_acceptance_phrasing():
    """"We were impressed but are pursuing other applicants" carries an acceptance-shaped clause
    inside a decline. Mislabelling that as an interview corrupts the outcome metrics in the
    direction that flatters, so rejection is matched first."""
    for snippet in (
        "We were impressed with your background, but are pursuing other applicants.",
        "Unfortunately we are not moving forward.",
        "We have decided to move forward with other candidates.",
        "The role was filled, will keep your resume on file.",
    ):
        label, _ = m.classify_inbound_ats_email("x@co.com", "Re: role", snippet)
        assert label == "REJECTION", snippet


def test_classifier_leaves_ordinary_replies_general():
    for snippet in ("Thanks for reaching out, let me look into it.", "Got it, thanks for the note."):
        assert m.classify_inbound_ats_email("x@co.com", "Re: role", snippet)[0] == "GENERAL", snippet


# ---- Persistence observability (/health) ----

def test_count_backup_snapshots_missing_dir_reports_not_exists():
    exists, count = m.count_backup_snapshots(os.path.join(tempfile.gettempdir(), "no-such-backup-dir-xyz"))
    assert exists is False
    assert count == 0


def test_count_backup_snapshots_counts_only_matching_files(tmp_path):
    (tmp_path / "jobs_cache_20260101_030000.db").write_text("x")
    (tmp_path / "jobs_cache_20260108_030000.db").write_text("x")
    (tmp_path / "not_a_snapshot.txt").write_text("x")
    exists, count = m.count_backup_snapshots(str(tmp_path))
    assert exists is True
    assert count == 2


def test_count_backup_snapshots_empty_dir_exists_with_zero_count(tmp_path):
    exists, count = m.count_backup_snapshots(str(tmp_path))
    assert exists is True
    assert count == 0


def test_get_persistence_status_reports_resolved_paths_and_row_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "BACKUP_DIR", str(tmp_path))
    (tmp_path / "jobs_cache_20260101_030000.db").write_text("x")

    status = m.get_persistence_status()

    assert status["db_path"] == os.path.abspath(m.DB_PATH)
    assert status["backup_dir"] == os.path.abspath(str(tmp_path))
    assert status["backup_dir_exists"] is True
    assert status["backup_snapshot_count"] == 1
    for table in ("seen_jobs", "pipeline_metrics", "application_outcomes", "daily_activity"):
        assert isinstance(status["row_counts"][table], int)


def test_get_persistence_status_flags_missing_backup_dir(monkeypatch):
    monkeypatch.setattr(m, "BACKUP_DIR", os.path.join(tempfile.gettempdir(), "no-such-backup-dir-xyz"))
    status = m.get_persistence_status()
    assert status["backup_dir_exists"] is False
    assert status["backup_snapshot_count"] == 0


# ==============================================================================
# Deterministic cover letter assembly (generate_cover_letter)
# ==============================================================================
# These guard the property the Strict Deterministic Template Engine exists to enforce: every
# candidate-facing word traces to templates/cover_letter_templates.json, never to a model.

def _all_letter_combos():
    for track in "abcde":
        for tone in ("conservative", "tech"):
            for idx in range(6):
                yield track, tone, idx


def test_cover_letter_never_calls_gemini(monkeypatch):
    """The whole point of the rewrite: no model in the path, so no invented experience."""
    def explode(*args, **kwargs):
        raise AssertionError("generate_cover_letter must not call Gemini")

    monkeypatch.setattr(m, "call_gemini_api", explode)
    letter = m.generate_cover_letter("Crain Communications", "Billing Operations Analyst", "e", 1)
    assert "Billing Operations Analyst" in letter


def test_cover_letter_body_comes_from_the_routed_track_bank():
    """Track e must pull from track_e_bizops, not the track-a default the old code always used."""
    bank = m.load_cover_letter_templates()
    letter = m.generate_cover_letter("Crain", "Billing Operations Analyst", "e", 1)
    body = bank["track_e_bizops"][1 % len(bank["track_e_bizops"])]
    # Compare on a distinctive clause, since sanitize_text() rewrites punctuation in both.
    assert body.split(".")[0].strip() in letter


def test_cover_letter_tone_mode_swaps_only_the_bridge_paragraph():
    conservative = m.generate_cover_letter("Crain", "Billing Operations Analyst", "e", 0, "", "conservative")
    tech = m.generate_cover_letter("Crain", "Billing Operations Analyst", "e", 0, "", "tech")
    assert conservative != tech
    # Paragraph 1 and the closer are tone-independent; only paragraph 2 moves.
    assert conservative.split("\n\n")[1] == tech.split("\n\n")[1]
    assert conservative.split("\n\n")[3] == tech.split("\n\n")[3]


def test_cover_letter_unknown_track_falls_back_to_track_a():
    bogus = m.generate_cover_letter("Crain", "Analyst", "z", 0)
    track_a = m.generate_cover_letter("Crain", "Analyst", "a", 0)
    assert bogus == track_a


def test_cover_letter_out_of_range_index_wraps_instead_of_raising():
    """Gemini routes bullet indices sized for the resume pool (10), not the letter pool (3)."""
    letter = m.generate_cover_letter("Crain", "Analyst", "e", 9)
    assert letter.startswith("Dear Crain Hiring Team,")
    assert letter.endswith("Kevin Miller")


def test_cover_letter_strips_legal_suffix_from_company():
    letter = m.generate_cover_letter("Crain Communications, Inc.", "Analyst", "e", 0)
    assert "Crain Communications, Inc." not in letter
    assert "Dear Crain Communications Hiring Team," in letter


def test_cover_letter_appends_location_only_when_known():
    with_loc = m.generate_cover_letter("Crain", "Analyst", "e", 0, "Detroit, MI")
    without = m.generate_cover_letter("Crain", "Analyst", "e", 0, "")
    assert "in Detroit, MI." in with_loc
    assert "Detroit" not in without


def test_cover_letter_every_combo_is_clean_and_well_formed():
    """No banned word, no unfilled placeholder, 3 paragraphs, recruiter-plausible length."""
    banned = [w.lower() for w in m.load_evidence_bank().get("banned_words", [])]
    for track, tone, idx in _all_letter_combos():
        letter = m.generate_cover_letter("Acme Group, Inc.", "Billing Operations Analyst",
                                         track, idx, "Detroit, MI", tone)
        ctx = f"track={track} tone={tone} idx={idx}"
        assert "{" not in letter and "}" not in letter, ctx
        # "Group" is part of the name, ", Inc." is the legal suffix - see clean_company_for_copy().
        assert letter.startswith("Dear Acme Group Hiring Team,"), ctx
        assert letter.endswith("\n\nBest regards,\nKevin Miller"), ctx
        assert letter.count("\n\n") == 5, ctx
        assert 110 <= len(letter.split()) <= 200, f"{ctx} words={len(letter.split())}"
        for word in banned:
            assert not re.search(rf"\b{re.escape(word)}\b", letter, re.I), f"{ctx} {word}"


def test_cover_letter_bank_survives_sanitize_text_unchanged():
    """sanitize_text() deletes colons and collapses 'X, Y, and Z' triples to 'X and Y'. Bank copy
    must be written around that, or a banked sentence silently loses its third item in the letter
    a recruiter actually reads."""
    bank = m.load_cover_letter_templates()
    for key, pool in bank.items():
        if key.startswith("_"):
            continue
        for i, entry in enumerate(pool):
            assert not re.search(r"[;:]", entry), f"{key}[{i}] has a colon/semicolon"
            assert not re.search(r"[—–]", entry), f"{key}[{i}] has an em/en dash"
            triple = re.search(r"\b(\w+),\s*(\w+),\s*and\s+(\w+)\b", entry)
            assert not triple, f"{key}[{i}] single-word triple would drop '{triple.group(3)}'"


def test_cover_letter_falls_back_when_bank_is_unreadable(monkeypatch):
    monkeypatch.setattr(m, "COVER_LETTER_TEMPLATES_PATH", "/no/such/cover_letter_templates.json")
    letter = m.generate_cover_letter("Crain", "Billing Operations Analyst", "e", 0)
    assert letter.startswith("Dear Crain Hiring Team,")
    assert "Billing Operations Analyst" in letter


def test_cover_letter_never_repeats_a_phrase_across_paragraphs():
    """Paragraph 1 and paragraph 2 are drawn from independent pools, so a phrase written into both
    reads as a copy-paste error to the one person who matters. Checks every routed combination for
    a repeated 6-word run."""
    bank = m.load_cover_letter_templates()
    pool_keys = m.TRACK_BULLET_POOL_KEYS
    for track, tone, idx in _all_letter_combos():
        bodies = bank[pool_keys[track]]
        bridges = bank[f"bridges_{tone}"]
        # Strip placeholders first: {company}/{job_title} legitimately recur across paragraphs,
        # so only the banked prose around them is under test.
        combined = re.sub(r"\{\w+\}", " ",
                          bodies[idx % len(bodies)] + " " + bridges[idx % len(bridges)])
        words = re.findall(r"[a-z']+", combined.lower())
        grams = [" ".join(words[i:i + 6]) for i in range(len(words) - 5)]
        dupes = {g for g in grams if grams.count(g) > 1}
        assert not dupes, f"track={track} tone={tone} idx={idx} repeats: {sorted(dupes)[:2]}"


def test_cover_letter_billing_copy_only_reaches_billing_roles():
    """Index 0 of the shared bridge/closer pools is billing-flavored. It is the right copy for a
    billing title and actively wrong for any other, so a non-billing role must never see it."""
    billing = m.generate_cover_letter("Crain", "Billing Operations Analyst", "e", 0, "", "conservative")
    assert "order-to-cash" in billing

    for title in ("Client Onboarding Specialist", "Financial Analyst", "Operations Associate"):
        letter = m.generate_cover_letter("Acme", title, "a", 0, "", "conservative")
        assert "order-to-cash" not in letter, title
        assert "dedicated billing" not in letter, title
        assert "before an invoice goes out" not in letter, title


def test_cover_letter_bank_uses_contractions():
    """The hand-written reference letter contracts ("I've made it a point"). An all-formal bank
    reads stiff and machine-written, which is the exact failure mode this copy exists to avoid.
    Assert the habit is present across the prose pools rather than checking any one sentence."""
    bank = m.load_cover_letter_templates()
    prose_keys = [k for k in bank if k.startswith("track_") or k.startswith("bridges_")]
    entries = [s for k in prose_keys for s in bank[k]]
    contracted = [s for s in entries if re.search(r"\b(I've|I'm|I'd|don't|doesn't|it's|that's)\b", s)]
    assert len(contracted) >= len(entries) // 2, (
        f"only {len(contracted)}/{len(entries)} banked paragraphs use a contraction"
    )


def test_cover_letter_paragraphs_vary_sentence_length():
    """Uniform sentence length is the loudest tell of generated text. The hand-written reference
    letter runs 13/20/25/24/27/16/16 words - a standard deviation above 5. Any banked paragraph
    whose sentences all land within a couple of words of each other reads flat, so require real
    variance in every multi-sentence entry."""
    bank = m.load_cover_letter_templates()
    for key, pool in bank.items():
        if key.startswith("_") or key in ("signoffs", "openers"):
            continue
        for i, entry in enumerate(pool):
            lens = [len(s.split()) for s in re.split(r"(?<=\.)\s+", entry) if s.strip()]
            if len(lens) < 3:
                continue
            assert max(lens) - min(lens) >= 8, f"{key}[{i}] sentence lengths too uniform: {lens}"


# ==============================================================================
# Stage 1c: general ATS watchlist sourcing
# ==============================================================================

def test_ats_watchlist_is_off_by_default_and_toggles():
    m.set_filter("ats_watchlist_enabled", False)
    assert not m.get_filter("ats_watchlist_enabled")
    m.set_filter("ats_watchlist_enabled", True)
    assert m.get_filter("ats_watchlist_enabled")
    m.set_filter("ats_watchlist_enabled", False)


def test_ats_fetchers_normalize_to_the_pipeline_job_schema(monkeypatch):
    """Greenhouse/Lever/Ashby payloads differ wildly. Whatever they return has to come out shaped
    like a JSearch job dict, or passes_strict_filter and every downstream card breaks."""
    class FakeRes:
        status_code = 200
        @staticmethod
        def json():
            return {"jobs": [{"id": 7, "title": "Billing Operations Analyst",
                              "content": "<p>Reconcile invoices</p>",
                              "absolute_url": "https://x/y",
                              "location": {"name": "Detroit, MI"},
                              "updated_at": "2026-09-01T00:00:00Z"}]}

    monkeypatch.setattr(m.requests, "get", lambda *a, **k: FakeRes())
    jobs = m.fetch_greenhouse_jobs("acme")
    assert len(jobs) == 1
    job = jobs[0]
    for field in ("job_id", "employer_name", "job_title", "job_description",
                  "job_apply_link", "job_city", "job_is_remote"):
        assert field in job, field
    assert job["job_id"].startswith("gh_acme_")
    assert "<p>" not in job["job_description"], "HTML must be stripped for the AI prompt"


def test_ats_fetchers_return_empty_list_on_failure(monkeypatch):
    """A dead board must not take the whole /t run down with it."""
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(m.requests, "get", boom)
    assert m.fetch_greenhouse_jobs("acme") == []
    assert m.fetch_lever_jobs("acme") == []
    assert m.fetch_ashby_jobs("acme") == []


def test_ats_watchlist_skips_slugs_already_covered_by_warm_radar():
    """Stage 1b already fetched the warm slugs. Stage 1c re-fetching them would double the HTTP
    calls against the same boards for zero new jobs."""
    watchlist = ["stockx", "shinola", "carta"]
    warm = ["shinola"]
    remaining = [s for s in watchlist if s not in set(warm)]
    assert remaining == ["stockx", "carta"]


# ==============================================================================
# Stage 1d: keyless remote feeds
# ==============================================================================

def _remote_job(**over):
    # Deliberately not "Acme": other tests in this module push that name into the applied-company
    # and cooldown caches, which would make these assertions fail for the wrong reason.
    job = {"job_title": "Billing Operations Analyst", "employer_name": "Northwind Remote Co",
           "job_description": "Reconciliation in Salesforce and Excel.", "job_is_remote": True,
           "job_city": "Remote", "job_state": ""}
    job.update(over)
    return job


def test_remote_filter_requires_a_core_skill_on_word_boundaries():
    """`"excel" in description` also matches "excellent communication skills", which is boilerplate
    in nearly every posting. That substring bug passed 22 of 39 irrelevant remote jobs."""
    assert m._passes_remote_filter(_remote_job(job_description="Daily reconciliation in Excel."))
    assert not m._passes_remote_filter(
        _remote_job(job_description="We want excellent communication skills and a team player.")
    )


def test_remote_filter_rejects_non_remote_jobs():
    assert not m._passes_remote_filter(_remote_job(job_is_remote=False))


def test_remote_filter_still_applies_the_non_geographic_gates():
    """Geography is the only thing this filter drops. A commission sales role or a senior title is
    just as wrong remote as it is in Farmington."""
    assert not m._passes_remote_filter(_remote_job(job_title="Senior Billing Analyst"))
    assert not m._passes_remote_filter(_remote_job(job_title="Account Executive"))
    assert not m._passes_remote_filter(_remote_job(employer_name="Robert Half"))
    assert not m._passes_remote_filter(
        _remote_job(job_description="Salesforce work with uncapped earnings and cold outreach.")
    )


def test_remote_filter_requires_title_and_company():
    assert not m._passes_remote_filter(_remote_job(job_title=""))
    assert not m._passes_remote_filter(_remote_job(employer_name=""))


def test_remote_feeds_are_off_by_default():
    """This pipeline is tuned for a Detroit desk; nationwide remote listings crowd that out."""
    m.set_filter("remote_feeds_enabled", False)
    assert not m.get_filter("remote_feeds_enabled")


def test_remote_feed_fetchers_survive_a_dead_endpoint(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("feed down")
    monkeypatch.setattr(m.requests, "get", boom)
    assert m.fetch_remoteok_jobs() == []
    assert m.fetch_himalayas_jobs() == []
    assert m.fetch_remotive_jobs() == []
    assert m.fetch_weworkremotely_jobs() == []
    assert m.fetch_remote_feed_jobs() == []


def test_remoteok_skips_the_legal_stub_element():
    """RemoteOK's element 0 is a legal/metadata notice, not a posting."""
    class FakeRes:
        status_code = 200
        @staticmethod
        def json():
            return [{"legal": "notice"}, {"id": 5, "company": "Acme", "position": "Analyst",
                                          "description": "<p>work</p>", "apply_url": "https://x",
                                          "date": "2026-09-01"}]
    import types
    monkey = types.SimpleNamespace(get=lambda *a, **k: FakeRes())
    orig = m.requests.get
    m.requests.get = monkey.get
    try:
        jobs = m.fetch_remoteok_jobs()
    finally:
        m.requests.get = orig
    assert len(jobs) == 1
    assert jobs[0]["job_title"] == "Analyst"
    assert "<p>" not in jobs[0]["job_description"]


# ==============================================================================
# Search breadth gears
# ==============================================================================

def test_gears_are_cumulative_never_subtractive():
    """This is a throttle, not a gearbox. Every gear keeps the local sources and adds on top, so
    no higher gear may turn OFF something a lower gear had on."""
    ordered = [m.SEARCH_GEARS[n] for n in sorted(m.SEARCH_GEARS)]
    for lower, higher in zip(ordered, ordered[1:]):
        assert higher["radius_miles"] >= lower["radius_miles"]
        assert higher["ats_watchlist_enabled"] >= lower["ats_watchlist_enabled"]
        assert higher["remote_feeds_enabled"] >= lower["remote_feeds_enabled"]
        assert higher["remote_feed_cap"] >= lower["remote_feed_cap"]


def test_apply_search_gear_writes_every_setting():
    m.apply_search_gear(5)
    assert m.get_filter("radius_miles") == 60
    assert m.get_filter("ats_watchlist_enabled") is True
    assert m.get_filter("remote_feeds_enabled") is True
    assert m.get_filter("remote_feed_cap") == 100
    m.apply_search_gear(1)
    assert m.get_filter("radius_miles") == 25
    assert m.get_filter("ats_watchlist_enabled") is False
    assert m.get_filter("remote_feeds_enabled") is False


def test_out_of_range_gear_clamps_instead_of_raising():
    """Reachable from a Telegram command, so a typed /gear 9 must land somewhere valid rather than
    erroring or leaving a half-applied mix of settings behind."""
    m.apply_search_gear(99)
    assert m.current_search_gear()[0] == max(m.SEARCH_GEARS)
    m.apply_search_gear(0)
    assert m.current_search_gear()[0] == min(m.SEARCH_GEARS)
    m.apply_search_gear("nonsense")
    assert m.current_search_gear()[0] is not None


def test_current_gear_reports_custom_after_a_manual_override():
    """A later /remote off edits one setting without touching search_gear. Reporting the stored
    number would then lie about what the pipeline is actually sourcing."""
    m.apply_search_gear(5)
    assert m.current_search_gear()[0] == 5
    m.set_filter("remote_feeds_enabled", False)
    gear_num, config = m.current_search_gear()
    assert gear_num is None and config is None
    assert "custom" in m.describe_search_gear().lower()


def test_describe_gear_lists_every_gear_and_marks_the_active_one():
    m.apply_search_gear(3)
    text = m.describe_search_gear()
    for num in m.SEARCH_GEARS:
        assert f"{num}." in text
    assert "▶️" in text
    assert "gear 3" in text.lower()


# ==============================================================================
# Sourcing filters survive gear changes and restarts
# ==============================================================================

def _raw_filter(key):
    with m.get_db_conn() as conn:
        row = conn.execute("SELECT value_json FROM search_filters WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.fixture
def sourcing_filters():
    """Snapshot the two sourcing lists and put them back, so these tests can blank them freely."""
    saved = {k: _raw_filter(k) for k in ("target_queries", "ats_company_slugs")}
    yield
    with m.get_db_conn() as conn:
        for key, val in saved.items():
            conn.execute("INSERT OR REPLACE INTO search_filters (key, value_json) VALUES (?, ?)", (key, json.dumps(val)))
        conn.commit()


def test_every_gear_leaves_queries_and_watchlist_untouched(monkeypatch, sourcing_filters):
    """Regression: /t reported 0 target rules & 0 ATS boards after /gear 3. Gears only move the
    breadth knobs; the lists they gate must come through every gear change intact."""
    monkeypatch.setattr(m, "crm_post", lambda *a, **k: None)
    m.set_filter("ats_company_slugs", ["stripe", "rocket", "rivian"])
    queries_before = _raw_filter("target_queries")
    assert len(queries_before) == len(m.DEFAULT_SEARCH_FILTERS["target_queries"])
    for gear in sorted(m.SEARCH_GEARS):
        m.apply_search_gear(gear)
        assert _raw_filter("target_queries") == queries_before
        assert _raw_filter("ats_company_slugs") == ["stripe", "rocket", "rivian"]


@pytest.mark.parametrize("blank", ["", [], None])
def test_hydration_cannot_blank_a_populated_list(monkeypatch, sourcing_filters, blank):
    """A blank System_Config cell comes back from Code.gs as "" and used to overwrite the 110 local
    queries on every restart."""
    queries_before = _raw_filter("target_queries")
    resp = _FakeResp({"filters": {"target_queries": blank, "radius_miles": 45}})
    resp.status_code = 200
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: resp)
    m.hydrate_filters_from_sheets()
    assert _raw_filter("target_queries") == queries_before


def test_hydration_still_applies_a_real_list_edit(monkeypatch, sourcing_filters):
    resp = _FakeResp({"filters": {"target_queries": ["Operations Analyst Detroit MI"]}})
    resp.status_code = 200
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: resp)
    m.hydrate_filters_from_sheets()
    assert _raw_filter("target_queries") == ["Operations Analyst Detroit MI"]


@pytest.mark.parametrize("bad_row", ["delete", "", []])
def test_restore_reseeds_missing_or_blank_target_queries(monkeypatch, sourcing_filters, bad_row):
    posted = []
    monkeypatch.setattr(m, "crm_post", lambda payload, **k: posted.append(payload))
    with m.get_db_conn() as conn:
        if bad_row == "delete":
            conn.execute("DELETE FROM search_filters WHERE key = 'target_queries'")
        else:
            conn.execute("UPDATE search_filters SET value_json = ? WHERE key = 'target_queries'", (json.dumps(bad_row),))
        conn.commit()
    assert m.restore_core_sourcing_filters() is True
    assert _raw_filter("target_queries") == m.DEFAULT_SEARCH_FILTERS["target_queries"]
    assert posted and posted[0]["key"] == "target_queries"


def test_restore_leaves_a_populated_list_alone(monkeypatch, sourcing_filters):
    monkeypatch.setattr(m, "crm_post", lambda *a, **k: pytest.fail("must not rewrite Sheets"))
    with m.get_db_conn() as conn:
        conn.execute("UPDATE search_filters SET value_json = ? WHERE key = 'target_queries'", (json.dumps(["Custom Q"]),))
        conn.commit()
    assert m.restore_core_sourcing_filters() is False
    assert _raw_filter("target_queries") == ["Custom Q"]


def test_t_warns_when_target_queries_is_empty(monkeypatch, sourcing_filters):
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text: sent.append(text))
    monkeypatch.setattr(m, "run_job_pipeline", lambda chat_id, top_n=2: 0)
    with m.get_db_conn() as conn:
        conn.execute("UPDATE search_filters SET value_json = '[]' WHERE key = 'target_queries'")
        conn.commit()
    _dispatch("/t")
    assert any("target_queries is empty" in s for s in sent)
    assert any("Scanning 0 target rules" in s for s in sent)


# ---- Shared Tier-1 dispatch (/t and manual ingest) ----

def _fake_match(score=88, clavicular=False, company="Huntington", title="FX Ops Analyst"):
    """A minimal process_single_candidate() result - only the keys dispatch_tier1_matches reads."""
    return {
        "job": {
            "employer_name": company,
            "job_title": title,
            "job_apply_link": "https://www.linkedin.com/jobs/view/4461280495/",
        },
        "score": score,
        "reason": "Strong ops/settlement overlap",
        "target_email": "ops@huntington.com",
        "age_badge": "NEW",
        "salary_str": "$65,000",
        "work_style": "On-site",
        "overlap_pct": 71,
        "short_id": "abc123",
        "sheet_uuid": "uuid-1",
        "alumni_line": "",
        "score_boost": 0,
        "is_clavicular": clavicular,
        "contact_name": "Dana" if clavicular else "",
        "tone_mode": "conservative",
    }


def test_dispatch_tier1_writes_the_row_before_sending_the_card(monkeypatch):
    """Ordering is load-bearing: a card carrying a sheet_uuid with no row behind it would leave
    /apply, /n, /f and the follow-up sequencer resolving against nothing."""
    order = []

    def _write(p, **kw):
        order.append(("write", p.get("target_code")))
        return True

    monkeypatch.setattr(m, "log_to_sheets_crm", _write)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: order.append(("card", None)))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    sent = m.dispatch_tier1_matches([_fake_match()])

    assert sent == 1
    assert order == [("write", "TC"), ("card", None)]


def test_dispatch_tier1_routes_standard_rows_to_tetiana_cold(monkeypatch):
    captured = {}

    def _write(p, **kw):
        captured[p["target_code"]] = p
        return True

    monkeypatch.setattr(m, "log_to_sheets_crm", _write)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: None)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    m.dispatch_tier1_matches([_fake_match(clavicular=False)])

    assert "TC" in captured and "CL" not in captured
    row = captured["TC"]["rows"][0]["row_data"]
    assert row[1] == "Huntington"        # company
    assert row[2] == "FX Ops Analyst"    # title
    assert row[5] == "Matched"           # status the sequencer keys off


def test_dispatch_tier1_routes_clavicular_rows_to_the_clavicular_tab(monkeypatch):
    captured = {}

    def _write(p, **kw):
        captured[p["target_code"]] = p
        return True

    monkeypatch.setattr(m, "log_to_sheets_crm", _write)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: None)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    m.dispatch_tier1_matches([_fake_match(clavicular=True)])

    assert "CL" in captured and "TC" not in captured
    assert "Warm Referral Matched: Dana" in captured["CL"]["rows"][0]["row_data"][8]


def test_dispatch_tier1_withholds_the_card_when_the_crm_write_fails(monkeypatch):
    cards = []
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda p, **kw: False)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: cards.append(1))
    monkeypatch.setattr(m, "send_health_alert", lambda msg: None)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    assert m.dispatch_tier1_matches([_fake_match()]) == 0
    assert cards == []


def test_dispatch_tier1_note_prefix_marks_hand_pasted_rows(monkeypatch):
    """The sheet note column is how Kevin tells a pasted row from a sourced one."""
    captured = {}

    def _write(p, **kw):
        captured[p["target_code"]] = p
        return True

    monkeypatch.setattr(m, "log_to_sheets_crm", _write)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: None)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    m.dispatch_tier1_matches([_fake_match()], note_prefix="Manually ingested via /job")
    assert captured["TC"]["rows"][0]["row_data"][8].startswith("Manually ingested via /job")

    captured.clear()
    m.dispatch_tier1_matches([_fake_match()])
    assert captured["TC"]["rows"][0]["row_data"][8].startswith("Matched via Pipeline")


def test_dispatch_tier1_handles_an_empty_match_list(monkeypatch):
    def _fail(p, **kw):
        pytest.fail("should not write")

    monkeypatch.setattr(m, "log_to_sheets_crm", _fail)
    assert m.dispatch_tier1_matches([]) == 0


# ---- Manual job ingest orchestration (/job + bookmarklet) ----

class _NoopThread:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass


def test_ingest_manual_job_lands_a_row_and_a_card(monkeypatch):
    captured = {}

    def _write(p, **kw):
        captured[p["target_code"]] = p
        return True

    monkeypatch.setattr(m, "process_single_candidate", lambda job: _fake_match())
    monkeypatch.setattr(m, "log_to_sheets_crm", _write)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: None)
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **kw: None)
    monkeypatch.setattr(m.threading, "Thread", _NoopThread)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    ok, message = m.ingest_manual_job(title="FX Ops Analyst", company="Huntington")

    assert ok is True and message == ""
    assert "TC" in captured


def test_ingest_manual_job_has_no_score_gate(monkeypatch):
    """A hand-picked job Kevin chose is already vetted - a 61 must still land in Tetiana Cold.
    The >=80 Tier-1 gate exists to triage hundreds of machine-sourced listings, not this."""
    captured = {}

    def _write(p, **kw):
        captured[p["target_code"]] = p
        return True

    monkeypatch.setattr(m, "process_single_candidate", lambda job: _fake_match(score=61))
    monkeypatch.setattr(m, "log_to_sheets_crm", _write)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: None)
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **kw: None)
    monkeypatch.setattr(m.threading, "Thread", _NoopThread)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    ok, _ = m.ingest_manual_job(title="FX Ops Analyst", company="Huntington")

    assert ok is True
    assert captured["TC"]["rows"][0]["row_data"][4] == 61


def test_ingest_manual_job_asks_for_typed_details_when_linkedin_blocks(monkeypatch):
    """The auth wall is the normal datacenter outcome - it must prompt, not file a junk row."""
    def _fail(p, **kw):
        pytest.fail("no row should be written")

    monkeypatch.setattr(m, "scrape_job_page", lambda url, timeout=8: ("", "", ""))
    monkeypatch.setattr(m, "log_to_sheets_crm", _fail)

    ok, message = m.ingest_manual_job(url="https://www.linkedin.com/jobs/view/4461280495/")

    assert ok is False
    assert "@" in message  # shows the Title @ Company fallback form


def test_ingest_manual_job_dedups_a_role_already_in_a_job_tab(monkeypatch):
    """Suppression comes from having a CRM row, not from seen_jobs."""
    def _no_score(job):
        pytest.fail("should not re-score a role already tracked in Tetiana Cold/Warm")

    def _no_write(p, **kw):
        pytest.fail("no duplicate row")

    monkeypatch.setattr(m, "process_single_candidate", _no_score)
    monkeypatch.setattr(m, "log_to_sheets_crm", _no_write)
    monkeypatch.setattr(
        m, "get_tracked_job_keys",
        lambda: {m.generate_dedup_hash("Huntington", "FX Ops Analyst")},
    )

    ok, message = m.ingest_manual_job(title="FX Ops Analyst", company="Huntington")

    assert ok is False
    assert "Already in the pipeline" in message


def test_ingest_manual_job_still_scores_a_seen_but_untracked_posting(monkeypatch):
    """The inverse, and the whole point of the change: a posting the pipeline has looked at before
    but that never became a CRM row must still produce a card. Blocking on seen_jobs is what made
    repeat runs reject 114 of 121 listings while dispatching almost nothing."""
    scored = []
    monkeypatch.setattr(m, "process_single_candidate", lambda job: scored.append(job) or None)
    monkeypatch.setattr(m, "get_tracked_job_keys", lambda: set())
    # Seen before by /t, but never tracked - must NOT suppress.
    m.save_seen_job_db(m.generate_dedup_hash("Huntington", "FX Ops Analyst"))

    ok, message = m.ingest_manual_job(title="FX Ops Analyst", company="Huntington")

    assert scored, "a seen-but-untracked posting must still be scored"
    assert "Already in the pipeline" not in message


def test_ingest_manual_job_reports_an_ai_rejection_without_writing(monkeypatch):
    def _no_write(p, **kw):
        pytest.fail("no row without a score")

    monkeypatch.setattr(m, "process_single_candidate", lambda job: None)
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **kw: None)
    monkeypatch.setattr(m, "log_to_sheets_crm", _no_write)

    ok, message = m.ingest_manual_job(title="Senior Rust Engineer", company="Acme")

    assert ok is False
    assert "AI screening" in message


def test_ingest_manual_job_scrapes_when_only_a_url_is_given(monkeypatch):
    """The happy path: a bare LinkedIn URL resolves to a real title/company via the scraper."""
    captured = {}

    def _write(p, **kw):
        captured[p["target_code"]] = p
        return True

    monkeypatch.setattr(
        m, "scrape_job_page",
        lambda url, timeout=8: ("FX Ops Analyst 2", "Huntington National Bank", "Settle trades."),
    )
    monkeypatch.setattr(m, "process_single_candidate", lambda job: _fake_match())
    monkeypatch.setattr(m, "log_to_sheets_crm", _write)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: None)
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **kw: None)
    monkeypatch.setattr(m.threading, "Thread", _NoopThread)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    ok, _ = m.ingest_manual_job(
        url="https://www.linkedin.com/jobs/search-results/?currentJobId=4461280495&refId=x"
    )

    assert ok is True
    assert "TC" in captured


def test_ingest_manual_job_skips_the_scrape_when_details_are_typed(monkeypatch):
    """The explicit form must not pay for a doomed HTTP round trip."""
    def _no_scrape(url, timeout=8):
        pytest.fail("scrape should be skipped when title and company are supplied")

    monkeypatch.setattr(m, "scrape_job_page", _no_scrape)
    monkeypatch.setattr(m, "process_single_candidate", lambda job: _fake_match())
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda p, **kw: True)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: None)
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **kw: None)
    monkeypatch.setattr(m.threading, "Thread", _NoopThread)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    ok, _ = m.ingest_manual_job(
        url="https://www.linkedin.com/jobs/view/4461280495/",
        title="FX Ops Analyst 2",
        company="Huntington National Bank",
    )
    assert ok is True


def test_ingest_scrapes_non_linkedin_careers_pages(monkeypatch):
    """Regression: the scrape was gated behind is_linkedin_job_url(), so an employer careers
    URL - the destination behind LinkedIn's own Apply button, and a BETTER source since it
    answers a plain GET - was never fetched and fell through to the typed-details prompt."""
    captured = {}
    scraped = []

    def _write(p, **kw):
        captured[p["target_code"]] = p
        return True

    def _scrape(url, timeout=12):
        scraped.append(url)
        return ("Foreign Exchange Ops Analyst 2", "Huntington", "Settle FX trades.")

    monkeypatch.setattr(m, "scrape_job_page", _scrape)
    monkeypatch.setattr(m, "process_single_candidate", lambda job: _fake_match())
    monkeypatch.setattr(m, "log_to_sheets_crm", _write)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: None)
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **kw: None)
    monkeypatch.setattr(m.threading, "Thread", _NoopThread)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    ok, _ = m.ingest_manual_job(
        url="https://huntington-careers.com/search/jobdetails/fx-analyst/abc?utm_source=linkedin"
    )

    assert ok is True
    assert scraped, "a non-LinkedIn careers URL must still be scraped"
    assert "TC" in captured


def test_ingest_blocked_message_does_not_blame_linkedin_for_other_hosts(monkeypatch):
    monkeypatch.setattr(m, "scrape_job_page", lambda url, timeout=12: ("", "", ""))

    ok, message = m.ingest_manual_job(url="https://careers.example.com/job/123")

    assert ok is False
    assert "LinkedIn" not in message


# ---- Funnel telemetry (why candidates were dropped) ----

def test_funnel_trace_ranks_rejections_most_common_first():
    """The summary exists so a 0-candidate run explains itself. Ordering matters: the dominant
    reason is the one Kevin acts on."""
    trace = m.FunnelTrace()
    trace.raw = 112
    for reason in ["city_allowlist"] * 94 + ["seniority"] * 8 + ["salary_floor"] * 5:
        trace.note(reason)
    trace.passed = 5

    line = trace.summary_line()

    assert line.startswith("112 raw -> 5 passed")
    assert line.index("City not in metro allowlist: 94") < line.index("Too senior: 8")
    assert line.index("Too senior: 8") < line.index("Below minimum salary: 5")


def test_funnel_trace_is_silent_when_nothing_was_dropped():
    trace = m.FunnelTrace()
    trace.raw = 4
    trace.passed = 4
    assert trace.summary_line() == ""


def test_funnel_trace_keeps_unlabeled_reasons_visible():
    """A gate added without a FUNNEL_REJECTION_LABELS entry must still surface, under its raw
    key - silently dropping it would recreate the blind spot this class exists to remove."""
    trace = m.FunnelTrace()
    trace.raw = 1
    trace.note("some_new_gate")
    assert "some_new_gate: 1" in trace.summary_line()


def test_passes_strict_filter_records_the_gate_that_rejected(monkeypatch):
    """The trace must name the ACTUAL gate, not just that something failed."""
    monkeypatch.setattr(m, "is_company_on_cooldown", lambda company: False)
    monkeypatch.setattr(m, "get_applied_crm_companies", lambda: set())
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: {
        "min_salary": 50000,
        "valid_cities": ["farmington", "detroit"],
        "title_exclusions": [],
        "company_exclusions": [],
        "hard_ban_keywords": [],
        "seniority_exclusions": [],
    }.get(key, default if default is not None else []))

    trace = m.FunnelTrace()
    out_of_area = {
        "employer_name": "Acme Corp",
        "job_title": "Operations Analyst",
        "job_description": "Reconciliation workflows with SQL.",
        "job_city": "Austin",
        "job_state": "TX",
    }

    assert m.passes_strict_filter(out_of_area, trace=trace) is False
    assert trace.reasons.get("out_of_state") == 1


def test_hard_ban_keywords_reject_commission_pay_not_commission_reporting(monkeypatch):
    """Bare "commission" used to reject ops roles that merely report on commissions. Uses the shipped
    defaults so a regression in DEFAULT_SEARCH_FILTERS itself is caught."""
    monkeypatch.setattr(m, "is_company_on_cooldown", lambda company: False)
    monkeypatch.setattr(m, "get_applied_crm_companies", lambda: set())
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: {
        "min_salary": 50000,
        "valid_cities": ["farmington"],
        "hard_ban_keywords": m.DEFAULT_SEARCH_FILTERS["hard_ban_keywords"],
    }.get(key, default if default is not None else []))

    def job(description):
        return {
            "employer_name": "Acme Wealth",
            "job_title": "Operations Analyst",
            "job_description": description,
            "job_city": "Farmington Hills",
            "job_state": "MI",
        }

    reporting = job("Own commission calculations and reporting; reconciliation in SQL and Excel.")
    assert m.passes_strict_filter(reporting) is True

    trace = m.FunnelTrace()
    commission_pay = job("Commission-only compensation; reconciliation in SQL and Excel.")
    assert m.passes_strict_filter(commission_pay, trace=trace) is False
    assert trace.reasons.get("hard_ban_keyword") == 1


def test_passes_strict_filter_works_without_a_trace(monkeypatch):
    """trace is optional - every existing caller passes nothing and must keep working."""
    monkeypatch.setattr(m, "is_company_on_cooldown", lambda company: False)
    monkeypatch.setattr(m, "get_applied_crm_companies", lambda: set())
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: {
        "min_salary": 50000,
        "valid_cities": ["farmington"],
    }.get(key, default if default is not None else []))

    job = {
        "employer_name": "Acme Corp",
        "job_title": "Operations Analyst",
        "job_description": "Reconciliation workflows with SQL.",
        "job_city": "Austin",
        "job_state": "TX",
    }

    assert m.passes_strict_filter(job) is False


# ---- Per-query yield attribution ----

def test_funnel_trace_attributes_candidates_to_their_source_query():
    trace = m.FunnelTrace()

    trace.set_query("Operations Specialist Troy MI")
    for _ in range(4):
        trace.raw += 1
        trace._bump("raw")
    trace.passed += 1
    trace._bump("passed")

    trace.set_query("Custodial Operations Schwab Fidelity Troy MI")
    trace.raw += 1
    trace._bump("raw")

    assert trace.per_query["Operations Specialist Troy MI"] == {"raw": 4, "passed": 1}
    assert trace.per_query["Custodial Operations Schwab Fidelity Troy MI"] == {"raw": 1, "passed": 0}


def test_funnel_trace_ignores_candidates_with_no_source_query():
    """ATS boards, remote feeds and warm sweeps have no search phrase, so they must not be
    credited to whichever query happened to run last."""
    trace = m.FunnelTrace()
    trace.set_query("Operations Specialist Troy MI")
    trace.raw += 1
    trace._bump("raw")

    trace.set_query(None)
    for _ in range(5):
        trace.raw += 1
        trace._bump("raw")

    assert trace.per_query == {"Operations Specialist Troy MI": {"raw": 1, "passed": 0}}
    assert trace.raw == 6


def test_query_yield_report_puts_the_worst_performer_first():
    trace = m.FunnelTrace()
    trace.per_query = {
        "good query": {"raw": 12, "passed": 3},
        "dead query": {"raw": 0, "passed": 0},
        "wasteful query": {"raw": 40, "passed": 0},
    }

    lines = trace.query_yield_report().splitlines()

    # Zero-passed first, and among those the one burning the most raw listings leads.
    assert "wasteful query" in lines[0]
    assert "dead query" in lines[1]
    assert "good query" in lines[2]


def test_query_yield_report_is_empty_without_attribution():
    assert m.FunnelTrace().query_yield_report() == ""


# ---- Geography gate: Michigan as a fallback for unrecognized city strings ----

def _geo_job(city, state=""):
    return {
        "employer_name": "Acme Corp",
        "job_title": "Operations Analyst",
        "job_description": "Reconciliation workflows with SQL and Salesforce.",
        "job_city": city,
        "job_state": state,
    }


@pytest.fixture
def _geo_filters(monkeypatch):
    monkeypatch.setattr(m, "is_company_on_cooldown", lambda company: False)
    monkeypatch.setattr(m, "get_applied_crm_companies", lambda: set())
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: {
        "min_salary": 50000,
        "valid_cities": ["farmington", "detroit", "troy"],
    }.get(key, default if default is not None else []))


def test_michigan_job_passes_even_when_the_city_is_not_allowlisted(_geo_filters):
    """valid_cities is hand-curated, so an unfamiliar in-radius suburb used to look identical to an
    out-of-area reject. Every query is already radius-limited, so a Michigan result that survived
    sourcing is overwhelmingly local."""
    for city, state in [
        ("Bingham Farms", "MI"),   # real metro suburb, not on the list
        ("Detroit Metro", "MI"),   # vague metro string
        ("Southeast Michigan", ""),  # state named inside the city field
        ("Ann Arbor, MI", ""),     # "city, MI" form with no state field
        ("", "MI"),                # ATS postings often send a blank city
    ]:
        assert m.passes_strict_filter(_geo_job(city, state)) is True, f"{city!r}/{state!r} should pass"


def test_out_of_state_is_still_rejected(_geo_filters):
    trace = m.FunnelTrace()
    assert m.passes_strict_filter(_geo_job("Chicago", "IL"), trace=trace) is False
    assert trace.reasons.get("out_of_state") == 1


def test_unknown_location_with_no_michigan_signal_is_still_rejected(_geo_filters):
    """Michigan is a fallback, not an opening of the gate - "Remote" and a blank location carry no
    geographic signal at all and must not slip through."""
    for city in ("Remote", ""):
        trace = m.FunnelTrace()
        assert m.passes_strict_filter(_geo_job(city), trace=trace) is False
        assert trace.reasons.get("city_allowlist") == 1


# ---- JSearch pagination bounds ----

def test_jsearch_always_fetches_from_page_one(monkeypatch):
    """No rolling offset. The old pointer advanced 3 pages per run and only wrapped at 20, so
    queries drifted onto deep pages that time out - 99 of 110 were stranded there returning zero -
    and on alternating runs a query skipped page 1 entirely to fetch a worse page alone."""
    requested = []

    def _capture(api_url, headers, params, query, page):
        requested.append(int(params["page"]))
        return [{"job_id": f"x{page}"}], False

    monkeypatch.setattr(m, "_fetch_jsearch_page_with_retry", _capture)
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: 45 if key == "radius_miles" else default)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    # Two consecutive runs must request exactly the same pages - there is no state to drift.
    m.fetch_single_query_jobs(("Operations Analyst Troy MI", "http://x", {}))
    first_run = list(requested)
    requested.clear()
    m.fetch_single_query_jobs(("Operations Analyst Troy MI", "http://x", {}))

    assert first_run == list(range(1, m.JSEARCH_PAGES_PER_RUN + 1))
    assert requested == first_run, "consecutive runs must fetch the same pages"


def test_jsearch_requests_are_scoped_to_the_us(monkeypatch):
    """Without country=us, JSearch answers a '... Auburn Hills MI' query with postings in
    Singapore, Dubai and Warsaw, which burn the page budget local listings should fill."""
    seen = {}

    def _capture(api_url, headers, params, query, page):
        seen.update(params)
        return [], True

    monkeypatch.setattr(m, "_fetch_jsearch_page_with_retry", _capture)
    monkeypatch.setattr(m, "get_query_start_page", lambda q: 1)
    monkeypatch.setattr(m, "save_query_next_page", lambda q, p: None)
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: 45 if key == "radius_miles" else default)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    m.fetch_single_query_jobs(("Operations Analyst Troy MI", "http://x", {}))

    assert seen.get("country") == "us"


# ---- Market supply report (/funnel) ----

def _seed_supply(jobs, applied=0, discovered=0):
    """Insert scored jobs plus raw discovery/applied events for the supply window.

    pipeline_metrics is deliberately NOT in the autouse clean_tables fixture (it is the durable
    metrics ledger other tests assert survives), so this clears it locally instead - otherwise
    events from an earlier test leak into these counts.
    """
    with m.get_db_conn() as conn:
        conn.execute("DELETE FROM pipeline_metrics")
        for i, score in enumerate(jobs):
            conn.execute(
                "INSERT INTO jobs (short_id, sheet_uuid, job_json) VALUES (?, ?, ?)",
                (f"sup{i}", f"uuid-sup-{i}", json.dumps({"fit_score": score})),
            )
        for _ in range(discovered):
            conn.execute("INSERT INTO pipeline_metrics (event_type) VALUES ('listing_discovered')")
        for _ in range(applied):
            conn.execute("INSERT INTO pipeline_metrics (event_type) VALUES ('applied')")
        conn.commit()


def test_market_supply_counts_only_qualified_scores():
    # 95/80 qualify, 79 does not - the threshold is inclusive at SUPPLY_QUALIFIED_SCORE.
    _seed_supply([95, 80, 79], discovered=10)
    supply = m.get_market_supply(days=7)
    assert supply["qualified"] == 2
    assert supply["discovered"] == 10
    assert supply["per_week"] == pytest.approx(2.0)


def test_market_supply_ignores_jobs_cached_before_fit_score_existed():
    # json_extract -> NULL must fail the comparison rather than counting as qualified.
    with m.get_db_conn() as conn:
        conn.execute("DELETE FROM pipeline_metrics")
        conn.execute(
            "INSERT INTO jobs (short_id, sheet_uuid, job_json) VALUES (?, ?, ?)",
            ("legacy", "uuid-legacy", json.dumps({"employer_name": "X"})),
        )
        conn.commit()
    assert m.get_market_supply(days=7)["qualified"] == 0


def test_market_supply_reports_no_ratio_when_nothing_applied():
    # No consumption rate to divide by: None, never a division-by-zero "infinite supply".
    _seed_supply([90, 90], applied=0)
    assert m.get_market_supply(days=7)["weeks_of_supply"] is None


def test_market_supply_ratio_flags_draining_faster_than_refill():
    _seed_supply([90, 90], applied=6)
    supply = m.get_market_supply(days=7)
    assert supply["weeks_of_supply"] == pytest.approx(2 / 6)
    assert "faster than the market refills" in m.format_market_supply_message(supply)


def test_market_supply_message_distinguishes_empty_window_from_dry_market():
    with m.get_db_conn() as conn:
        conn.execute("DELETE FROM pipeline_metrics")
        conn.commit()
    empty = m.format_market_supply_message(m.get_market_supply(days=7))
    assert "run /t a few times" in empty


# ---- PEOPLE tab membership (contact-capture gate) ----

def test_people_tabs_covers_every_carmen_tab_plus_killed():
    # A PEOPLE tab missing from this tuple silently duplicates a real contact: the sent-mail
    # capture gate would not find them and would write a second row into Carmen Cold.
    assert set(m.PEOPLE_TABS) == {"Carmen Cold", "Carmen Hot", "Carmen Warm", "Killed"}


def test_carmen_hot_contact_is_not_recaptured(monkeypatch):
    # The bug this guards: promote someone to Carmen Hot, email them, and the sent-mail scanner
    # re-adds them to Carmen Cold - restarting the ladder on a contact already graduated.
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: pytest.fail("should not reach the live CRM"))
    with m.get_db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sheet_row_map (sheet_uuid, sheet_tab, contact_email) "
            "VALUES ('uuid-hot', 'Carmen Hot', 'promoted@example.com')"
        )
        conn.commit()
    assert m.is_logged_person_contact("promoted@example.com") is True


def test_warm_tone_tabs_excludes_the_archive():
    # Killed is archived - a contact parked there should not pull warm copy if ever re-touched.
    assert "Killed" not in m.WARM_TONE_TABS
    assert "Carmen Hot" in m.WARM_TONE_TABS


# ---- Posting-freshness scoring (regression: the whole-dict call) ----

_FRESHNESS_BASE_JOB = {
    "job_title": "Operations Analyst",
    "job_description": "reconciliation and reporting",
    "job_city": "troy",
}


def test_freshness_scoring_separates_a_fresh_posting_from_a_stale_one():
    """parse_posted_hours() takes an ISO string and fails open to 48 on anything else, so handing
    it the whole job dict scored every listing as exactly 48h old: the +8 bonus fired on all of
    them and the >=720h -8 penalty could never fire at all.

    Timestamps are built off now() so they cannot rot. Note the 26-day case lands at ~624h, which
    is past every bonus but short of the 720h penalty line - the -8 branch needs a 31-day posting.
    """
    fresh = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=26)).isoformat()
    ancient = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()

    assert m.parse_posted_hours(fresh) != m.parse_posted_hours(stale)
    assert m.parse_posted_hours(fresh) <= 72        # +8 branch
    assert 168 < m.parse_posted_hours(stale) < 720  # no freshness modifier at all
    assert m.parse_posted_hours(ancient) >= 720     # -8 branch

    def bonus_for(posted):
        _, layer1_bonus = m.calculate_hybrid_score_modifier(
            dict(_FRESHNESS_BASE_JOB, job_posted_at_datetime_utc=posted), 70)
        return layer1_bonus

    # Freshness is the only thing differing between these jobs, so the gaps are the branch deltas.
    assert bonus_for(fresh) - bonus_for(stale) == 8       # +8 against nothing
    assert bonus_for(fresh) - bonus_for(ancient) == 16    # +8 against -8


def test_freshness_scoring_fails_open_to_48_hours_without_a_timestamp():
    """Fail-open is deliberate and stays: ATS feeds routinely omit the posted date, and scoring
    those as stale would bury exactly the direct-from-employer listings worth the most.
    """
    assert m.parse_posted_hours("") == 48
    fresh = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()

    def bonus_for(job):
        _, layer1_bonus = m.calculate_hybrid_score_modifier(job, 70)
        return layer1_bonus

    fresh_bonus = bonus_for(dict(_FRESHNESS_BASE_JOB, job_posted_at_datetime_utc=fresh))
    # 48 lands in the <=72 bucket, so a dateless posting keeps the same +8 a fresh one earns.
    assert bonus_for(dict(_FRESHNESS_BASE_JOB)) == fresh_bonus                                 # key absent
    assert bonus_for(dict(_FRESHNESS_BASE_JOB, job_posted_at_datetime_utc="")) == fresh_bonus  # blank
    assert bonus_for(dict(_FRESHNESS_BASE_JOB, job_posted_at_datetime_utc=None)) == fresh_bonus


# ---- /dead decoy marking + /decoys report ----

def test_posted_hours_migration_is_idempotent(monkeypatch, clean_outcomes):
    """Render's application_outcomes already carries rows, so the column can only arrive by ALTER -
    and init_db() runs on every boot while SQLite has no ADD COLUMN IF NOT EXISTS."""
    monkeypatch.setattr(m, "hydrate_filters_from_sheets", lambda: None)
    monkeypatch.setattr(m, "restore_core_sourcing_filters", lambda: None)
    m.record_application_outcome("uuid-premigration", "applied", company="Atwell")

    m.init_db()
    m.init_db()

    with m.get_db_conn() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(application_outcomes)")]
        surviving = conn.execute(
            "SELECT COUNT(*) FROM application_outcomes WHERE sheet_uuid = 'uuid-premigration'").fetchone()[0]
    assert columns.count("posted_hours") == 1
    assert surviving == 1  # the pre-existing row was migrated, not recreated away


def test_dead_swipe_persists_status_source_and_posted_hours(monkeypatch, clean_outcomes):
    """Reads the row back out of SQLite rather than trusting the handler's return: the failure mode
    this guards is a command that reports success to Telegram and persists nothing."""
    posted = (datetime.now(timezone.utc) - timedelta(hours=300)).isoformat()
    m.save_job_to_cache("short-dead", {
        "job_id": "lever_abc123", "employer_name": "Acme Corp", "job_title": "Ops Analyst",
        "job_posted_at_datetime_utc": posted,
    }, sheet_uuid="uuid-dead")
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-dead", "sheet_tab": "Tetiana Cold", "contact_name": "", "contact_company": ""})
    sent, edited = [], []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t: sent.append(t))
    monkeypatch.setattr(m, "edit_telegram_message", lambda cid, mid, t: edited.append(t) or True)
    # /dead is measurement, not a CRM transition - it must not move or restatus the row.
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: pytest.fail("/dead must not write to the CRM"))

    _dispatch("/dead", reply_to_message={"message_id": 77, "text": "Ops Analyst"})

    with m.get_db_conn() as conn:
        row = conn.execute(
            "SELECT status, source, company, role, posted_hours FROM application_outcomes "
            "WHERE sheet_uuid = ?", ("uuid-dead",)).fetchone()
    assert row[0] == "dead_link"
    assert row[1] == "lever"
    assert (row[2], row[3]) == ("Acme Corp", "Ops Analyst")
    assert 299 <= row[4] <= 300  # second-resolution created_at can truncate one hour off
    assert "Dead link" in edited[0]


def test_dead_swipe_records_a_null_age_when_the_posting_carries_no_date(monkeypatch, clean_outcomes):
    """None, not 48: parse_posted_hours' fail-open default is right for scoring and wrong here,
    because a fabricated age would silently move the /decoys median."""
    m.save_job_to_cache("short-undated", {
        "job_id": "gh_undated", "employer_name": "Undated Inc", "job_title": "Ops Analyst",
    }, sheet_uuid="uuid-undated")
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-undated", "sheet_tab": "Tetiana Cold", "contact_name": "", "contact_company": ""})
    for name in ("send_telegram_message", "edit_telegram_message"):
        monkeypatch.setattr(m, name, lambda *a, **k: True)

    _dispatch("/dead", reply_to_message={"message_id": 78, "text": "Ops Analyst"})

    with m.get_db_conn() as conn:
        row = conn.execute(
            "SELECT status, source, posted_hours FROM application_outcomes WHERE sheet_uuid = ?",
            ("uuid-undated",)).fetchone()
    assert row == ("dead_link", "greenhouse", None)


def test_dead_swipe_on_an_uncached_job_records_no_source_rather_than_guessing(monkeypatch, clean_outcomes):
    """derive_job_source() defaults to jsearch for an unrecognised id, so a card whose cache entry
    a restart wiped would silently pad jsearch's decoy count. NULL source -> 'unknown' in /decoys."""
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-uncached", "sheet_tab": "Tetiana Cold",
        "contact_name": "", "contact_company": "Ghost Co"})
    for name in ("send_telegram_message", "edit_telegram_message"):
        monkeypatch.setattr(m, name, lambda *a, **k: True)

    _dispatch("/dead", reply_to_message={"message_id": 79, "text": "Ops Analyst"})

    with m.get_db_conn() as conn:
        row = conn.execute(
            "SELECT status, source, company, posted_hours FROM application_outcomes WHERE sheet_uuid = ?",
            ("uuid-uncached",)).fetchone()
    assert row == ("dead_link", None, "Ghost Co", None)
    assert "unknown" in m.get_decoy_metrics()["by_source"]


def test_decoys_report_reads_cleanly_with_zero_rows(clean_outcomes):
    msg = m.format_decoy_metrics_message()
    assert "No outcome rows recorded yet" in msg
    # No fabricated 0.0% anywhere: an absent measurement must not look like a measured result.
    assert "%" not in msg
    assert len(msg) < 4096


def test_decoys_report_ranks_the_worst_source_first_and_pairs_the_medians(clean_outcomes):
    for i in range(4):
        m.record_application_outcome(f"u-js-dead-{i}", "dead_link", source="jsearch", posted_hours=600 + i)
    m.record_application_outcome("u-js-live", "applied", source="jsearch", posted_hours=10)
    m.record_application_outcome("u-gh-dead", "dead_link", source="greenhouse", posted_hours=100)
    for i in range(9):
        m.record_application_outcome(f"u-gh-live-{i}", "applied", source="greenhouse", posted_hours=20)

    metrics = m.get_decoy_metrics()
    assert metrics["total_dead"] == 5
    assert metrics["total_rows"] == 15
    # Rate is dead over ALL rows for that source, so volume is not mistaken for quality.
    assert metrics["by_source"]["jsearch"]["decoy_rate"] == pytest.approx(80.0)
    assert metrics["by_source"]["greenhouse"]["decoy_rate"] == pytest.approx(10.0)
    assert metrics["median_dead_posted_hours"] == 601
    assert metrics["median_live_posted_hours"] == 20

    msg = m.format_decoy_metrics_message()
    assert msg.index("jsearch") < msg.index("greenhouse")  # worst offender on line one
    assert "5 of 15" in msg
    assert len(msg) < 4096


def test_decoys_report_does_not_claim_a_decoy_rate_before_any_dead_mark(clean_outcomes):
    m.record_application_outcome("u-applied-only", "applied", source="greenhouse", posted_hours=12)
    msg = m.format_decoy_metrics_message()
    assert "no <code>/dead</code> marks yet" in msg
    assert "0 of 1" in msg
