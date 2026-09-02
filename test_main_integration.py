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
from datetime import date
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
        for table in ("crm_outbox", "sheet_row_map", "company_cooldown", "company_identities",
                      "jobs", "followup_sequencer_log"):
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

    ready = result["followups_ready"]
    assert [r["sheet_uuid"] for r in ready] == ["seq-fu1"]
    assert ready[0]["attempt"] == 1
    assert ready[0]["draft_text"] and "{" not in ready[0]["draft_text"]  # interpolated, not raw template

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
    assert result["counts"] == {"followups_ready": 1, "going_cold": 1, "buried": 1, "top_matched": 2}


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

    assert result["counts"] == {"followups_ready": 1, "going_cold": 1, "buried": 1, "top_matched": 2}
    assert enqueued == []
    with m.get_db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM followup_sequencer_log").fetchone()[0] == 0


# ---- Daily "needs you today" card (render_followup_needs_card) ----

def test_needs_card_renders_every_populated_section(monkeypatch):
    _mock_sequencer_crm(monkeypatch)
    card = m.render_followup_needs_card(m.run_followup_sequencer(today=_SEQ_TODAY, dry_run=True))
    assert "Needs You Today" in card
    assert "Follow-ups ready (1)" in card
    assert "Going cold (1)" in card and "10d untouched" in card
    assert "Buried overnight (1)" in card
    assert "Top 3 untouched matches" in card
    assert "Summary:</b> 1 follow-ups · 1 going cold · 1 buried · 2 top matches" in card


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
    bad = "Hi, I wanted to discuss alignment: happy to grab 15 minutes!\n\nBest regards,\nKevin"

    ok, message = m.update_template_entry(path, "cold_ops", 0, bad)

    assert ok is True
    assert read()["cold_ops"][0] == bad, "the edit must land on disk even with violations"
    assert "Template Updated" in message
    assert "Voice check" in message and "saved anyway" in message
    for expected in ("alignment", "colon", "exclamation", "Best regards", "15 minutes"):
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

    ok, message = m.update_template_entry(path, "cold_ops", 0, "Hi, saw the ops role. Worth a quick chat?")

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
    assert text.count("<a href=") == 5  # Apply, Hiring Mgr, Recruiter, Apollo, Full Card
    assert html.escape(_CARD_JOB["job_apply_link"], quote=True) in text
    for kept_url in (m.build_apollo_url("Atwell"), m.build_recruiter_dork("Atwell"),
                     m.build_hiring_manager_dork("Atwell", _CARD_JOB["job_title"])):
        assert html.escape(kept_url, quote=True) in text
    for stage_only_url in (m.build_linkedin_url("Atwell"), m.build_alumni_dork("Atwell")):
        assert html.escape(stage_only_url, quote=True) not in text
    for link_text in ("Hiring Mgr", "Recruiter", "Apollo"):
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
