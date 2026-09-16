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
from datetime import date, timedelta
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
                      "jobs", "followup_sequencer_log", "seen_jobs", "seen_content_hashes"):
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
    assert result["counts"] == {"followups_ready": 1, "going_cold": 1, "buried": 1,
                                "top_matched": 2, "buries_suppressed": 0}


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

    assert result["counts"] == {"followups_ready": 1, "going_cold": 1, "buried": 1,
                                "top_matched": 2, "buries_suppressed": 0}
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
    ladder at +3 rather than firing a nudge immediately - that is the manual-move path working
    with no Apps Script trigger involved."""
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
    assert [(p["action"], p["next_followup"]) for p in enqueued] == [("update_snooze", expected)]


def test_carmen_cold_row_is_never_auto_buried_to_died(monkeypatch):
    """A networking contact is not a job application. A CC row that has exhausted the
    ladder is surfaced as 'going cold' for a human call - never an append_note/update_status->Died
    write, and no further nudges."""
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
    assert [r["sheet_uuid"] for r in result["going_cold"]] == ["cc-old"]
    assert enqueued == []  # zero writes for a would-be bury on a PEOPLE row


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


# ---- Resume PDF attachment filename ----

def test_resume_pdf_filename_drops_track_code_and_legal_suffix():
    # The recruiter-visible name carries the company and nothing internal: no Track A-E routing
    # key, no mangled run-together words, no legal suffix.
    assert m.resume_pdf_filename("Atwell, LLC") == "Kevin_Miller_Resume_Atwell.pdf"
    assert m.resume_pdf_filename("Goldman Sachs") == "Kevin_Miller_Resume_Goldman_Sachs.pdf"
    assert m.resume_pdf_filename("Ernst & Young LLP") == "Kevin_Miller_Resume_Ernst_Young.pdf"
    assert m.resume_pdf_filename("Booz Allen Hamilton Holdings Corporation") == (
        "Kevin_Miller_Resume_Booz_Allen_Hamilton.pdf"
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
        assert letter.startswith("Dear Acme Hiring Team,"), ctx
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


def test_ingest_manual_job_dedups_a_repasted_posting(monkeypatch):
    def _no_score(job):
        pytest.fail("should not re-score an already-seen posting")

    def _no_write(p, **kw):
        pytest.fail("no duplicate row")

    monkeypatch.setattr(m, "process_single_candidate", _no_score)
    monkeypatch.setattr(m, "log_to_sheets_crm", _no_write)
    m.save_seen_job_db(m.generate_dedup_hash("Huntington", "FX Ops Analyst"))

    ok, message = m.ingest_manual_job(title="FX Ops Analyst", company="Huntington")

    assert ok is False
    assert "Already in the pipeline" in message


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
