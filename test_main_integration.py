"""Integration tests for main.py's SQLite-backed workflows (CRM outbox, cooldown/company-identity,
reply-mapping, batch follow-ups, Gmail draft MIME attachment).

Isolation strategy: JOBS_DB_PATH is set to a temp file BEFORE importing main, so main's own
init_db() builds its schema there instead of touching the real jobs_cache.db, and PYTEST_CURRENT_TEST
(auto-set by pytest) makes main skip starting its background daemons (Gmail poller, CRM outbox
worker, morning digest, backup scheduler) so nothing races against these tests' assertions.
"""
import base64
import os
import sqlite3
import tempfile
import uuid
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
        for table in ("crm_outbox", "sheet_row_map", "company_cooldown", "company_identities", "jobs"):
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
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda payload, max_retries=1: True)
    m.process_crm_outbox_batch(inter_job_sleep=0)
    with m.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crm_outbox").fetchone()[0] == 0


def test_crm_outbox_failure_increments_retry_and_stays_pending(monkeypatch):
    m.enqueue_crm_payload({"action": "update_status", "sheet_uuid": "abc"})
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda payload, max_retries=1: False)
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
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda payload, max_retries=1: False)
    m.process_crm_outbox_batch(inter_job_sleep=0)
    with m.get_db_conn() as conn:
        row = conn.execute("SELECT retry_count, status FROM crm_outbox").fetchone()
    assert row == (10, "FAILED")


def test_crm_outbox_batch_ignores_rows_past_max_retries(monkeypatch):
    with m.get_db_conn() as conn:
        conn.execute(
            "INSERT INTO crm_outbox (payload_json, status, retry_count) VALUES (?, 'FAILED', 10)",
            ('{"action": "update_status"}',)
        )
        conn.commit()
    calls = []
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda payload, max_retries=1: calls.append(payload) or True)
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
