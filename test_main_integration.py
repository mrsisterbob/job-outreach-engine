"""Integration tests for main.py's SQLite-backed workflows (CRM outbox, cooldown/company-identity,
reply-mapping, batch follow-ups, Gmail draft MIME attachment).

Isolation strategy: JOBS_DB_PATH is set to a temp file BEFORE importing main, so main's own
init_db() builds its schema there instead of touching the real jobs_cache.db, and PYTEST_CURRENT_TEST
(auto-set by pytest) makes main skip starting its background daemons (Gmail poller, CRM outbox
worker, morning digest, backup scheduler) so nothing races against these tests' assertions.
"""
import base64
import difflib
import html
import io
import json
import os
import re
import sqlite3
import threading
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from email import message_from_bytes

import pytest

_tmp_db_fd, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_tmp_db_fd)
os.environ["JOBS_DB_PATH"] = _TMP_DB_PATH

import main as m  # noqa: E402  (must import after JOBS_DB_PATH is set)
import resume_engine  # noqa: E402
import track_registry  # noqa: E402
import pipeline_utils  # noqa: E402


@pytest.fixture(autouse=True)
def clean_tables():
    """Truncate the tables under test before every test so cases don't bleed into each other."""
    with m.get_db_conn() as conn:
        # seen_jobs/seen_content_hashes are the dedup ledgers: without truncating them, a test that
        # ingests a posting makes every later test using the same company/title silently take the
        # "already in the pipeline" branch instead of the path it meant to exercise.
        for table in ("crm_outbox", "sheet_row_map", "company_cooldown", "company_identities",
                      "jobs", "followup_sequencer_log", "followup_queue_snapshot", "gmail_drafts", "seen_jobs",
                      "seen_content_hashes", "jd_term_yield", "job_link_status",
                      # died_roles is permanent by design, so a row left behind by one test would
                      # silently suppress a role in every later one.
                      "died_roles",
                      "command_usage"):
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


# ---- Outbox alert timing: back-off is not news, abandonment is ----

def _capture_alerts(monkeypatch):
    alerts = []
    monkeypatch.setattr(m, "send_health_alert", lambda msg: alerts.append(msg))
    return alerts


def _fail_crm(monkeypatch, status=500):
    """Point the REAL log_to_sheets_crm at a webhook that always fails."""
    class _Resp:
        status_code = status
        text = "boom"
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "crm_post", lambda payload, **kw: _Resp())
    monkeypatch.setattr(m.time, "sleep", lambda *_a, **_k: None)


def test_outbox_retry_pass_does_not_alert(monkeypatch):
    """Regression: the outbox IS the retry mechanism, so a failed attempt is a back-off step, not a
    delivery failure. It used to fire 'Failed to log payload after 1 attempts' on every 5s pass -
    one stuck /warm write produced four identical Telegram warnings inside two minutes.
    """
    _fail_crm(monkeypatch)
    alerts = _capture_alerts(monkeypatch)
    m.enqueue_crm_payload({"action": "update_status", "sheet_uuid": "abc"})

    for _ in range(3):  # three worker passes
        m.process_crm_outbox_batch(inter_job_sleep=0)

    assert alerts == [], f"back-off passes must stay silent, got {alerts}"
    with m.get_db_conn() as conn:
        assert conn.execute("SELECT retry_count FROM crm_outbox").fetchone()[0] == 3


def test_outbox_alerts_once_when_it_gives_up(monkeypatch):
    """The real exhaustion event is retry_count hitting 10 - that one must still reach Telegram."""
    _fail_crm(monkeypatch)
    alerts = _capture_alerts(monkeypatch)
    with m.get_db_conn() as conn:
        conn.execute(
            "INSERT INTO crm_outbox (payload_json, status, retry_count) VALUES (?, 'PENDING', 9)",
            (json.dumps({"action": "update_status", "sheet_uuid": "abc-123"}),)
        )
        conn.commit()

    m.process_crm_outbox_batch(inter_job_sleep=0)

    assert len(alerts) == 1, f"expected exactly one abandonment alert, got {alerts}"
    assert "abc-123" in alerts[0], "the alert must identify WHICH write was lost"
    assert "update_status" in alerts[0]
    with m.get_db_conn() as conn:
        assert conn.execute("SELECT status FROM crm_outbox").fetchone()[0] == "FAILED"

    # ...and having gone FAILED, it is no longer selected, so it cannot alert again.
    m.process_crm_outbox_batch(inter_job_sleep=0)
    assert len(alerts) == 1


def test_direct_caller_still_alerts_on_exhaustion(monkeypatch):
    """Callers that are NOT backed by the outbox keep their alert - for them the attempts really
    were the last word."""
    _fail_crm(monkeypatch)
    alerts = _capture_alerts(monkeypatch)

    assert m.log_to_sheets_crm({"action": "update_status", "sheet_uuid": "zz"}, max_retries=2) is False

    assert len(alerts) == 1
    assert "zz" in alerts[0] and "HTTP 500" in alerts[0]


def test_alert_text_names_the_failure_reason():
    """Four identical alerts were indistinguishable; the Apps Script message only hit the logs."""
    text = m.crm_failure_alert_text(
        {"action": "batch_add_rows", "tab": "TC", "rows": [1, 2, 3]}, 3, "Lock timeout - server busy"
    )
    assert "batch_add_rows" in text and "tab=TC" in text and "rows=3" in text
    assert "Lock timeout" in text


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
    # The Died set is a SEPARATE cache with no staleness bound, so it must be cleared explicitly -
    # a set left over from a previous test would keep suppressing here and nowhere would say why.
    m._DIED_SUPPRESSION_CACHE["fetched_at"] = 0
    m._DIED_SUPPRESSION_CACHE["data"] = set()
    return asked


def test_tracked_keys_include_clavicular_tab(monkeypatch):
    """Regression: rows land in Clavicular (target_code CL) but the tracked set read only TC+TW, so
    a warm-referral role was tracked in the sheet yet invisible to the ingest gate. The card then
    shipped while Code.gs's dedup guard suppressed the write - a card pointing at a nonexistent row.
    """
    role = {"company": "Doeren Mayhew", "title": "Client Onboarding and Operations Specialist"}
    asked = _stub_tracked_tabs(monkeypatch, {"TC": [], "TW": [], "CL": [role]})
    m.get_tracked_job_keys()
    # DD (Died) is read too, but for the opposite reason: TC/TW/CL are tabs a dispatch can WRITE
    # to, Died is the tab a role can never be sourced out of again.
    assert asked == ["TC", "TW", "CL", "DD"], "every tab dispatch can write to must be read back"
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
    m._DIED_SUPPRESSION_CACHE["fetched_at"] = 0
    m._DIED_SUPPRESSION_CACHE["data"] = set()
    assert m.get_tracked_job_keys() == set()
    assert not m.is_role_tracked("Doeren Mayhew", "Client Onboarding and Operations Specialist")


# ---- Died is terminal: a buried role is forbidden from /t, permanently ----

def test_a_role_in_died_is_never_sourced_again(monkeypatch):
    """The DACUT case. "Data Analyst (SQL / Business Intelligence)" was auto-retired to Died when
    its link went dead, then rediscovered the next day and written straight back into Tetiana Cold
    - because the discovery gate read only the live tabs. Died is terminal: whatever put a role
    there (an /x, a rejection, a dead posting), it must never be sourced again."""
    _stub_tracked_tabs(monkeypatch, {
        "TC": [], "TW": [], "CL": [],
        "DD": [{"company": "DACUT", "title": "Data Analyst (SQL / Business Intelligence)"}],
    })
    m.get_tracked_job_keys()
    assert m.is_role_tracked("DACUT", "Data Analyst (SQL / Business Intelligence)")
    # The punctuation variant too: Code.gs keys on normalizeDedupKey, and a role that slips the
    # md5 hash but collides there would be refused at the write with no card to show for it.
    assert m.is_role_tracked("DACUT", "Data Analyst SQL Business Intelligence")
    # A different role at the same company is still fair game - burying one job does not
    # blacklist the employer.
    assert not m.is_role_tracked("DACUT", "Treasury Operations Manager")


def test_died_suppression_survives_a_sheets_outage_that_clears_the_live_set(monkeypatch):
    """The two caches must fail in OPPOSITE directions. The live-tab set errs open past its
    staleness bound so a role whose row Kevin deleted becomes ingestable again. Died has no such
    escape: erring open there re-sources exactly what he buried."""
    _stub_tracked_tabs(monkeypatch, {
        "TC": [{"company": "Rocket", "title": "FX Analyst"}], "TW": [], "CL": [],
        "DD": [{"company": "DACUT", "title": "Data Analyst (SQL / Business Intelligence)"}],
    })
    m.get_tracked_job_keys()
    assert m.is_role_tracked("Rocket", "FX Analyst")

    # Sheets stops answering, and both caches go stale past the live set's bound.
    monkeypatch.setattr(m, "crm_post", lambda p, timeout=10: None)
    monkeypatch.setattr(m, "send_health_alert", lambda msg: None)
    stale = time.time() - (m._TRACKED_ROLE_CACHE_MAX_STALE_SECONDS + 60)
    m._TRACKED_ROLE_CACHE["fetched_at"] = stale
    m._DIED_SUPPRESSION_CACHE["fetched_at"] = stale

    keys = m.get_tracked_job_keys()
    assert not m.is_role_tracked("Rocket", "FX Analyst"), "the live set errs open, as it always has"
    assert keys, "but the Died keys are still enforced"
    assert m.is_role_tracked("DACUT", "Data Analyst (SQL / Business Intelligence)"), \
        "a buried role stays buried even when Sheets is unreachable"


def _seed_dead_link(sheet_uuid, company, role, status, retired=0):
    """One row in job_link_status as the nightly sweep records it."""
    with m.get_db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO job_link_status "
            "(sheet_uuid, company, role, job_link, status, verdict, reason, retired, notified, "
            " first_dead_at) "
            "VALUES (?, ?, ?, ?, ?, 'dead', 'page says no longer available', ?, 0, ?)",
            (sheet_uuid, company, role, "https://example.com/job", status, retired,
             date.today().isoformat()))
        conn.commit()


def test_linksx_archives_the_applied_rows_the_sweep_refuses_to_touch(monkeypatch):
    """The three rows /links reports but will not move on its own.

    The nightly sweep only auto-retires a "Matched" row - a posting coming down on a job Kevin
    APPLIED to is not a rejection, so those wait for his call. /linksx is that call, made once
    for all of them instead of hunting down each card to swipe /x.
    """
    _seed_dead_link("u-yochana", "Yochana", "Jr. Analyst - Entry Level", "Applied")
    _seed_dead_link("u-autowh", "Auto Warehousing", "Hybrid Revenue Systems Analyst", "Applied")
    _seed_dead_link("u-hunt", "Huntington", "Foreign Exchange Ops Analyst 2", "Applied")
    # Already retired by the sweep - /linksx must leave it alone, it is finished.
    _seed_dead_link("u-optech", "OpTech", "IT Systems Analyst", "Matched", retired=1)

    sent, payloads = [], []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, txt, **k: sent.append(txt))
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: payloads.append(p))

    _dispatch("/linksx")

    moved = [p for p in payloads if p.get("action") == "update_status"]
    assert {p["sheet_uuid"] for p in moved} == {"u-yochana", "u-autowh", "u-hunt"}, \
        "every APPLIED dead-link row moves, and the already-retired one is not touched again"
    assert all(p["new_tab"] == "Died" for p in moved)
    # Each move is preceded by a note recording why, the same two-step the sweep uses.
    assert len([p for p in payloads if p.get("action") == "append_note"]) == 3

    # THE WRITE PATH: what the NEXT /t run reads back. A role archived here must be permanently
    # unsourceable, not merely moved on the sheet.
    monkeypatch.setattr(m, "send_health_alert", lambda msg: None)
    monkeypatch.setattr(m, "crm_post", lambda p, timeout=10: None)
    m._TRACKED_ROLE_CACHE["fetched_at"] = 0
    m._TRACKED_ROLE_CACHE["data"] = set()
    m._DIED_SUPPRESSION_CACHE["fetched_at"] = 0
    m._DIED_SUPPRESSION_CACHE["data"] = set()
    m._DIED_GATE_ALERTED["at"] = 0
    assert m.is_role_tracked("Yochana", "Jr. Analyst - Entry Level")
    assert m.is_role_tracked("Huntington", "Foreign Exchange Ops Analyst 2")


def test_linksx_says_so_plainly_when_there_is_nothing_waiting(monkeypatch):
    """Only auto-retired rows exist, so there is no decision left for Kevin to make."""
    _seed_dead_link("u-optech", "OpTech", "IT Systems Analyst", "Matched", retired=1)
    sent, payloads = [], []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, txt, **k: sent.append(txt))
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: payloads.append(p))

    _dispatch("/linksx")

    assert payloads == [], "nothing is written when nothing is waiting"
    assert "Nothing to archive" in sent[0]


def test_the_links_card_advertises_linksx_when_rows_are_waiting(monkeypatch):
    """A command Kevin cannot discover is a command he will not use - the card has to name it."""
    _seed_dead_link("u-yochana", "Yochana", "Jr. Analyst - Entry Level", "Applied")
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, txt, **k: sent.append(txt))

    _dispatch("/links")

    assert "/linksx" in sent[0]
    assert "archive all 1 to Died" in sent[0]


def test_an_undeployed_apps_script_cannot_silently_disable_the_died_gate(monkeypatch):
    """THE BUG that let ALPINE POWER SYSTEMS / ADMIN respawn out of Died.

    An Apps Script that predates the DD target code answers the Died read with HTTP 200 and
    {"status":"error","message":"Unknown target_code: DD"}. The old code checked only for
    status=="success", fell through, and returned an EMPTY set - so the gate reported no buried
    roles, suppressed nothing, and logged nothing unusual. It looked deployed and was not.

    Two things must now hold: the local ledger keeps enforcing, and Kevin is told.
    """
    m._reset_died_ledger()
    m.record_died_role("ALPINE POWER SYSTEMS", "ADMIN")

    alerts = []
    monkeypatch.setattr(m, "send_health_alert", lambda msg: alerts.append(msg))
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")

    class _OldDeployment:
        status_code = 200
        def json(self):
            return {"status": "error", "message": "Unknown target_code: DD"}

    monkeypatch.setattr(m, "crm_post", lambda p, timeout=10: _OldDeployment())
    m._DIED_SUPPRESSION_CACHE["fetched_at"] = 0
    m._DIED_SUPPRESSION_CACHE["data"] = set()
    m._DIED_GATE_ALERTED["at"] = 0

    keys = m.died_suppression_keys()
    assert keys, "an unreadable Died tab must not mean 'nothing is buried'"
    assert m.generate_dedup_hash("ALPINE POWER SYSTEMS", "ADMIN") in keys
    assert alerts and "Died suppression is OFF" in alerts[0], \
        "a gate that cannot enforce must say so - silence is what caused the respawn"
    m._reset_died_ledger()


def test_the_local_ledger_blocks_a_buried_role_with_no_network_at_all(monkeypatch):
    """The ledger is written by the same process that does the burying, so it holds the line
    when Apps Script is unreachable, un-deployed, or answering nonsense."""
    m._reset_died_ledger()
    m.record_died_role("ALPINE POWER SYSTEMS", "ADMIN")

    monkeypatch.setattr(m, "send_health_alert", lambda msg: None)
    monkeypatch.setattr(m, "crm_post", lambda p, timeout=10: None)
    m._TRACKED_ROLE_CACHE["fetched_at"] = 0
    m._TRACKED_ROLE_CACHE["data"] = set()
    m._DIED_SUPPRESSION_CACHE["fetched_at"] = 0
    m._DIED_SUPPRESSION_CACHE["data"] = set()
    m._DIED_GATE_ALERTED["at"] = 0

    assert m.is_role_tracked("ALPINE POWER SYSTEMS", "ADMIN"), \
        "a locally-buried role is forbidden from /t even with Sheets down"
    # Case and the punctuation variant collapse to the same keys.
    assert m.is_role_tracked("Alpine Power Systems", "ADMIN")
    # A different role at the same company is still allowed through.
    assert not m.is_role_tracked("ALPINE POWER SYSTEMS", "Operations Analyst")
    m._reset_died_ledger()


def test_burying_a_role_records_it_locally_so_the_next_pull_refuses_it(monkeypatch):
    """The write path, per CLAUDE.md: drive the real retire and assert what the NEXT discovery
    pass reads back, not what the function returned."""
    m._reset_died_ledger()
    monkeypatch.setattr(m, "send_health_alert", lambda msg: None)
    monkeypatch.setattr(m, "crm_post", lambda p, timeout=10: None)
    m._DIED_SUPPRESSION_CACHE["fetched_at"] = 0
    m._DIED_SUPPRESSION_CACHE["data"] = set()
    m._DIED_GATE_ALERTED["at"] = 0

    assert not m.is_role_tracked("Stellantis Financial", "ICT Product Analyst, Purchasing Systems")
    m.record_died_role("Stellantis Financial", "ICT Product Analyst, Purchasing Systems")
    m._DIED_SUPPRESSION_CACHE["fetched_at"] = 0
    assert m.is_role_tracked("Stellantis Financial", "ICT Product Analyst, Purchasing Systems")
    m._reset_died_ledger()


def test_locate_tracked_role_names_died_so_the_block_is_explainable(monkeypatch):
    """Without this, a role blocked by Died reported "not found" - which reads as a stale-cache
    ghost and invites Kevin to retry an ingest that is permanently forbidden."""
    _stub_tracked_tabs(monkeypatch, {
        "TC": [], "TW": [], "CL": [],
        "DD": [{"company": "DACUT", "title": "Data Analyst (SQL / Business Intelligence)"}],
    })
    found = m.locate_tracked_role("DACUT", "Data Analyst (SQL / Business Intelligence)")
    assert found and found["tab"] == "Died"


def test_a_died_suppressed_row_never_ships_a_card(monkeypatch):
    """Code.gs refuses the write and reports died_suppressed. Unlike duplicate_suppressed there is
    no live row to re-point the card at, so the card must be withheld outright - shipping one would
    give every /apply and /warm on it a uuid that exists in no tab."""
    m._stash_batch_dispositions({}, [
        {"sent_uuid": "uuid-died", "status": "died_suppressed", "existing_uuid": ""},
    ])
    assert m.get_batch_disposition("uuid-died")["status"] == "died_suppressed"


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


def _crm_batch_result(monkeypatch, sent, written, message=""):
    """Run log_to_sheets_crm() for a batch_add_rows whose Apps Script reply reports `written`."""
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "send_health_alert", lambda t: None)
    monkeypatch.setattr(m.time, "sleep", lambda *a: None)

    class _Resp:
        status_code = 200
        def json(self):
            return {"status": "success", "count": written, "message": message}

    monkeypatch.setattr(m, "crm_post", lambda p, timeout=10: _Resp())
    return m.log_to_sheets_crm({"action": "batch_add_rows", "rows": [{}] * sent})


def test_duplicate_suppressed_batch_is_not_a_failed_write(monkeypatch):
    """A short count from the Apps Script dedup guard must NOT read as a failed batch.

    On 2026-09-21 one duplicate out of five rows made log_to_sheets_crm() return False, which made
    dispatch_tier1_matches() withhold all five Tier-1 cards - including a 100-score role - and
    alert that none of the rows were in the sheet when four of them were. The suppressed row is a
    live Company+Role that is already tracked, so the batch is a success.
    """
    assert _crm_batch_result(
        monkeypatch, sent=5, written=4,
        message="Batch inserted 4 rows (1 duplicate(s) suppressed)") is True


def test_batch_that_writes_nothing_still_fails(monkeypatch):
    """Zero written is the case the withholding gate exists for: no row, so no card."""
    assert _crm_batch_result(monkeypatch, sent=5, written=0) is False


def test_fully_written_batch_succeeds(monkeypatch):
    assert _crm_batch_result(monkeypatch, sent=5, written=5) is True


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

# Applied at the bump boundary -> follow-up #1 ; Applied past the bury boundary -> bury ;
# Interviewing 10d ago -> stale ; two Matched rows for the "top 3" section ; one future-dated
# Applied row that must be left alone.
#
# The two Applied dates are DERIVED from the cadence knobs rather than hardcoded. They were
# literals ("2026-05-28", "2026-05-16") chosen against the old bump-at-2 / bury-at-14 numbers,
# so retuning the cadence silently flipped this row from a bump to silence and that one from a
# bury to a bump - which is how twelve sequencer tests failed at once on a two-line change.
_SEQ_BUMP_DATE = (_SEQ_TODAY - timedelta(days=m.FOLLOWUP_1_DAYS)).isoformat()
_SEQ_BURY_DATE = (_SEQ_TODAY - timedelta(days=m.FOLLOWUP_BURY_DAYS + 2)).isoformat()
_SEQ_RECORDS = {
    "TC": [
        {"sheet_uuid": "seq-fu1", "company": "Acme", "title": "Ops Analyst", "name": "",
         "status": "Applied", "date_added": _SEQ_BUMP_DATE, "next_followup": "1970-01-01", "raw_priority": "70"},
        {"sheet_uuid": "seq-bury", "company": "Beta", "title": "Ops Lead", "name": "",
         "status": "Applied", "date_added": _SEQ_BURY_DATE, "next_followup": "1970-01-01", "raw_priority": "60"},
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
    # The bump snoozes straight to the bury boundary: anchor + FOLLOWUP_BURY_DAYS. Computed, not
    # a literal date, so the assertion states the RULE rather than one cadence's arithmetic.
    expected = (date.fromisoformat(_SEQ_BUMP_DATE) + timedelta(days=m.FOLLOWUP_BURY_DAYS)).isoformat()
    assert snoozes[0]["next_followup"] == expected
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
                                "top_matched": 2, "live_conversations": 0, "buries_suppressed": 0,
                                "kills_suppressed": 0}


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
                                "top_matched": 2, "live_conversations": 0, "buries_suppressed": 0,
                                "kills_suppressed": 0}
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
            "date_added": (today - timedelta(days=m.CARMEN_LADDER_DAYS_COLD[0])).strftime("%Y-%m-%d"),
            "next_followup": today.strftime("%Y-%m-%d"), "raw_priority": "High"}


def _due_application(i, email="kjmiller406@gmail.com"):
    """A JOBS row sitting exactly on the bump boundary - follow-up #1 due. Contact Email is often
    Kevin's own.

    Date derived from FOLLOWUP_1_DAYS rather than the old "2026-05-28" literal, which was four
    days before _SEQ_TODAY and only landed on a bump while the knob was 2.
    """
    return {"sheet_uuid": f"app-{i}", "company": f"Acme{i}", "title": "Ops Analyst", "name": "",
            "email": email, "status": "Applied", "date_added": _SEQ_BUMP_DATE,
            "next_followup": "1970-01-01", "raw_priority": "70"}


def test_the_gate_sees_the_real_sequencer_row_not_just_a_hand_built_dict(monkeypatch):
    """REGRESSION. autosend_block_reason() reads `note` and `title`, and the sequencer's entry
    dict originally carried NEITHER - it had `role` for display and dropped the notes cell. Every
    unit test passed because they hand-built dicts that happened to have those keys, while in
    production the reply check read "" and could never fire: a contact mid-conversation would have
    been auto-bumped. This drives the real sequencer, so the entry is the one production builds."""
    # The reply re-anchors the ladder to the reply date, so the row is placed on its ENGAGED
    # rung-1 due date - otherwise nothing is due and the gate is never reached.
    replied_on = _SEQ_TODAY - timedelta(days=m.CARMEN_LADDER_DAYS_ENGAGED[0])
    replied = _due_person(1)
    replied["note"] = f"[{replied_on.isoformat()}] {m.INBOUND_REPLY_NOTE_MARKER} - asked for a call"
    replied["date_added"] = replied_on.strftime("%Y-%m-%d")
    replied["next_followup"] = _SEQ_TODAY.strftime("%Y-%m-%d")
    _mock_followup_rows(monkeypatch, cc_rows=[replied])

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    ready = result["followups_ready"]
    assert len(ready) == 1
    assert "note" in ready[0] and "title" in ready[0], "the gate's inputs must survive into the entry"
    assert ready[0]["autosend"] is False
    assert ready[0]["autosend_block"] == "already_replied"


def test_a_real_sequencer_row_with_a_messy_title_is_held_back(monkeypatch):
    """The other gate input the entry used to drop. A JOBS-schema row carrying board noise must
    not auto-send, however clean the sanitized version reads."""
    row = _due_person(2)
    row["title"] = "Ops Analyst Intermediate /work from home reputed company reputed company/"
    _mock_followup_rows(monkeypatch, cc_rows=[row])

    result = m.run_followup_sequencer(today=_SEQ_TODAY)

    assert result["followups_ready"][0]["autosend_block"] == "messy_title"


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
    log) so the bury is reached."""
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
    assert app["date_added"] == _SEQ_BUMP_DATE
    assert app["next_followup"] == "1970-01-01"
    # anchor + FOLLOWUP_BURY_DAYS: the bump snoozes straight to the bury boundary.
    assert app["new_next_followup"] == (
        date.fromisoformat(_SEQ_BUMP_DATE) + timedelta(days=m.FOLLOWUP_BURY_DAYS)
    ).isoformat()
    assert app["days_silent"] == m.FOLLOWUP_1_DAYS
    assert app["buries_on"] == (
        date.fromisoformat(_SEQ_BUMP_DATE) + timedelta(days=m.FOLLOWUP_BURY_DAYS)
    ).isoformat()
    assert "draft_text" not in app and "draft_id" not in app
    snoozes = [p for p in enqueued if p["action"] == "update_snooze"]
    bury_boundary = (
        date.fromisoformat(_SEQ_BUMP_DATE) + timedelta(days=m.FOLLOWUP_BURY_DAYS)
    ).isoformat()
    assert [(p["sheet_uuid"], p["next_followup"]) for p in snoozes] == [("app-0", bury_boundary)]
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
    # thread_reply=True is asserted here, not merely tolerated: a follow-up that silently stops
    # threading is invisible in Gmail's own UI (the draft still looks right) and only shows up as a
    # bare "Re:" with no quoted history in the RECIPIENT's inbox.
    assert calls == [{"to_email": "pat@acme.com", "company_name": "Nliven", "job_title": "",
                      "custom_body": "Exact card text.", "custom_subject": "Re: Nliven",
                      "thread_reply": True}]
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
    # No prior conversation. Stubbed rather than left to the real requests.get: unstubbed, the
    # thread lookup makes a LIVE call to Gmail, which 401s on the fake token and lands on this same
    # branch by accident - a passing test that depended on the network and on nothing else.
    monkeypatch.setattr(m, "find_reply_thread", lambda email, token: None)
    monkeypatch.setattr(m, "send_telegram_message", lambda *a, **k: pings.append(a) or 1)
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "123")
    _save_today([_ready_entry("cc-0")])

    first = _click("cc-0")
    second = _click("cc-0")

    assert first[:2] == (302, "https://mail.google.com/mail/u/0/#drafts/draft-42")
    assert second[:2] == first[:2]
    assert len(posts) == 1
    assert pings == []


def _thread_env(monkeypatch):
    """Env + OAuth for the real create_gmail_draft, and the POST captured for inspection."""
    for var, val in (("GMAIL_CLIENT_ID", "cid"), ("GMAIL_CLIENT_SECRET", "cs"),
                     ("GMAIL_REFRESH_TOKEN", "rt"), ("GMAIL_USER", "me@example.com")):
        monkeypatch.setenv(var, val)
    monkeypatch.setattr(m, "get_gmail_access_token", lambda: "token")
    monkeypatch.setattr(m, "send_telegram_message", lambda *a, **k: 1)
    posts = []

    class Created:
        status_code = 200
        def json(self):
            return {"id": "draft-99"}

    def fake_post(url, **kw):
        posts.append(kw.get("json") or {})
        return Created()

    monkeypatch.setattr(m.requests, "post", fake_post)
    return posts


def _sent_message(post):
    """The decoded RFC 2822 message out of a captured drafts.create body."""
    raw = post["message"]["raw"]
    return base64.urlsafe_b64decode(raw.encode()).decode()


def test_followup_draft_threads_into_the_existing_conversation(monkeypatch):
    """The whole point of the feature: Gmail must receive threadId + In-Reply-To + the thread's
    own subject. Asserted on the REQUEST BODY, because every one of these is invisible in the
    draft Kevin sees - a broken thread only shows up in the recipient's inbox."""
    posts = _thread_env(monkeypatch)
    monkeypatch.setattr(m, "find_reply_thread", lambda email, token: {
        "thread_id": "t-500", "subject": "Ops Analyst @ Nliven", "message_id": "<abc@mail>"})
    _save_today([_ready_entry("cc-0", text="Following up.")])

    status, location, _ = _click("cc-0")

    assert (status, location) == (302, "https://mail.google.com/mail/u/0/#drafts/draft-99")
    assert posts[0]["message"]["threadId"] == "t-500"
    sent = _sent_message(posts[0])
    assert "In-Reply-To: <abc@mail>" in sent
    assert "References: <abc@mail>" in sent
    # The thread's real subject, NOT the computed "Re: Nliven": Gmail drops a draft whose subject
    # does not match the thread, so a synthesized one would defeat the threading it asked for.
    assert "Subject: Ops Analyst @ Nliven" in sent
    assert "Re: Nliven" not in sent


def test_draft_without_a_prior_thread_is_a_plain_new_message(monkeypatch):
    """The degrade path. No thread found means no threadId and no reply headers - the old
    behaviour, not an error."""
    posts = _thread_env(monkeypatch)
    monkeypatch.setattr(m, "find_reply_thread", lambda email, token: None)
    _save_today([_ready_entry("cc-0")])

    assert _click("cc-0")[0] == 302
    assert "threadId" not in posts[0]["message"]
    sent = _sent_message(posts[0])
    assert "In-Reply-To" not in sent
    assert "Subject: Re: Nliven" in sent


def test_first_touch_never_threads(monkeypatch):
    """/e and every other first-contact path must not reply into an old conversation, even when
    one exists. thread_reply defaults False, so the lookup is never even attempted."""
    posts = _thread_env(monkeypatch)
    monkeypatch.setattr(m, "find_reply_thread",
                        lambda email, token: pytest.fail("first touch must not look up a thread"))

    ok, _, draft_id = m.create_gmail_draft(
        to_email="pat@acme.com", company_name="Nliven", job_title="Analyst",
        custom_body="First hello.")

    assert (ok, draft_id) == (True, "draft-99")
    assert "threadId" not in posts[0]["message"]


def test_repeat_click_on_a_threaded_draft_does_not_create_a_second(monkeypatch):
    """The dedup key regression. The stored subject is the THREAD's, which this route cannot
    compute, so a subject-based pre-check would miss it and draft again on every click."""
    posts = _thread_env(monkeypatch)
    monkeypatch.setattr(m, "find_reply_thread", lambda email, token: {
        "thread_id": "t-500", "subject": "Ops Analyst @ Nliven", "message_id": "<abc@mail>"})
    _save_today([_ready_entry("cc-0")])

    first = _click("cc-0")
    second = _click("cc-0")

    assert first[:2] == second[:2] == (302, "https://mail.google.com/mail/u/0/#drafts/draft-99")
    assert len(posts) == 1


def _thread_api(monkeypatch, listed, detail):
    """Stub the two Gmail thread calls find_reply_thread makes. Returns the captured list query."""
    seen = {}

    class _Res:
        def __init__(self, body, status=200):
            self.status_code = status
            self._body = body
        def json(self):
            return self._body

    def fake_get(url, **kw):
        if url.endswith("/threads"):
            seen["q"] = (kw.get("params") or {}).get("q", "")
            return _Res(listed)
        return _Res(detail)

    monkeypatch.setattr(m.requests, "get", fake_get)
    return seen


def _hdrs(**kw):
    return {"payload": {"headers": [{"name": k.replace("_", "-"), "value": v}
                                    for k, v in kw.items()]}}


def test_find_reply_thread_reads_first_subject_and_last_message_id(monkeypatch):
    """Subject comes off the FIRST message (the thread's canonical subject) and Message-ID off the
    LAST (what we are actually replying to). Taking both off the same message is the easy bug."""
    seen = _thread_api(
        monkeypatch,
        {"threads": [{"id": "t-1"}]},
        {"messages": [_hdrs(Subject="Ops Analyst @ Nliven", Message_ID="<first@mail>"),
                      _hdrs(Subject="Re: Ops Analyst @ Nliven", Message_ID="<last@mail>")]},
    )

    assert m.find_reply_thread("pat@acme.com", "token") == {
        "thread_id": "t-1", "subject": "Ops Analyst @ Nliven", "message_id": "<last@mail>"}
    # from:me scopes this to conversations Kevin STARTED - replying into a thread he was merely
    # cc'd on would be worse than sending a new mail.
    assert seen["q"] == "from:me to:pat@acme.com"


@pytest.mark.parametrize("listed,detail", [
    ({"threads": []}, {}),                                          # no prior conversation
    ({"threads": [{"id": "t-1"}]}, {"messages": []}),               # thread with no messages
    ({"threads": [{"id": "t-1"}]},                                  # missing Message-ID
     {"messages": [_hdrs(Subject="Ops Analyst @ Nliven")]}),
])
def test_find_reply_thread_returns_none_when_it_cannot_vouch_for_a_thread(monkeypatch, listed, detail):
    """Every gap falls back to None (a plain new message) rather than a half-built reply."""
    _thread_api(monkeypatch, listed, detail)
    assert m.find_reply_thread("pat@acme.com", "token") is None


def test_find_reply_thread_survives_a_gmail_error(monkeypatch):
    def boom(url, **kw):
        raise m.requests.exceptions.Timeout("gmail timed out")
    monkeypatch.setattr(m.requests, "get", boom)
    assert m.find_reply_thread("pat@acme.com", "token") is None


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
    anchor = (_SEQ_TODAY - timedelta(days=m.CARMEN_LADDER_DAYS_COLD[0])).strftime("%Y-%m-%d")
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
    assert ready[0]["ladder_day"] == m.CARMEN_LADDER_DAYS_COLD[0]
    draft = ready[0]["draft_text"]
    # FIRST name only: "Hi Dana Reyes," is the tell that a machine addressed you.
    assert draft.startswith("Hi Dana,")
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
    expected = (_SEQ_TODAY + timedelta(days=m.CARMEN_LADDER_DAYS_COLD[0])).strftime("%Y-%m-%d")
    assert [p["action"] for p in enqueued] == ["append_note", "update_snooze", "set_context"]
    assert enqueued[0]["note"].startswith(f"[{_SEQ_TODAY.isoformat()}] {m.LADDER_RESTART_NOTE_MARKER}")
    assert enqueued[1]["next_followup"] == expected
    # The Column E marker is prepended to the existing "Medium", which is Kevin's own text.
    assert enqueued[2]["context"] == "NEW · unsent | Medium"
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
    assert [p["action"] for p in enqueued] == ["append_note", "update_snooze", "set_context"]


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
        elif payload["action"] == "set_context":
            # Column E round-trips as raw_priority, so a later pass reads back what the marker
            # wrote. Without this the fake sheet would re-read the original cell forever and the
            # "already correct, skip the write" guard would never be exercised.
            row["raw_priority"] = payload["context"]
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
    """Regression for the unreachable path: the final nudge used to write no date, so the row
    re-read as its last rung and nudged forever. It must get exactly the ladder's nudges, a grace
    week, then Killed.

    This contact never replies, so since the cold/engaged split it walks the COLD (4, 11) ladder:
    two nudges, killed at day 18."""
    start = _SEQ_TODAY
    cold = m.CARMEN_LADDER_DAYS_COLD
    sheet = _FakeCarmenSheet(monkeypatch, [_person("ghost", start.isoformat())])
    results = _run_days(sheet, start, 40)

    nudge_days = {n: e["attempt"] for n, r in results.items() for e in r["followups_ready"]}
    assert nudge_days == {d: i + 1 for i, d in enumerate(cold)}
    kill_days = [n for n, r in results.items() if r["killed"]]
    assert kill_days == [cold[-1] + pipeline_utils.CARMEN_KILL_GRACE_DAYS]
    assert sheet.moves == [("ghost", "Killed")]
    assert sheet.row("ghost") is None
    kill_note = [p for p in sheet.payloads if p["action"] == "append_note" and "reason" in p["note"]]
    assert [p["note"] for p in kill_note] == [f"[reason: no reply after {len(cold)} nudges]"]
    # The final nudge's card line says when the kill check happens.
    card = m.render_followup_needs_card(results[cold[-1]])
    terminal = (start + timedelta(days=cold[-1] + pipeline_utils.CARMEN_KILL_GRACE_DAYS)).isoformat()
    assert f"→ killed {terminal} if silent" in card


def test_a_contact_who_replied_walks_the_longer_engaged_ladder(monkeypatch):
    """The other side of the split: a row carrying a reply note gets all three nudges and the
    day-28 triage, because it is a live conversation rather than a push against silence."""
    start = _SEQ_TODAY
    engaged = m.CARMEN_LADDER_DAYS_ENGAGED
    note = f"[{start.isoformat()}] {m.INBOUND_REPLY_NOTE_MARKER} - said to circle back next month"
    sheet = _FakeCarmenSheet(monkeypatch, [_person("talker", start.isoformat(), note=note)])
    results = _run_days(sheet, start, 40)

    nudge_days = {n: e["attempt"] for n, r in results.items() for e in r["followups_ready"]}
    assert nudge_days == {d: i + 1 for i, d in enumerate(engaged)}
    # A contact who has talked to Kevin is never auto-killed: it waits for /promote or /demote.
    assert sheet.moves == []
    assert [r["sheet_uuid"] for r in results[engaged[-1] + pipeline_utils.CARMEN_KILL_GRACE_DAYS]["ready_to_promote"]] == ["talker"]
    # This is the row "spent" exists for: nothing is moved, so Column E is the only readout that
    # it is done laddering and waiting on Kevin.
    markers = [p["context"] for p in sheet.payloads if p["action"] == "set_context"]
    assert markers[-1] == "WARM · spent | Medium"
    assert "WARM · 1 of 2 | Medium" in markers


def test_ladder_marker_tracks_the_row_through_column_e(monkeypatch):
    """The sheet-side readout: Column E carries the track and how many contacts have been spent,
    so sorting on it groups the board. Counts are TOTAL contacts (day 0 + the nudges)."""
    start = _SEQ_TODAY
    cold = m.CARMEN_LADDER_DAYS_COLD
    sheet = _FakeCarmenSheet(monkeypatch, [_person("ghost", start.isoformat())])
    results = _run_days(sheet, start, 40)

    markers = [p["context"] for p in sheet.payloads if p["action"] == "set_context"]
    # "Medium" is Kevin's own Column E text and survives every rewrite. No "spent" marker here:
    # a silent contact is moved to Killed on the same pass, so the tab move is the readout and a
    # Column E write would be redundant. "spent" is for a row that REPLIED and is waiting on
    # /promote - see test_a_contact_who_replied_walks_the_longer_engaged_ladder.
    assert markers == [
        "NEW · unsent | Medium",
        "COLD · 1 of 2 | Medium",
    ]
    # One write per state change, not one per day: the quiet between rungs must not re-stamp the
    # cell, or the marker would never show the rung the row is actually on.
    assert len(markers) == len(set(markers))
    assert results is not None


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
    assert [e["attempt"] for e in results[m.CARMEN_LADDER_DAYS_COLD[0]]["followups_ready"]] == [1]
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
    # Scheduled one engaged rung past the reply - where route_inbound_reply_to_crm leaves it -
    # so the row sits on rung 1 whatever that rung's offset currently is.
    rung = m.CARMEN_LADDER_DAYS[0]
    sheet = _FakeCarmenSheet(monkeypatch, [_person("talker", (start - timedelta(days=20)).isoformat(),
                                                   next_followup=(start + timedelta(days=rung)).isoformat(),
                                                   note=reply)])
    # Run exactly up to the day the stale-anchor revival fires, so the window ends on the phase
    # change rather than somewhere past it.
    #
    # The revival is gated on BOTH the anchor being older than CARMEN_STALE_ANCHOR_DAYS and the
    # scheduled date no longer looking ladder-shaped, so it lands a terminal gap past the stale
    # bound - not at it. This was written as a literal "+ 12", which happened to equal that sum
    # under the old 7-day grace and silently pointed 7 days past the revival once it changed.
    # The row parks on its terminal date (anchor + CARMEN_TERMINAL_GAP_DAYS) and stays
    # "ladder-shaped" for CARMEN_STALE_ANCHOR_DAYS past THAT date, so the revival fires on the sum.
    # _run_days uses range(), hence the +1 to make the last simulated day the revival day itself.
    #
    # This was a literal "CARMEN_STALE_ANCHOR_DAYS + 12", which equalled that sum only while the
    # grace was 7 days. Deriving it means the window tracks the cadence instead of drifting.
    revival_day = pipeline_utils.CARMEN_TERMINAL_GAP_DAYS + pipeline_utils.CARMEN_STALE_ANCHOR_DAYS + 1
    results = _run_days(sheet, start, revival_day + 1)

    # The reply restarted the ladder from its own date: the single engaged nudge after the reply.
    nudge_days = sorted(n for n, r in results.items() if r["followups_ready"])
    assert nudge_days == list(m.CARMEN_LADDER_DAYS)
    # Every morning until acted on, but only while the reply anchor is still fresh: at
    # CARMEN_STALE_ANCHOR_DAYS past the reply the row is revived onto the ladder instead, which is
    # the intended escape from nagging Kevin about the same contact forever.
    first_promote = m.CARMEN_LADDER_DAYS[-1] + pipeline_utils.CARMEN_KILL_GRACE_DAYS
    # Ends the day the stale-anchor revival fires - that day the row rejoins the ladder instead
    # of waiting on Kevin, which is the intended escape from nagging him forever.
    revives_on = max(results)
    assert results[revives_on]["revived"], "sanity: the window ends at the revival, not mid-promote"
    promote_days = [n for n, r in results.items() if r["ready_to_promote"]]
    assert promote_days == list(range(first_promote, revives_on))
    assert sheet.moves == []
    entry = results[first_promote]["ready_to_promote"][0]
    assert entry["replied_on"] == start.isoformat()
    card = m.render_followup_needs_card(results[first_promote])
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

    # clear_followup sits between the move and the note: see
    # test_promote_clears_the_followup_date_but_demote_keeps_it.
    assert [p["action"] for p in enqueued] == ["update_status", "clear_followup", "set_status", "append_note"]
    assert enqueued[0] == {**enqueued[0], "sheet_uuid": _PROMOTE_UUID, "new_tab": "Carmen Hot"}
    assert re.match(r"^\[\d{4}-\d{2}-\d{2}\] Promoted from Carmen Cold to Carmen Hot", enqueued[3]["note"])
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


# ---- Inbound rejection -> Died, with a pick card when the company holds several live roles ----

# Verbatim from the IHA/Trinity Workday rejection that prompted this path.
_REAL_REJECTION_BODY = (
    "Dear Kevin , Thank you for your interest in the EHR Clinical Analyst - Onsite in Southeast "
    "Michigan position at IHA Medical Group . After careful consideration, your application will "
    "not be moving forward to the next phase of our recruiting process due to the skillset we are "
    "seeking to fill this role."
)


def _reject_job_row(uuid_val, company, title):
    """Distinct name: _job_row is redefined later in this file for the link-sweep tests, and the
    later definition wins for the whole module."""
    return {"sheet_uuid": uuid_val, "company": company, "job_title": title, "status": "Applied"}


@pytest.fixture(autouse=True)
def _clear_pending_kills():
    """PENDING_REJECTION_KILLS is module-global, so a card parked by one test would otherwise be
    visible to the next one under random ordering."""
    m.PENDING_REJECTION_KILLS.clear()
    yield
    m.PENDING_REJECTION_KILLS.clear()


def _rejection_env(monkeypatch, rows_by_code=None):
    enqueued, outcomes = [], []
    rows_by_code = rows_by_code or {}
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(r) for r in rows_by_code.get(code, [])])
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)
    monkeypatch.setattr(m, "record_application_outcome",
                        lambda uuid_val, status, **kw: outcomes.append((uuid_val, status)) or True)
    return enqueued, outcomes


def test_company_is_read_from_the_rejection_body_not_the_sender():
    """trinityhealth@myworkday.com resolves to no company at all - company_domain_of() returns ""
    for an ATS domain - so the employer has to come from the text."""
    assert m.extract_company_from_rejection(
        "Update on your application for 00681317 - EHR Clinical Analyst",
        _REAL_REJECTION_BODY) == "IHA Medical Group"
    # No recognisable phrasing -> "" , never a guess.
    assert m.extract_company_from_rejection("Re: your application", "We are moving on. Best of luck.") == ""


def test_a_single_live_row_is_archived_without_asking(monkeypatch):
    enqueued, outcomes = _rejection_env(monkeypatch, {
        "TW": [_reject_job_row("uuid-iha", "IHA Medical Group", "EHR Clinical Analyst")]})

    line = m.route_rejection_to_died("IHA Medical Group")

    assert [p["action"] for p in enqueued] == ["update_status", "append_note"]
    assert enqueued[0] == {**enqueued[0], "sheet_uuid": "uuid-iha", "new_tab": "Died"}
    assert outcomes == [("uuid-iha", "rejection")]
    assert "Auto-archived to Died" in line and "EHR Clinical Analyst" in line
    assert m.PENDING_REJECTION_KILLS == {}


def test_two_live_rows_write_nothing_and_raise_a_pick_card(monkeypatch):
    """The ATS names ONE req. Auto-killing every Trinity row would archive live applications."""
    enqueued, outcomes = _rejection_env(monkeypatch, {
        "TC": [_reject_job_row("uuid-a", "Trinity Health MI", "EHR Clinical Analyst")],
        "TW": [_reject_job_row("uuid-b", "Trinity Health MI", "Data Analyst")],
    })

    line = m.route_rejection_to_died("Trinity Health MI")

    assert enqueued == [] and outcomes == []            # nothing written
    assert "2 live roles" in line
    assert "<b>1.</b> EHR Clinical Analyst" in line and "<b>2.</b> Data Analyst" in line
    assert "/kill Trinity Health MI 1" in line
    # The pick card must carry no 🆔: swipe recovery takes the first uuid it finds.
    assert "🆔" not in line


def test_kill_picks_one_row_from_the_pending_card(monkeypatch):
    enqueued, outcomes = _rejection_env(monkeypatch, {
        "TC": [_reject_job_row("uuid-a", "Trinity Health MI", "EHR Clinical Analyst")],
        "TW": [_reject_job_row("uuid-b", "Trinity Health MI", "Data Analyst")],
    })
    m.route_rejection_to_died("Trinity Health MI")
    enqueued.clear()

    reply = m.resolve_pending_kill("Trinity Health MI", "2")

    assert [p["sheet_uuid"] for p in enqueued if p["action"] == "update_status"] == ["uuid-b"]
    assert outcomes == [("uuid-b", "rejection")]
    assert "Data Analyst" in reply
    # Cleared, so a second /kill cannot re-archive rows already moved.
    assert m.PENDING_REJECTION_KILLS == {}
    assert "Nothing pending" in m.resolve_pending_kill("Trinity Health MI", "1")


def test_kill_all_archives_every_row_and_rejects_bad_picks(monkeypatch):
    rows = {"TC": [_reject_job_row("uuid-a", "Trinity Health MI", "Role A")],
            "TW": [_reject_job_row("uuid-b", "Trinity Health MI", "Role B")]}
    enqueued, outcomes = _rejection_env(monkeypatch, rows)
    m.route_rejection_to_died("Trinity Health MI")
    assert "Out of range" in m.resolve_pending_kill("Trinity Health MI", "9")
    assert "Pick a number" in m.resolve_pending_kill("Trinity Health MI", "second")
    assert m.PENDING_REJECTION_KILLS                      # a bad pick must NOT discard the card

    enqueued.clear()
    reply = m.resolve_pending_kill("Trinity Health MI", "all")
    assert sorted(p["sheet_uuid"] for p in enqueued if p["action"] == "update_status") == ["uuid-a", "uuid-b"]
    assert "Archived 2 role(s)" in reply


def test_rejection_never_touches_carmen_contacts(monkeypatch):
    """A recruiter who rejected one req is still a live contact for the next one - Killed is for
    contacts archived after three unanswered nudges, not for anyone who said no once."""
    enqueued, _ = _rejection_env(monkeypatch, {
        "TW": [_reject_job_row("uuid-iha", "IHA Medical Group", "EHR Clinical Analyst")],
        "CC": [{"sheet_uuid": "uuid-person", "name": "A Recruiter", "company": "IHA Medical Group",
                "email": "r@iha.org"}],
    })
    m.route_rejection_to_died("IHA Medical Group")
    assert [p["sheet_uuid"] for p in enqueued if p["action"] == "update_status"] == ["uuid-iha"]
    assert all(p.get("new_tab") != "Killed" for p in enqueued)


def test_an_unmatched_company_archives_nothing(monkeypatch):
    enqueued, outcomes = _rejection_env(monkeypatch, {"TW": [
        _reject_job_row("uuid-other", "Some Other Co", "Analyst")]})
    assert m.route_rejection_to_died("IHA Medical Group") == ""
    assert enqueued == [] and outcomes == []
    assert m.route_rejection_to_died("") == ""


def _beth(uuid_val="beth-uuid-0000-0000", name="Beth Young", email="beth.young@altarum.org"):
    """A Carmen Cold row as /e's capture path writes it - no cached job, so no short_id."""
    return {"sheet_uuid": uuid_val, "name": name, "email": email,
            "company": "Altarum", "last_contact": "2026-09-21", "next_followup": "2026-09-25"}


def test_promote_by_email_moves_an_existing_carmen_contact(monkeypatch):
    """The UUID is hidden in Column J, so the address on screen has to work."""
    sent, enqueued = _promote_env(monkeypatch, cold=[_beth()])
    _dispatch("/promote beth.young@altarum.org")

    assert [p["action"] for p in enqueued] == ["update_status", "clear_followup", "set_status", "append_note"]
    assert enqueued[0] == {**enqueued[0], "sheet_uuid": "beth-uuid-0000-0000", "new_tab": "Carmen Hot"}
    assert "Promoted Beth Young" in sent[0]


def test_promote_clears_the_followup_date_but_demote_keeps_it(monkeypatch):
    """Beth landed in Carmen Hot showing 9/24 - the outreach ladder's nudge date, carried across
    the move, for a conversation that had already happened. Carmen Hot is not in
    SEQUENCER_SCAN_TABS, so nothing reads it; blank hands the column to Kevin's date picker."""
    sent, enqueued = _promote_env(monkeypatch, cold=[_beth()])
    _dispatch("/promote beth.young@altarum.org")

    assert [p["action"] for p in enqueued] == ["update_status", "clear_followup", "set_status", "append_note"]
    assert enqueued[1]["sheet_uuid"] == "beth-uuid-0000-0000"
    assert "Next Followup Date cleared" in sent[0]

    # The bench IS a follow-up cadence, so /demote must not blank it.
    _, enqueued2 = _promote_env(monkeypatch, hot=[_beth()])
    _dispatch("/demote beth.young@altarum.org")
    assert "clear_followup" not in [p["action"] for p in enqueued2]


def _hot_row(name, when, status="Follow-up Due", company="Altarum"):
    return {"sheet_uuid": f"uuid-{name.lower().replace(' ', '-')}", "name": name,
            "company": company, "email": f"{name.split()[0].lower()}@x.com",
            "status": status, "next_followup": when}


def _hot_env(monkeypatch, rows):
    monkeypatch.setattr(m, "fetch_networking_cards",
                        lambda code, qty=None: [dict(r) for r in rows] if code == "CH" else [])


def test_carmen_hot_classifies_by_the_date_kevin_typed(monkeypatch):
    """The picker date is the whole point - nothing read that column before."""
    _hot_env(monkeypatch, [
        _hot_row("Beth Young", "2026-09-19"),      # 3 days ago
        _hot_row("Cara Today", "2026-09-22"),      # today
        _hot_row("Dan Later", "2026-09-25"),       # in 3 days
        _hot_row("Eve Undated", ""),
    ])
    out = m.scan_carmen_hot_conversations(date(2026, 9, 22))

    assert [(e["name"], e["state"]) for e in out] == [
        ("Beth Young", "overdue"), ("Cara Today", "today"),
        ("Dan Later", "upcoming"), ("Eve Undated", "undated")]
    assert out[0]["days"] == -3 and out[2]["days"] == 3


def test_carmen_hot_puts_the_longest_overdue_first(monkeypatch):
    """The post-call follow-up is the thing that actually gets dropped."""
    _hot_env(monkeypatch, [
        _hot_row("Recent Miss", "2026-09-21"),
        _hot_row("Old Miss", "2026-09-10"),
        _hot_row("Upcoming", "2026-09-30"),
    ])
    out = m.scan_carmen_hot_conversations(date(2026, 9, 22))
    assert [e["name"] for e in out] == ["Old Miss", "Recent Miss", "Upcoming"]


def test_carmen_hot_drops_settled_conversations(monkeypatch):
    """A closed thread is not something that needs Kevin today."""
    _hot_env(monkeypatch, [
        _hot_row("Live One", "2026-09-19"),
        _hot_row("Done Deal", "2026-09-19", status="No Longer Relevant"),
    ])
    out = m.scan_carmen_hot_conversations(date(2026, 9, 22))
    assert [e["name"] for e in out] == ["Live One"]


def test_carmen_hot_survives_a_garbled_date(monkeypatch):
    """A hand-typed cell can hold anything; it must not crash the morning card."""
    _hot_env(monkeypatch, [_hot_row("Typo Person", "next tuesday")])
    out = m.scan_carmen_hot_conversations(date(2026, 9, 22))
    assert out[0]["state"] == "undated"


def test_the_daily_card_leads_with_live_conversations(monkeypatch):
    """An overdue post-interview follow-up outranks every cold nudge on the card."""
    card = m.render_followup_needs_card({
        "counts": {"live_conversations": 2, "followups_ready": 1},
        "live_conversations": [
            {"name": "Beth Young", "company": "Altarum", "status": "Phone Screen",
             "when": "2026-09-19", "state": "overdue", "days": -3},
            {"name": "Dan Later", "company": "Sanctuary", "status": "Networking Call",
             "when": "2026-09-25", "state": "upcoming", "days": 3},
        ],
        "followups_ready": [{"company": "X", "role": "Analyst", "short_id": "abc",
                             "next_followup": "2026-09-22"}],
    })
    assert "Live conversations (2)" in card and "1 need you now" in card
    assert "Beth Young" in card and "3d ago" in card and "Phone Screen" in card
    # Live conversations must come BEFORE the cold-nudge section.
    assert card.index("Live conversations") < card.index("Nudge these people")
    assert "nothing here is auto-sent" in card


def test_carmen_hot_is_never_added_to_the_sequencer_scan(monkeypatch):
    """Surfacing is not automating: an auto-bump to someone with a call booked is wrong, and
    followup_action() can return bury_ghosted."""
    assert all(code != "CH" for code, _ in m.SEQUENCER_SCAN_TABS)


def test_people_statuses_can_never_rank_as_job_applications():
    """Column F is the same position in both schemas and statusRank() buckets it for funnel_stats.
    A contact set to a JOBS word would be counted as a real application at that stage."""
    people_vocab = ["Cold Lead", "Warm Lead", "Phone Screen", "Interview", "Networking Call",
                    "Referral", "Follow-up Due", "No Longer Relevant"]
    assert all(m.status_rank(s) == -1 for s in people_vocab)
    # "Interview" is NOT "Interviewing" - the near-miss is the whole reason this is checked.
    assert m.status_rank("Interview") == -1 and m.status_rank("Interviewing") == 4
    assert m.CARMEN_HOT_DEFAULT_STATUS in people_vocab


def test_promote_sets_an_honest_status_not_cold_lead(monkeypatch):
    """Beth replied and is booking a call, and her row still read "Cold Lead"."""
    _, enqueued = _promote_env(monkeypatch, cold=[_beth()])
    _dispatch("/promote beth.young@altarum.org")

    status_writes = [p for p in enqueued if p["action"] == "set_status"]
    assert len(status_writes) == 1
    assert status_writes[0]["status"] == m.CARMEN_HOT_DEFAULT_STATUS
    assert status_writes[0]["status"] != "Cold Lead"
    assert m.status_rank(status_writes[0]["status"]) == -1     # never a funnel application


def test_a_job_card_promote_writes_no_followup_date(monkeypatch):
    """The created-from-scratch path must match: an invented ladder date in Carmen Hot looks like
    a commitment Kevin never made."""
    _, written, _ = _job_card_env(monkeypatch, job=_ALTARUM_JOB)
    _dispatch("/promote 4e886991 Beth Young beth.young@altarum.org")
    assert written[0]["next_followup"] == ""


def test_promote_by_full_name_spanning_two_words(monkeypatch):
    """"Beth Young" arrives split across the token and the trailing group."""
    sent, enqueued = _promote_env(monkeypatch, cold=[_beth()])
    _dispatch("/promote Beth Young")
    assert enqueued[0]["new_tab"] == "Carmen Hot"
    assert "Promoted Beth Young" in sent[0]


def test_promote_by_name_is_case_insensitive_and_demote_works_too(monkeypatch):
    _, enqueued = _promote_env(monkeypatch, cold=[_beth()])
    _dispatch("/promote beth young")
    assert enqueued[0]["new_tab"] == "Carmen Hot"

    _, enqueued2 = _promote_env(monkeypatch, hot=[_beth()])
    _dispatch("/demote beth.young@altarum.org")
    assert enqueued2[0]["new_tab"] == "Carmen Warm"


def test_promote_by_name_refuses_two_people_with_the_same_name(monkeypatch):
    """Ambiguity must refuse rather than guess which row to move."""
    sent, enqueued = _promote_env(monkeypatch, cold=[
        _beth("uuid-a-0000-0000", email="beth.young@altarum.org"),
        _beth("uuid-b-0000-0000", email="b.young@example.com"),
    ])
    _dispatch("/promote Beth Young")
    assert enqueued == [] and "matches more than one contact" in sent[0]


def test_promote_partial_name_does_not_match(monkeypatch):
    """A bare first name is a coin flip the day a second Beth is captured."""
    sent, enqueued = _promote_env(monkeypatch, cold=[_beth()])
    _dispatch("/promote beth")
    assert enqueued == [] and "is not a Carmen Cold or Carmen Hot contact" in sent[0]


def test_an_existing_contact_is_moved_never_duplicated_by_the_job_card_form(monkeypatch):
    """/promote <job_id> Beth Young beth.young@altarum.org when Beth ALREADY has a row: the
    contact lookup wins, so she is promoted rather than written a second time."""
    sent, enqueued = _promote_env(monkeypatch, cold=[_beth()])
    written = []
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda p: written.append(p) or True)
    _dispatch("/promote beth.young@altarum.org Beth Young")
    assert written == []
    assert enqueued[0]["new_tab"] == "Carmen Hot"


def _job_card_env(monkeypatch, job=None, logged=False, write_ok=True):
    """/promote <job_id> Name email - the job-card form. Carmen tabs are deliberately EMPTY:
    the whole point is the person has no contact row yet."""
    sent, written, cached = [], [], []
    monkeypatch.setattr(m, "fetch_networking_cards", lambda code, qty=None: [])
    monkeypatch.setattr(m, "get_sheet_uuid_by_short_id", lambda sid: None)
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: None)
    monkeypatch.setattr(m, "get_job_from_cache", lambda t: dict(job) if job else {})
    monkeypatch.setattr(m, "get_job_by_sheet_uuid", lambda t: {})
    monkeypatch.setattr(m, "is_logged_person_contact", lambda e: logged)
    monkeypatch.setattr(m, "log_to_sheets_crm", lambda p: written.append(p) or write_ok)
    monkeypatch.setattr(m, "record_captured_contact", lambda **kw: cached.append(kw) or True)
    return sent, written, cached


_ALTARUM_JOB = {"employer_name": "Altarum", "job_title": "Business Technology Analyst"}


def test_promote_from_a_job_card_creates_a_carmen_hot_contact(monkeypatch):
    """The real case: Beth replied from an address the CRM has never seen, and the only 🆔 in
    Telegram is the Altarum JOB card."""
    sent, written, cached = _job_card_env(monkeypatch, job=_ALTARUM_JOB)
    _dispatch("/promote 4e886991 Beth Young beth.young@altarum.org")

    assert len(written) == 1
    p = written[0]
    assert p["action"] == "quick_add" and p["target_code"] == "CH"
    assert p["name"] == "Beth Young" and p["email"] == "beth.young@altarum.org"
    assert p["company"] == "Altarum" and p["status"] == m.CARMEN_HOT_DEFAULT_STATUS
    assert "Business Technology Analyst" in p["note"]
    # The local cache mirrors the row, so the NEXT /promote finds her as a contact.
    assert cached[0]["sheet_tab"] == "Carmen Hot"
    assert cached[0]["contact_email"] == "beth.young@altarum.org"
    assert "Added Beth Young" in sent[0] and "job row was not moved" in sent[0]


def test_promote_from_a_job_card_never_moves_the_job_row(monkeypatch):
    """A JOBS->PEOPLE tab move would blank the name column and delete the application. The job
    id is READ for its company and nothing else."""
    sent, written, _ = _job_card_env(monkeypatch, job=_ALTARUM_JOB)
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)
    _dispatch("/promote 4e886991 Beth Young beth.young@altarum.org")

    assert enqueued == []                                    # no update_status, no tab move
    assert all(p["action"] != "update_status" for p in written)
    assert "/interview 4e886991" in sent[0]                   # points at the command that advances it


def test_promote_job_card_derives_the_name_when_only_an_email_is_given(monkeypatch):
    _, written, _ = _job_card_env(monkeypatch, job=_ALTARUM_JOB)
    _dispatch("/promote 4e886991 beth.young@altarum.org")
    assert written[0]["name"] == "Beth Young"                 # from the local part


def test_promote_job_card_refuses_role_mailboxes_and_duplicates(monkeypatch):
    """bizops@ is the inbox the outreach went TO, not the person who replied."""
    sent, written, _ = _job_card_env(monkeypatch, job=_ALTARUM_JOB)
    _dispatch("/promote 4e886991 Biz Ops bizops@altarum.org")
    assert written == [] and "role mailbox" in sent[0]

    sent2, written2, _ = _job_card_env(monkeypatch, job=_ALTARUM_JOB, logged=True)
    _dispatch("/promote 4e886991 Beth Young beth.young@altarum.org")
    assert written2 == [] and "already a CRM contact" in sent2[0]


def test_promote_job_card_reports_a_failed_crm_write(monkeypatch):
    """A silent failure here would leave Kevin believing the contact exists."""
    sent, _, cached = _job_card_env(monkeypatch, job=_ALTARUM_JOB, write_ok=False)
    _dispatch("/promote 4e886991 Beth Young beth.young@altarum.org")
    assert cached == [] and "CRM write failed" in sent[0]


def test_promote_with_a_job_id_and_no_email_explains_both_commands(monkeypatch):
    """The message that cost Kevin five minutes: "not a contact" is true but unactionable."""
    sent, written, _ = _job_card_env(monkeypatch, job=_ALTARUM_JOB)
    _dispatch("/promote 4e886991")
    assert written == []
    assert "is a JOB, not a contact" in sent[0]
    assert "/interview 4e886991" in sent[0] and "Name name@company.com" in sent[0]


def test_promote_job_card_form_leaves_the_plain_contact_promote_untouched(monkeypatch):
    """Regression: the two-token form must still move an existing Carmen Cold row."""
    sent, enqueued = _promote_env(monkeypatch, cold=[_person(_PROMOTE_UUID, "2026-05-01")])
    _dispatch(f"/promote {_PROMOTE_UUID[:8]}")
    assert [p["action"] for p in enqueued] == ["update_status", "clear_followup", "set_status", "append_note"]
    assert enqueued[0]["new_tab"] == "Carmen Hot"


def test_help_lists_promote_and_demote(monkeypatch):
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)
    _dispatch("/help")
    menu = "".join(sent)
    # /promote's label now names what Kevin can actually see and type - the UUID is hidden in
    # Column J, so "<id>" alone was never the usual way in.
    assert "/promote &lt;email | name | id&gt;" in menu and "/demote &lt;id&gt;" in menu
    assert "/promote &lt;job id&gt; Name email" in menu
    # The optional date is the part Kevin will not remember six weeks from now, so every place
    # that prints these commands has to show the template, not just the bare id form.
    assert "/interview &lt;id&gt; [YYYY-MM-DD]" in menu


def test_the_daily_card_prints_the_interview_date_template(monkeypatch):
    """The follow-up card's footer is what Kevin actually reads while acting on a row - a bare
    "/interview <id>" there hides the date argument no matter what /help says."""
    card = m.render_followup_needs_card({
        "counts": {"followups_ready": 1},
        "followups_ready": [{"company": "Altarum", "role": "Business Technology Analyst",
                             "short_id": "abc123", "next_followup": "2026-09-25"}],
    })
    assert "/interview &lt;id&gt; [YYYY-MM-DD]" in card


# ---- Daily "needs you today" card (render_followup_needs_card) ----

def test_needs_card_renders_every_populated_section(monkeypatch):
    _mock_sequencer_crm(monkeypatch)
    card = m.render_followup_needs_card(m.run_followup_sequencer(today=_SEQ_TODAY, dry_run=True))
    assert "Needs You Today" in card
    assert "Applications going quiet (1)" in card
    assert f"{m.FOLLOWUP_1_DAYS}d silent" in card
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


def test_needs_card_gives_each_person_a_draft_link_without_breaking_swipe_safety():
    """One-tap drafting from the card. The uuid now appears - but inside a URL, where the swipe
    recovery parser does not read it, so the multi-entry invariant still holds: a swipe on this
    card must resolve to NOTHING rather than silently to entry #1.

    The link is the ✉️ glyph alone, not the person's name: tapping it creates a Gmail draft, and a
    linked name across 24 rows is a mis-tap waiting to happen while scrolling."""
    uuid_a, uuid_b = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    ready = [
        {"company": "Acme", "role": "", "name": "Dana", "attempt": 1, "draft_text": "hi",
         "short_id": "abc123", "sheet_uuid": uuid_a, "sheet_tab": "Carmen Cold",
         "email": "dana@acme.com", "next_followup": "2026-06-01", "new_next_followup": "2026-06-08"},
        # No address on file - nothing to draft, so this row gets no link rather than a dead one.
        {"company": "Beta", "role": "", "name": "Sam", "attempt": 2, "draft_text": "hi",
         "short_id": "def456", "sheet_uuid": uuid_b, "sheet_tab": "Carmen Cold",
         "email": "", "next_followup": "2026-06-01", "new_next_followup": "2026-06-08"},
    ]
    card = m.render_followup_needs_card(
        {"followups_ready": ready, "going_cold": [], "buried": [], "top_matched": [],
         "counts": {"followups_ready": 2}})

    assert f"<a href='{m.BASE_URL}/followups/draft/{uuid_a}'>✉️</a>" in card
    assert uuid_b not in card
    assert m._parse_sheet_uuid_from_card_text(card) == (None, None)
    assert "✉️ drafts the follow-up in Gmail" in card


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


def _sendable(**over):
    """A follow-up entry that passes every auto-send gate; override one field per test."""
    base = {"sheet_uuid": "u-1", "email": "dana@acme.com", "name": "Dana",
            "title": "Operations Analyst", "company": "Acme", "company_raw": "Acme",
            "draft_text": "Hi Dana,\n\nCircling back.\n\nBest,\nKevin", "note": "",
            "next_followup": "2026-09-20"}
    base.update(over)
    return base


def test_a_clean_followup_is_allowed_to_autosend():
    assert m.autosend_block_reason(_sendable(), _sendable()["draft_text"]) is None


@pytest.mark.parametrize("override,reason", [
    ({"email": ""}, "no_address"),
    ({"email": "operations@acme.com [⚠️ Fallback Email]"}, "guessed_address"),
    ({"email": "operations@acme.com"}, "role_mailbox"),
    ({"name": ""}, "no_name"),
    ({"title": "Financial Operations Analyst Intermediate /work from home reputed company reputed company/"},
     "messy_title"),
    ({"title": "AlixPartners"}, "messy_title"),
])
def test_the_gate_refuses_every_way_an_autosend_can_embarrass_him(override, reason):
    """Each of these is survivable in a draft Kevin reads, and not survivable once the message
    leaves on its own. The gate fails CLOSED: the row goes back to being a manual draft."""
    entry = _sendable(**override)
    assert m.autosend_block_reason(entry, entry["draft_text"]) == reason


def test_the_gate_refuses_a_row_whose_contact_already_replied():
    """The reply collision. Re-anchoring re-TIMES a bump; it does not cancel it, and a bump to
    someone mid-conversation is exactly what makes a system look automated."""
    note = f"[2026-09-22] {m.INBOUND_REPLY_NOTE_MARKER} - asked for a call"
    entry = _sendable(note=note)
    assert m.autosend_block_reason(entry, entry["draft_text"]) == "already_replied"


def test_the_gate_refuses_a_row_touched_on_linkedin():
    """/linkedin feeds the same gate: Kevin messaged them there, so the email must not fire."""
    note = f"[2026-09-22] {m.LINKEDIN_TOUCH_NOTE_MARKER} - connect/DM sent by hand."
    entry = _sendable(note=note)
    assert m.autosend_block_reason(entry, entry["draft_text"]) == "already_replied"


def test_the_gate_refuses_an_unfilled_placeholder():
    """An unrendered {company} means interpolation fell through to the raw template."""
    entry = _sendable(draft_text="Hi Dana,\n\nAbout the role at {company}.\n\nKevin")
    assert m.autosend_block_reason(entry, entry["draft_text"]) == "unresolved_placeholder"
    assert m.autosend_block_reason(_sendable(), "") == "empty_draft"


def test_multiple_contacts_at_one_company_are_staggered_across_days():
    """THE MEASURED BUG: three emails to flagstar.com inside 7 minutes. Same three people, same
    ladder - spaced two days apart, so only the longest-waiting one goes today."""
    entries = [
        _sendable(sheet_uuid="a", email="a@flagstar.com", name="Andy", next_followup="2026-09-20"),
        _sendable(sheet_uuid="b", email="b@flagstar.com", name="Zach", next_followup="2026-09-21"),
        _sendable(sheet_uuid="c", email="c@flagstar.com", name="Karen", next_followup="2026-09-22"),
    ]
    for e in entries:
        e["company_raw"] = "Flagstar Bank"

    m._apply_autosend_plan(entries, date(2026, 9, 23))

    assert [e["autosend"] for e in entries] == [True, False, False]
    assert [e["autosend_block"] for e in entries] == [None, "company_spacing", "company_spacing"]
    # And the held rows are told when they go, rather than silently vanishing.
    # 24h23m apart, minute-precision: the drift is the point, so it is asserted, not rounded away.
    assert [e["autosend_on"] for e in entries] == [
        "2026-09-23 07:30", "2026-09-24 07:53", "2026-09-25 08:16"]


def test_different_companies_all_send_the_same_day():
    """Spacing is per EMPLOYER, not a global throttle - one contact each at three companies is
    not the pattern that trips a corporate filter."""
    entries = [_sendable(sheet_uuid=s, email=f"x@{c}.com", company_raw=c)
               for s, c in (("a", "acme"), ("b", "beta"), ("c", "gamma"))]

    m._apply_autosend_plan(entries, date(2026, 9, 23))

    assert all(e["autosend"] for e in entries)


def test_a_legal_suffix_does_not_split_one_company_into_two():
    """"Acme Corp" and "Acme Corp Inc." are one employer; treating them as two would send both
    on the same morning, which is the exact thing the spacing exists to stop."""
    entries = [_sendable(sheet_uuid="a", email="a@acme.com", company_raw="Acme Corp"),
               _sendable(sheet_uuid="b", email="b@acme.com", company_raw="Acme Corp Inc.")]

    m._apply_autosend_plan(entries, date(2026, 9, 23))

    assert [e["autosend"] for e in entries] == [True, False]


def test_a_blocked_row_never_consumes_a_companys_slot():
    """A colleague whose message can never auto-send must not push an eligible one to next week."""
    entries = [_sendable(sheet_uuid="bad", email="", company_raw="Acme"),
               _sendable(sheet_uuid="good", email="dana@acme.com", company_raw="Acme")]

    m._apply_autosend_plan(entries, date(2026, 9, 23))

    assert entries[0]["autosend"] is False and entries[0]["autosend_block"] == "no_address"
    assert entries[1]["autosend"] is True, "the sendable colleague still goes today"


def test_the_run_cap_bounds_unattended_volume():
    """Unattended volume is what burns a sender. Past the ceiling rows defer, never drop."""
    entries = [_sendable(sheet_uuid=str(i), email=f"x@c{i}.com", company_raw=f"c{i}")
               for i in range(m.MAX_AUTOSENDS_PER_RUN + 3)]

    m._apply_autosend_plan(entries, date(2026, 9, 23))

    assert sum(1 for e in entries if e["autosend"]) == m.MAX_AUTOSENDS_PER_RUN
    assert all(e["autosend_block"] == "run_cap" for e in entries if not e["autosend"])


def _linkedin_env(monkeypatch, rows):
    """Stub the tab scan /linkedin walks, and capture what it sends and enqueues."""
    sent, enqueued = [], []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, **k: sent.append(t))
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)
    monkeypatch.setattr(m, "log_daily_activity", lambda *a, **k: None)
    monkeypatch.setattr(m, "record_command_usage", lambda *a, **k: None)
    # /linkedin now asks Code.gs to search every tab instead of pulling tabs down and scanning
    # here, so the stub answers the lookup rather than the tab fetch.
    class _Res:
        status_code = 200
        def __init__(self, payload): self._p = payload
        def json(self): return self._p
    def _lookup(params):
        email = str(params.get("email") or "").lower()
        for tab, recs in rows.items():
            for r in recs:
                cell = re.sub(r'\s*\[.*?\]\s*', '', str(r.get("email") or "")).strip().lower()
                if cell and cell == email:
                    return _Res({"status": "success", "found": True,
                                 "sheet_uuid": r.get("sheet_uuid", ""), "sheet_tab": tab,
                                 "name": r.get("name", ""), "company": r.get("company", "")})
        return _Res({"status": "success", "found": False})
    monkeypatch.setattr(m, "crm_get", _lookup)
    return sent, enqueued


def test_linkedin_logs_an_outbound_touch_against_the_matching_row(monkeypatch):
    """The reply-collision fix: Kevin messaged them on LinkedIn, so the email ladder must stop
    treating them as untouched. Found by email address across the scanned tabs."""
    sent, enqueued = _linkedin_env(monkeypatch, {"Carmen Cold": [
        {"sheet_uuid": "u-1", "email": "dana@acme.com", "name": "Dana Reed", "company": "Acme"}]})

    _dispatch("/linkedin dana@acme.com")

    assert len(enqueued) == 1
    assert enqueued[0]["action"] == "append_note" and enqueued[0]["sheet_uuid"] == "u-1"
    assert m.LINKEDIN_TOUCH_NOTE_MARKER in enqueued[0]["note"]
    assert "Dana Reed" in sent[0] and "Carmen Cold" in sent[0]


def test_linkedin_matches_through_a_bracketed_confidence_tag(monkeypatch):
    """The stored cell can read "dana@acme.com [⚠️ Fallback Email]" - the address still matches."""
    sent, enqueued = _linkedin_env(monkeypatch, {"Tetiana Warm": [
        {"sheet_uuid": "u-2", "email": "dana@acme.com [⚠️ Fallback Email]", "name": "Dana"}]})

    _dispatch("/linkedin DANA@ACME.COM")

    assert len(enqueued) == 1 and enqueued[0]["sheet_uuid"] == "u-2"


def test_linkedin_writes_nothing_when_no_row_matches(monkeypatch):
    """A miss must be loud and harmless: no note may be appended to a row Kevin did not mean."""
    sent, enqueued = _linkedin_env(monkeypatch, {"Carmen Cold": [
        {"sheet_uuid": "u-1", "email": "someone@else.com", "name": "Someone"}]})

    _dispatch("/linkedin dana@acme.com")

    assert enqueued == []
    assert "No CRM row found" in sent[0]


def test_linkedin_says_unreachable_rather_than_not_found_on_a_crm_outage(monkeypatch):
    """A dead CRM read must never read as a successful write, and must not be reported as "no row
    found" either - that would send Kevin off to re-add a contact who is already there."""
    sent, enqueued = _linkedin_env(monkeypatch, {})
    monkeypatch.setattr(m, "crm_get", lambda params: None)

    _dispatch("/linkedin dana@acme.com")

    assert enqueued == []
    assert "unreachable" in sent[0].lower()


def test_linkedin_finds_a_contact_outside_the_sequencer_tabs(monkeypatch):
    """Code.gs searches EVERY tab. The original hand-rolled scan only walked the four sequencer
    tabs, so an older networking contact in Carmen Warm was silently unreachable."""
    sent, enqueued = _linkedin_env(monkeypatch, {"Carmen Warm": [
        {"sheet_uuid": "u-9", "email": "old@friend.com", "name": "Sam", "company": "Beta"}]})

    _dispatch("/linkedin old@friend.com")

    assert len(enqueued) == 1 and enqueued[0]["sheet_uuid"] == "u-9"
    assert "Carmen Warm" in sent[0]


def test_linkedin_rejects_a_malformed_address_without_scanning(monkeypatch):
    sent, enqueued = _linkedin_env(monkeypatch, {})

    _dispatch("/linkedin not-an-email")

    assert enqueued == [] and "not a valid email" in sent[0]


def test_linkedin_bare_command_explains_itself(monkeypatch):
    sent, enqueued = _linkedin_env(monkeypatch, {})

    _dispatch("/linkedin")

    assert enqueued == [] and "Usage" in sent[0]


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


def _interview_env(monkeypatch, short_id="abc123"):
    sent, enqueued = [], []
    monkeypatch.setattr(m, "get_sheet_uuid_by_short_id",
                        lambda sid: "uuid-target" if sid == short_id else None)
    monkeypatch.setattr(m, "send_telegram_message", lambda chat_id, text, *a, **k: sent.append(text) or 1)
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)
    return sent, enqueued


def test_interview_with_a_date_anchors_the_followup_to_the_day_after(monkeypatch):
    """Status alone leaves Next Followup Date wherever the outreach ladder put it, so a role
    being actively interviewed for keeps a stale anchor and the sequencer goes quiet."""
    sent, enqueued = _interview_env(monkeypatch)
    _dispatch("/interview abc123 2026-09-25")

    assert [p["action"] for p in enqueued] == ["set_status", "update_snooze", "append_note"]
    assert enqueued[0]["status"] == "Interviewing"
    assert enqueued[1]["next_followup"] == "2026-09-26"          # the day AFTER the call
    assert "Interview scheduled for 2026-09-25" in enqueued[2]["note"]
    assert "Interview:" in sent[0] and "2026-09-26" in sent[0]


def test_interview_without_a_date_is_unchanged(monkeypatch):
    """Regression: the bare form must still write Status and nothing else."""
    _, enqueued = _interview_env(monkeypatch)
    _dispatch("/interview abc123")
    assert [p["action"] for p in enqueued] == ["set_status"]
    assert enqueued[0]["status"] == "Interviewing"


def test_interview_rejects_a_malformed_date_instead_of_defaulting(monkeypatch):
    """Silently writing today's anchor for "9/25" would schedule the wrong follow-up."""
    for bad in ("9/25", "2026-13-01", "next-tuesday", "25-09-2026"):
        sent, enqueued = _interview_env(monkeypatch)
        _dispatch(f"/interview abc123 {bad}")
        assert enqueued == [], f"{bad} should not have written anything"
        assert "Bad date" in sent[0]


def test_replied_refuses_a_date_argument(monkeypatch):
    """/replied has no interview to anchor to - a date there is a typo, not an instruction."""
    sent, enqueued = _interview_env(monkeypatch)
    _dispatch("/replied abc123 2026-09-25")
    assert enqueued == [] and "takes no date" in sent[0]


def test_interview_date_is_validated_before_the_id_is_resolved(monkeypatch):
    """A bad date on an unknown id reports the date, not a confusing "record not found"."""
    sent, enqueued = _interview_env(monkeypatch)
    _dispatch("/interview totally-unknown-id 9/25")
    assert enqueued == [] and "Bad date" in sent[0]


def test_interview_usage_mentions_the_optional_date(monkeypatch):
    sent, enqueued = _interview_env(monkeypatch)
    _dispatch("/interview")
    assert enqueued == [] and "YYYY-MM-DD" in sent[0]


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
    # the cap, 2 tools (python, sql) -> +8, +8 salary in the 90k band, +6 no years-of-experience
    # demand. A flat +10/+15 per category fired on any single hit, which nearly every ops posting
    # clears. Plus +4 for the plain job-family title: "Operations Analyst" carries no junior/entry
    # word, so it earns the smaller family bonus rather than the +6 entry-level one. It previously
    # scored 0 here - this test's old comment credited a "+6 Analyst entry-level title" that never
    # fired, since the entry-level branch matches "analyst i" and not a bare "analyst".
    assert layer1_bonus == 40
    # 100 raw, compressed by soft_cap_score() rather than flattened at the 100 clamp.
    assert final_score == 91


def test_plain_job_family_titles_earn_the_small_title_bonus(monkeypatch):
    """A bare "Operations Analyst" or "EHR Clinical Analyst" is the role actually being targeted,
    but carries no junior/entry-level word, and the entry-level branch matches "analyst i" rather
    than a bare "analyst". Those titles scored 0 from the title rules, which is part of why good
    postings were landing in the 60s and missing the 80-point Tier-1 gate."""
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: default if default is not None else [])
    base = {"employer_name": "Acme", "job_description": "Operations role.", "job_city": "Detroit"}

    def shift(title):
        """The title rules' contribution alone. The description carries its own bonuses (no
        years-of-experience demand, etc.), so measure against a title matching no branch."""
        _, bonus = m.calculate_hybrid_score_modifier(dict(base, job_title=title), 60)
        _, neutral = m.calculate_hybrid_score_modifier(dict(base, job_title="Widget Handler"), 60)
        return bonus - neutral

    for title in ("Operations Analyst", "EHR Clinical Analyst", "Business Administrator", "Office Assistant"):
        assert shift(title) == 4, f"{title} should earn the plain job-family bonus, got {shift(title)}"

    # The explicit entry-level marker still outranks the generic family word.
    assert shift("Junior Operations Analyst") == 6

    # Seniority and wrong-family branches come first in the chain, so a family word never
    # rescues a title the filter is meant to reject.
    assert shift("Senior Operations Analyst") == -18
    assert shift("Data Engineer") == -30


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

    def run(gemini_base, layer1_bonus, job_id="jsearch_1", **job_overrides):
        state.update(
            score=max(1, min(100, gemini_base + layer1_bonus)),
            layer1_bonus=layer1_bonus,
            gemini_base=gemini_base,
        )
        job = {
            "job_title": "Operations Analyst", "employer_name": "Acme Co",
            "job_id": job_id, "job_description": "ops role", "job_city": "Detroit",
        }
        job.update(job_overrides)
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


def test_wildcard_role_earns_exactly_the_wildcard_bonus_through_total_boost(run_candidate):
    """ODDBALL_KEYWORDS used to only badge the card. A match now also earns WILDCARD_BONUS (+2),
    and it must ride in total_boost so the card's (+N) shows it and the stacking cap sees it."""
    assert m.WILDCARD_BONUS == 2
    plain = run_candidate(gemini_base=60, layer1_bonus=10)
    assert plain["score"] == 70 and plain["score_boost"] == 0
    assert "WILDCARD" not in plain["age_badge"]

    wild = run_candidate(gemini_base=60, layer1_bonus=10, job_title="Supply Chain Operations Analyst")
    assert wild["score"] == 72
    assert wild["score_boost"] == 2
    assert "🎲 [WILDCARD ROLE]" in wild["age_badge"]      # badge unchanged


def test_wildcard_bonus_is_trimmed_by_the_stacking_cap(run_candidate, monkeypatch):
    """Layer 1 already at the cap (+30): the wildcard +2 has no headroom and must not leak past
    BONUS_STACK_CAP. With an alum (+20) on top, alum + wildcard together still cap at +30."""
    at_cap = run_candidate(gemini_base=55, layer1_bonus=30, job_description="logistics ops role")
    assert at_cap["score"] == 55 + m.BONUS_STACK_CAP
    assert at_cap["score_boost"] == 0

    monkeypatch.setattr(
        m, "resolve_live_alumni_at_company",
        lambda *a, **k: {"name": "Dana Reyes", "linkedin_url": "https://linkedin.com/in/dana",
                         "headline": "Ops Lead"},
    )
    stacked = run_candidate(gemini_base=50, layer1_bonus=10, job_description="logistics ops role")
    # l1 10 + alum 20 + wildcard 2 = 32 -> capped to 30
    assert stacked["score"] == 50 + m.BONUS_STACK_CAP
    assert stacked["score_boost"] == 20


def test_wildcard_bonus_does_not_open_the_clavicular_gate(run_candidate, monkeypatch):
    """Clavicular's +30 is gated at raw score >= 70. A 68 wildcard must not be lifted to 70 first."""
    monkeypatch.setattr(
        m, "get_warm_crm_contacts",
        lambda: {m.normalize_company_for_match("Acme Co"):
                 {"name": "Sam", "raw_company": "Acme Co", "note": "n", "priority_score": 10}},
    )
    result = run_candidate(gemini_base=68, layer1_bonus=0, job_id="gh_1",
                           job_description="logistics ops role")
    assert result["is_clavicular"] is False
    assert result["score"] == 70 and result["score_boost"] == 2


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
    for track in track_registry.TRACK_LETTERS:
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


def _resolved_bridge_pool(bank, track, tone, billing=False):
    """Mirror of generate_cover_letter's paragraph-2 resolution: track-keyed pool first, then tone.

    Kept in one place so the 6-gram guard below cannot silently check a different pool than the one
    a reader actually receives. The tests that use it also assert the chosen bridge really appears
    in the rendered letter, which pins this mirror to the function instead of to a memory of it.
    """
    pool_key = m.TRACK_BULLET_POOL_KEYS[track]
    bespoke = bank.get(f"bridges_{pool_key}")
    pool = bespoke or bank.get(f"bridges_{tone}") or bank.get("bridges_conservative") or []
    # The billing gate slices index 0 off the SHARED pool only - never off a bespoke one.
    if not billing and not bespoke and len(pool) > 1:
        pool = pool[1:]
    return pool


def test_cover_letter_tone_mode_swaps_only_the_bridge_paragraph():
    """Still the contract for a track on the SHARED pools. Track e has no bespoke bridge, so tone
    is what picks its paragraph 2. Narrowed deliberately when track-keyed pools shipped - see the
    sibling test below for what a bespoke track does instead."""
    bank = m.load_cover_letter_templates()
    assert "bridges_track_e_bizops" not in bank, "track e is the shared-pool case in this test"

    conservative = m.generate_cover_letter("Crain", "Billing Operations Analyst", "e", 0, "", "conservative")
    tech = m.generate_cover_letter("Crain", "Billing Operations Analyst", "e", 0, "", "tech")
    assert conservative != tech
    # Paragraph 1 and the closer are tone-independent; only paragraph 2 moves.
    assert conservative.split("\n\n")[1] == tech.split("\n\n")[1]
    assert conservative.split("\n\n")[3] == tech.split("\n\n")[3]


def test_bespoke_bridge_beats_the_tone_pool():
    """A track with its own bridge pool ignores tone_mode entirely: a track-specific argument is
    more informative than a tone-specific register. Before this, paragraphs 2 and 3 were
    byte-identical across all eight tracks, so a supply-chain letter opened on multi-site
    reconciliation and then pivoted to Salesforce for no reason."""
    bank = m.load_cover_letter_templates()
    for track in ("f", "g", "h", "b", "d"):
        pool_key = m.TRACK_BULLET_POOL_KEYS[track]
        bespoke = bank[f"bridges_{pool_key}"]
        conservative = m.generate_cover_letter("Rivian", "Operations Analyst", track, 1, "", "conservative")
        tech = m.generate_cover_letter("Rivian", "Operations Analyst", track, 1, "", "tech")
        assert conservative == tech, f"track {track} still moves with tone"
        assert bespoke[1 % len(bespoke)] in tech, f"track {track} did not use its own bridge"
        for shared in bank["bridges_tech"] + bank["bridges_conservative"]:
            # bridges_conservative[2] was deliberately COPIED into track f, with the shared entry
            # left in place so that pool keeps its indices (Gemini routes shared bridges by index).
            # So a shared paragraph only indicates a fallback if it is not also bespoke copy.
            if shared in bespoke:
                continue
            assert shared not in tech, f"track {track} fell back to a shared bridge"


def test_track_without_a_bespoke_pool_still_uses_the_shared_one():
    """The fallback is what makes this additive and shippable track by track."""
    bank = m.load_cover_letter_templates()
    for track in ("a", "c", "e"):
        pool_key = m.TRACK_BULLET_POOL_KEYS[track]
        assert f"bridges_{pool_key}" not in bank
        assert f"closers_{pool_key}" not in bank
        letter = m.generate_cover_letter("Acme", "Operations Associate", track, 2, "", "tech")
        expected = _resolved_bridge_pool(bank, track, "tech")
        assert expected[2 % len(expected)] in letter, f"track {track} lost the shared bridge"


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


def test_cover_letter_omits_the_location_on_an_opener_that_cannot_take_one():
    """Opener 1 ends "...and wanted to add some context", which cannot carry a trailing city. The
    location is dropped rather than the opener rewritten or a different one routed, because the
    opener index is part of the routing Gemini already returned."""
    bank = m.load_cover_letter_templates()
    flags = bank["_openers_take_location"]
    assert flags[1] is False, "opener 1 is the one that cannot take a location"

    letter = m.generate_cover_letter("Crain", "Analyst", "e", 1, "Normal, IL")
    assert "add some context." in letter
    assert "Normal" not in letter

    # A flagged-true opener still gets it, so the fix did not just disable the feature.
    assert "in Normal, IL." in m.generate_cover_letter("Crain", "Analyst", "e", 0, "Normal, IL")


def test_no_letter_says_add_some_context_in_a_city():
    """The exact shipped regression. Kevin sent a letter reading "wanted to add some context in
    Dearborn, Michigan." Checked across every routed combination, not just opener 1."""
    for track, tone, idx in _all_letter_combos():
        letter = m.generate_cover_letter("Rivian", "Operations Analyst", track, idx, "Normal, IL", tone)
        assert re.search(r"context in [A-Z]", letter) is None, f"track={track} tone={tone} idx={idx}"
        assert re.search(r"\bin Normal, IL\.(?!\s*$)", letter) or "Normal" not in letter or \
            letter.count("Normal") == 1, f"track={track} idx={idx} location rendered oddly"


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
        # Resolve paragraph 2 exactly as generate_cover_letter does. Reading bank["bridges_<tone>"]
        # directly went blind the moment bespoke track pools shipped: it would have checked a pool
        # those tracks never render, and the guard would have passed on unchecked prose.
        bridges = _resolved_bridge_pool(bank, track, tone)
        bridge = bridges[idx % len(bridges)]
        # Cross-check the mirror against the real output, so this cannot drift from the function.
        rendered = m.generate_cover_letter("Qzco", "Operations Analyst", track, idx, "", tone)
        assert bridge in rendered, f"resolution mirror is wrong for track={track} tone={tone} idx={idx}"
        # Strip placeholders first: {company}/{job_title} legitimately recur across paragraphs,
        # so only the banked prose around them is under test.
        combined = re.sub(r"\{\w+\}", " ", bodies[idx % len(bodies)] + " " + bridge)
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


def test_billing_gate_never_slices_a_track_keyed_pool(monkeypatch):
    """The billing gate skips index 0 of the SHARED bridge/closer pools, because index 0 there is
    deliberately billing-flavored. A track-keyed pool has no such entry, so slicing [1:] off it
    silently drops a perfectly good paragraph and shifts every other index by one.

    Built on a synthetic bank rather than the real one so it tests the gate itself: it fails for
    the right reason even if no shipped track has a bespoke pool yet.
    """
    fake = {
        "openers": ["I am writing about the {job_title} opening at {company}."],
        "track_a_wealth_ops": ["Body paragraph that carries the adjacency claim for this track."],
        "bridges_track_a_wealth_ops": ["BESPOKE-BRIDGE-ZERO.", "BESPOKE-BRIDGE-ONE."],
        "closers_track_a_wealth_ops": ["BESPOKE-CLOSER-ZERO.", "BESPOKE-CLOSER-ONE."],
        "bridges_conservative": ["SHARED-BILLING-BRIDGE.", "SHARED-BRIDGE-ONE."],
        "bridges_tech": ["SHARED-BILLING-BRIDGE.", "SHARED-BRIDGE-ONE."],
        "closers": ["SHARED-BILLING-CLOSER.", "SHARED-CLOSER-ONE."],
        "signoffs": ["Thank you for your time and consideration."],
    }
    monkeypatch.setattr(m, "load_cover_letter_templates", lambda: fake)

    # Non-billing title: the gate fires. A bespoke pool must still surrender its index 0.
    letter = m.generate_cover_letter("Acme", "Operations Associate", "a", 0, "", "conservative")
    assert "BESPOKE-BRIDGE-ZERO." in letter, "gate sliced index 0 off a track-keyed bridge pool"
    assert "BESPOKE-CLOSER-ZERO." in letter, "gate sliced index 0 off a track-keyed closer pool"

    # The shared pools must keep being gated - that behavior is the reason the gate exists.
    del fake["bridges_track_a_wealth_ops"]
    del fake["closers_track_a_wealth_ops"]
    shared = m.generate_cover_letter("Acme", "Operations Associate", "a", 0, "", "conservative")
    assert "SHARED-BILLING-BRIDGE." not in shared
    assert "SHARED-BILLING-CLOSER." not in shared


def test_logistics_and_supply_chain_letters_claim_no_freight_work():
    """Kevin has no trucking, freight, warehouse or inventory experience. Tracks f and g are the
    ones a logistics or manufacturing posting routes to, so their letters are where an invented
    claim would land - and it would surface in the interview, not in review."""
    banned = (r"\bcarriers?\b|freight|\bTMS\b|\btrucks?\b|\brail\b|\bocean\b|\bdocks?\b|"
              r"\blanes?\b|dispatch|OTIF|bill of lading|warehouse|inventory")
    for track in ("f", "g"):
        for tone in ("conservative", "tech"):
            for idx in range(6):
                letter = m.generate_cover_letter("Rivian", "Carrier Operations Analyst",
                                                 track, idx, "Normal, IL", tone)
                # The job TITLE legitimately contains "Carrier" - only the banked prose is on trial.
                prose = letter.replace("Carrier Operations Analyst", " ")
                hits = sorted({h.group(0) for h in re.finditer(banned, prose, re.I)})
                assert hits == [], f"track={track} tone={tone} idx={idx} claims freight work: {hits}"


def test_cover_letter_bank_uses_contractions():
    """The hand-written reference letter contracts ("I've made it a point"). An all-formal bank
    reads stiff and machine-written, which is the exact failure mode this copy exists to avoid.
    Assert the habit is present across the prose pools rather than checking any one sentence."""
    bank = m.load_cover_letter_templates()
    prose_keys = [k for k in bank
                  if k.startswith("track_") or k.startswith("bridges_") or k.startswith("closers_")]
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

    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: _fake_match())
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

    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: _fake_match(score=61))
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
    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: scored.append(job) or None)
    monkeypatch.setattr(m, "get_tracked_job_keys", lambda: set())
    # Seen before by /t, but never tracked - must NOT suppress.
    m.save_seen_job_db(m.generate_dedup_hash("Huntington", "FX Ops Analyst"))

    ok, message = m.ingest_manual_job(title="FX Ops Analyst", company="Huntington")

    assert scored, "a seen-but-untracked posting must still be scored"
    assert "Already in the pipeline" not in message


def test_ingest_manual_job_reports_an_ai_rejection_without_writing(monkeypatch):
    def _no_write(p, **kw):
        pytest.fail("no row without a score")

    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: None)
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
    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: _fake_match())
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
    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: _fake_match())
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
    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: _fake_match())
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


def test_aggregator_relist_matches_the_domain_not_the_employer():
    """The employer on an aggregator row is usually the REAL company, which is why the card looks
    legitimate right up until the link 404s. Matching has to be on the apply link."""
    # The exact URL that served a dead listing on 2026-09-22.
    assert m.is_aggregator_relist(
        "https://www.learn4good.com/jobs/farmington-hills/michigan/info_technology/5451227774/e/")
    assert m.is_aggregator_relist("https://jobs.learn4good.com/x")      # subdomain
    assert m.is_aggregator_relist("https://www.talent.com/view?id=9")
    # A board that hosts its OWN reqs must never match.
    for ok in ("https://boards.greenhouse.io/acme/jobs/123",
               "https://jobs.lever.co/acme/abc",
               "https://altarum.wd1.myworkdayjobs.com/x",
               "https://www.notlearn4good.com/jobs/x"):
        assert not m.is_aggregator_relist(ok), ok
    assert not m.is_aggregator_relist("") and not m.is_aggregator_relist(None)


def test_strict_filter_rejects_an_aggregator_relist_at_the_gate(monkeypatch):
    """A dead link wastes the AI screen, the card and the click. /decoys measured this class
    after the fact; the gate stops it before the screen runs."""
    monkeypatch.setattr(m, "is_company_on_cooldown", lambda company: False)
    monkeypatch.setattr(m, "get_applied_crm_companies", lambda: set())
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: {
        "min_salary": 50000,
        "valid_cities": ["farmington", "detroit"],
        "title_exclusions": [], "company_exclusions": [],
        "hard_ban_keywords": [], "seniority_exclusions": [],
    }.get(key, default if default is not None else []))

    job = {
        "employer_name": "Acme Corp", "job_title": "Operations Analyst",
        "job_description": "Reconciliation workflows with SQL and reporting.",
        "job_city": "Farmington Hills", "job_state": "MI",
        "job_salary": "$70,000", "job_min_salary": 70000, "job_max_salary": 90000,
        "job_apply_link": "https://www.learn4good.com/jobs/farmington-hills/michigan/x/1/e/",
    }
    trace = m.FunnelTrace()
    assert m.passes_strict_filter(job, trace=trace) is False
    assert trace.reasons.get("aggregator_link") == 1
    # The SAME job on a real ATS link is unaffected by this gate.
    trace2 = m.FunnelTrace()
    m.passes_strict_filter({**job, "job_apply_link": "https://boards.greenhouse.io/acme/jobs/1"},
                           trace=trace2)
    assert trace2.reasons.get("aggregator_link") is None


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


# ---- Inbound Gmail: the two real interview emails that were dropped ----
#
# Both reached Kevin's INBOX and both were discarded. Sender addresses and the substance of each
# message are the real ones; subject and snippet are reconstructed from what each email said,
# since Gmail's stored copy is not in this repo. They are the acceptance criteria for this change.

STEMLER_SENDER = "Andy Stemler <astemler@nextpathcp.com>"
STEMLER_SUBJECT = "Raymond James Interview - Monday 9/21 9:00am CST"
STEMLER_SNIPPET = (
    "Hi Kevin, confirming your interview with Raymond James for Monday 9/21 at 9:00am CST. "
    "Let me know if anything changes on your end. Thanks, Andy Stemler, NextPath Career Partners"
)

FITTERMAN_SENDER = "Chris.Fitterman-Harris@raymondjames.com"
FITTERMAN_SUBJECT = "Invitation: Interview - Kevin Miller @ Wed Sep 30, 2026 2pm - 3pm (EDT)"
FITTERMAN_SNIPPET = (
    "You have been invited to the following event. Interview with Raymond James. "
    "Join Zoom Meeting https://zoom.us/j/98765432100 Going? Yes - Maybe - No"
)

FITTERMAN_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "PRODID:-//Google Inc//Google Calendar 70.9054//EN\r\n"
    "VERSION:2.0\r\n"
    "METHOD:REQUEST\r\n"
    "BEGIN:VEVENT\r\n"
    "DTSTART;TZID=America/New_York:20260930T140000\r\n"
    "DTEND;TZID=America/New_York:20260930T150000\r\n"
    "SUMMARY:Interview - Kevin Miller\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def _b64url(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _gmail_message(msg_id, sender, subject, snippet, extra_headers=None, ics=None, age_seconds=7200):
    """One Gmail messages.get(format=full) response, shaped the way the real API returns it."""
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    for name, value in (extra_headers or {}).items():
        headers.append({"name": name, "value": value})
    payload = {"mimeType": "multipart/alternative", "headers": headers, "parts": [
        {"mimeType": "text/plain", "filename": "", "body": {"data": _b64url(snippet)}},
    ]}
    if ics:
        payload["mimeType"] = "multipart/mixed"
        payload["parts"].append(
            {"mimeType": "text/calendar", "filename": "invite.ics", "body": {"data": _b64url(ics)}})
    return {
        "id": msg_id, "threadId": f"thread-{msg_id}", "snippet": snippet, "payload": payload,
        "internalDate": str(int((time.time() - age_seconds) * 1000)),
    }


def _fake_tray_recorder(tray_state):
    """An in-memory stand-in for record_inbound_thread with the same upsert contract:
    returns (is_new_thread, message_count)."""
    def _record(thread_id, sender_email, sender_name, company, subject, snippet,
                status_label, match_reason, sheet_uuid, is_tier1):
        row = tray_state.get(thread_id)
        if row:
            row["message_count"] += 1
            row["state"] = "open"
            return False, row["message_count"]
        tray_state[thread_id] = {
            "thread_id": thread_id, "sender_email": sender_email, "sender_name": sender_name,
            "company": company, "subject": subject, "status_label": status_label,
            "match_reason": match_reason, "sheet_uuid": sheet_uuid,
            "is_tier1": 1 if is_tier1 else 0, "alerted": 0, "state": "open", "message_count": 1,
        }
        return True, 1
    return _record


def _run_poll_with_fake_gmail(monkeypatch, messages, crm_lookup=None, thread_started=False,
                              spam_messages=None, tray_state=None, delivery_fails=False,
                              shadow_messages=None, domain_match=None, real_tray=False):
    """Drive the real check_inbound_gmail_replies() against a faked Gmail API.

    `messages` answers the label:INBOX query, `spam_messages` the label:SPAM one and
    `shadow_messages` the shadow sweep's newer_than: query - the fake routes on the `q` param,
    because serving the same list to several passes is how a message gets processed twice and a
    test quietly asserts against the wrong one.

    shadow_messages defaults to EMPTY rather than to `messages`. In production both queries hit
    label:INBOX and overlap heavily, but a test that wants the shadow path asks for it: the poll
    tests assert on alerts, and silently feeding them a second pass over the same mail would mean
    every one of them was also exercising a path it never mentions.

    Returns (alerts, marked_read): the Telegram messages actually sent, and the ids whose UNREAD
    label was removed. Nothing here asserts on a return value - check_inbound_gmail_replies has
    none; what it does is send alerts and write CRM rows, so that is what gets captured.

    The inbound tray starts EMPTY for every test unless a case opts in with `tray_state`. Thread
    dedup is keyed on thread_id and the tray is durable by design, so a shared tray would make one
    test's conversation silence the next test's alert - which is real behaviour, but it belongs in
    the dedup tests that assert it deliberately, not as a hidden coupling between unrelated cases.
    """
    if tray_state is None:
        tray_state = {}
    # real_tray=True leaves the SQLite tray in place, for tests that read it back through
    # get_open_inbound_threads() - the same read /inbox does.
    if not real_tray:
        monkeypatch.setattr(m, "record_inbound_thread", _fake_tray_recorder(tray_state))
        monkeypatch.setattr(m, "mark_inbound_thread_alerted", lambda tid: tray_state.get(tid, {}).update(alerted=1))

    for var in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"):
        monkeypatch.setenv(var, "fake")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(m, "get_gmail_access_token", lambda: "fake-token")
    monkeypatch.setattr(m, "is_verified_crm_contact", crm_lookup or (lambda sender: None))
    monkeypatch.setattr(m, "match_unknown_sender_to_crm_company", domain_match or (lambda sender: None))
    monkeypatch.setattr(m, "is_thread_kevin_started", lambda tid, token: thread_started)

    inbox_by_id = {msg["id"]: msg for msg in messages}
    spam_by_id = {msg["id"]: msg for msg in (spam_messages or [])}
    shadow_by_id = {msg["id"]: msg for msg in (shadow_messages or [])}
    by_id = {**inbox_by_id, **spam_by_id, **shadow_by_id}
    alerts, marked_read = [], []

    class _Res:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return self._body

    def fake_get(url, **kwargs):
        if url.endswith("/messages"):
            query = (kwargs.get("params") or {}).get("q", "")
            if "label:SPAM" in query:
                listed = spam_by_id
            elif query.startswith("newer_than:"):
                listed = shadow_by_id
            else:
                listed = inbox_by_id
            return _Res({"messages": [{"id": i} for i in listed]})
        return _Res(by_id[url.rsplit("/", 1)[-1]])

    def fake_post(url, **kwargs):
        if url.endswith("/modify"):
            marked_read.append(url.split("/messages/")[1].split("/")[0])
        return _Res({})

    monkeypatch.setattr(m.requests, "get", fake_get)
    monkeypatch.setattr(m.requests, "post", fake_post)
    # The real send_telegram_message returns the sent message_id on success and None on failure -
    # it never raises. delivery_fails=True reproduces the failure return, which is what the
    # mark-read decision now hinges on.
    monkeypatch.setattr(m, "send_telegram_message",
                        lambda cid, text: (alerts.append(text),
                                           None if delivery_fails else 12345)[1])
    m.check_inbound_gmail_replies()
    return alerts, marked_read


def test_the_recruiter_email_kevin_missed_now_reaches_an_alert(monkeypatch):
    """astemler@nextpathcp.com, confirming a real Raymond James interview. It died three separate
    ways: 2h old against a 300s age gate, no required keyword in Gmail's snippet, and not a CRM
    contact. Each assertion below is one of those three, so a regression names its own cause."""
    two_hours_ago_ms = int((time.time() - 7200) * 1000)
    passed, reason = m.passes_email_prefilter(
        STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET, internal_date_ms=two_hours_ago_ms)
    assert passed, reason
    assert m.classify_inbound_ats_email(STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET)[0] == "INTERVIEW_SET"

    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("stemler", STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET, age_seconds=7200)])

    assert len(alerts) == 1
    assert "astemler@nextpathcp.com" in alerts[0]
    assert "Interview Signal Detected" in alerts[0]
    assert "No CRM changes were made" in alerts[0]  # stranger: alert yes, CRM writes no
    assert marked_read == ["stemler"]


def test_the_calendar_invite_kevin_missed_now_reaches_an_alert_with_its_date(monkeypatch):
    """Chris.Fitterman-Harris@raymondjames.com, a Google Calendar invitation for a real interview.
    The .ics is only visible at format=full, which is why the fetch changed."""
    is_invite, start = m.extract_calendar_invite(
        _gmail_message("f", FITTERMAN_SENDER, FITTERMAN_SUBJECT, FITTERMAN_SNIPPET, ics=FITTERMAN_ICS)["payload"])
    assert is_invite is True
    assert start == "Wed Sep 30, 2026 2:00 PM (America/New_York)"

    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("fitterman", FITTERMAN_SENDER, FITTERMAN_SUBJECT, FITTERMAN_SNIPPET,
                       ics=FITTERMAN_ICS, age_seconds=86400)])

    assert len(alerts) == 1
    assert "Calendar invite" in alerts[0]
    assert "Wed Sep 30, 2026 2:00 PM (America/New_York)" in alerts[0]
    assert "raymondjames.com" in alerts[0]


def test_tier1_interview_signal_survives_every_soft_filter(monkeypatch):
    """A day-old invite carrying an excluded keyword and no required keyword. Every soft rule says
    drop it; Tier 1 says an interview signal outranks all of them."""
    monkeypatch.setattr(m, "EMAIL_REQUIRED_KEYWORDS", "zzz-never-present")
    monkeypatch.setattr(m, "EMAIL_EXCLUDED_KEYWORDS", "interview")
    monkeypatch.setattr(m, "EMAIL_MAX_AGE_SECONDS", 300)
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("t1", FITTERMAN_SENDER, FITTERMAN_SUBJECT, FITTERMAN_SNIPPET,
                       ics=FITTERMAN_ICS, age_seconds=86400)])
    assert len(alerts) == 1


def test_tier1_still_obeys_the_sender_blacklist_and_blocked_domains(monkeypatch):
    """The bypass is about what the message says, never about who may send it. A robot mailbox
    blasting calendar invites is still a robot."""
    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("robot", "no-reply@calendar-spam.com", FITTERMAN_SUBJECT,
                       FITTERMAN_SNIPPET, ics=FITTERMAN_ICS),
        _gmail_message("blocked", "recruiter@quora.com", FITTERMAN_SUBJECT,
                       FITTERMAN_SNIPPET, ics=FITTERMAN_ICS),
    ])
    assert alerts == []
    assert sorted(marked_read) == ["blocked", "robot"]


def test_spam_sweep_rescues_a_calendar_invite_gmail_filed_as_spam(monkeypatch):
    """Kevin's recruiter warned outright that the Raymond James invite might land in Spam. A false
    positive there is unrecoverable in practice - nobody reads that folder."""
    alerts, marked_read = _run_poll_with_fake_gmail(
        monkeypatch, [],
        spam_messages=[_gmail_message("spam-invite", FITTERMAN_SENDER, FITTERMAN_SUBJECT,
                                      FITTERMAN_SNIPPET, ics=FITTERMAN_ICS)])
    assert len(alerts) == 1
    assert "Found in SPAM" in alerts[0]
    assert "Wed Sep 30, 2026 2:00 PM (America/New_York)" in alerts[0]
    assert "No CRM changes were made" in alerts[0]
    assert "#spam/" in alerts[0]  # links into Spam, not a nonexistent inbox thread
    # Marked read so the same invite does not re-alert every cycle - and nothing else.
    assert marked_read == ["spam-invite"]


def test_spam_sweep_surfaces_nothing_that_is_not_an_interview_signal(monkeypatch):
    """The whole point of the narrow query. Ordinary spam stays where Gmail put it, and is left
    completely untouched - not alerted, and not even marked read."""
    alerts, marked_read = _run_poll_with_fake_gmail(
        monkeypatch, [],
        spam_messages=[
            _gmail_message("spam-pills", "deals@pharma-spam.ru", "Cheap meds now",
                           "Order discount pharmaceuticals today with free worldwide shipping included."),
            _gmail_message("spam-human", "dana@atwell.com", "Re: Operations Analyst",
                           "Thanks for reaching out Kevin, let me look into it and get back to you."),
            _gmail_message("spam-bulk", "deals@leejeans.com", "40% off everything",
                           "Shop the fall sale now, free shipping on every order over fifty dollars.",
                           extra_headers={"List-Unsubscribe": "<https://leejeans.com/u/abc>"}),
        ])
    assert alerts == []
    assert marked_read == []


def test_spam_sweep_never_touches_the_crm(monkeypatch):
    """Structural, not incidental: sweep_spam_for_interview_signals calls no CRM function at all.
    A sender who IS a known contact still gets no writes when the message came from Spam."""
    for name in ("is_verified_crm_contact", "match_unknown_sender_to_crm_company",
                 "route_inbound_reply_to_crm", "record_application_outcome",
                 "log_metric_event", "enqueue_crm_payload"):
        monkeypatch.setattr(m, name, lambda *a, **k: pytest.fail(f"{name} reached from the Spam sweep"))
    alerts, _ = _run_poll_with_fake_gmail(
        monkeypatch, [],
        spam_messages=[_gmail_message("spam-invite", FITTERMAN_SENDER, FITTERMAN_SUBJECT,
                                      FITTERMAN_SNIPPET, ics=FITTERMAN_ICS)])
    assert len(alerts) == 1


def test_spam_sweep_still_obeys_the_sender_blacklist_and_blocked_domains(monkeypatch):
    """Spam is the one folder where a robot blasting calendar invites is actually likely."""
    alerts, marked_read = _run_poll_with_fake_gmail(
        monkeypatch, [],
        spam_messages=[
            _gmail_message("spam-robot", "no-reply@calendar-spam.com", FITTERMAN_SUBJECT,
                           FITTERMAN_SNIPPET, ics=FITTERMAN_ICS),
            _gmail_message("spam-blocked", "recruiter@quora.com", FITTERMAN_SUBJECT,
                           FITTERMAN_SNIPPET, ics=FITTERMAN_ICS),
        ])
    assert alerts == []
    assert marked_read == []  # blocked, so not even marked read


def test_spam_sweep_is_capped_per_cycle(monkeypatch):
    """Gmail lists newest first, so a fresh invite is at the top; the cap bounds the cost of a
    folder that is junk by definition. Raising it is a deliberate trade, not a free win."""
    assert m.SPAM_SWEEP_MAX_RESULTS == 10
    spam = [_gmail_message(f"s{i}", FITTERMAN_SENDER, FITTERMAN_SUBJECT, FITTERMAN_SNIPPET,
                           ics=FITTERMAN_ICS) for i in range(25)]
    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [], spam_messages=spam)
    # maxResults is sent to Gmail, but a server that ignores it must not cost 25 alerts - the
    # slice inside the sweep is what actually holds the line, so that is what this exercises.
    assert len(alerts) == 10
    assert len(marked_read) == 10


def test_spam_sweep_still_runs_when_the_inbox_query_fails(monkeypatch):
    """An INBOX list error must not take down the safety net for the mail most likely to be lost."""
    for var in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"):
        monkeypatch.setenv(var, "fake")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(m, "get_gmail_access_token", lambda: "fake-token")
    invite = _gmail_message("spam-invite", FITTERMAN_SENDER, FITTERMAN_SUBJECT,
                            FITTERMAN_SNIPPET, ics=FITTERMAN_ICS)
    alerts = []

    class _Res:
        def __init__(self, body, status=200):
            self.status_code = status
            self._body = body

        def json(self):
            return self._body

    def fake_get(url, **kwargs):
        if url.endswith("/messages"):
            if "label:SPAM" in (kwargs.get("params") or {}).get("q", ""):
                return _Res({"messages": [{"id": "spam-invite"}]})
            return _Res({}, status=500)  # INBOX list blows up
        return _Res(invite)

    monkeypatch.setattr(m.requests, "get", fake_get)
    monkeypatch.setattr(m.requests, "post", lambda url, **kw: _Res({}))
    monkeypatch.setattr(m, "send_telegram_message",
                        lambda cid, text: (alerts.append(text), 12345)[1])
    m.check_inbound_gmail_replies()
    # Two messages now: the rescued invite, plus the failure notice that the INBOX query broke.
    # The failure notice is the point - a silent INBOX outage is how Kevin finds out days later.
    rescued = [a for a in alerts if "Found in SPAM" in a]
    failures = [a for a in alerts if "Email poller failure" in a]
    assert len(rescued) == 1
    assert len(failures) == 1 and "INBOX list" in failures[0]


def test_bulk_mail_is_separated_from_humans_by_the_list_unsubscribe_header(monkeypatch):
    """The one rule that tells Andy Stemler apart from Lee Jeans. The newsletter below deliberately
    avoids the word 'unsubscribe' in its body, so only the header can catch it."""
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("bulk", "deals@leejeans.com", "40% off everything this weekend",
                       "Shop the fall sale now. Free shipping on orders over fifty dollars today.",
                       extra_headers={"List-Unsubscribe": "<https://leejeans.com/u/abc>"}),
        _gmail_message("human", "dana@atwell.com", "Re: Operations Analyst",
                       "Thanks for reaching out Kevin, let me look into it and get back to you."),
    ])
    assert len(alerts) == 1
    assert "dana@atwell.com" in alerts[0]


def test_job_board_blast_cannot_buy_a_tier1_bypass_with_the_word_interview(monkeypatch):
    """A real subject line from Kevin's inbox: "Application status update - YOUR INTERVIEW REQUEST
    AWAITING YOUR CONFIRMATION". It matches \\binterview\\b, so it scored Tier 1 and skipped the
    bulk gate, the age gate and the CRM whitelist - the exact path a genuine invite uses.

    Job-board volume makes this the highest-frequency false positive there is, and the bypass is
    the one route with no downstream filter behind it. List-Unsubscribe is what tells the two
    apart: the blast sets it, a recruiter typing by hand does not."""
    blast_subject = "Application status update - YOUR INTERVIEW REQUEST AWAITING YOUR CONFIRMATION"
    # The classifier still calls it an interview; the demotion is deliberately a poll-loop decision
    # so the Spam sweep and the CRM badge keep reading the same classifier.
    assert m.classify_inbound_ats_email("jobs@job-matches.example", blast_subject, "")[0] == "INTERVIEW_SET"

    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("blast", "alerts@job-matches.example", blast_subject,
                       "Your interview request is awaiting confirmation. View details and apply now.",
                       extra_headers={"List-Unsubscribe": "<https://job-matches.example/u/1>"}),
        _gmail_message("real", STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET),
    ])

    assert len(alerts) == 1, "the bulk blast must not alert"
    assert "astemler@nextpathcp.com" in alerts[0], "the real interview must still get through"
    assert sorted(marked_read) == ["blast", "real"]


def test_spam_sweep_does_not_resurrect_bulk_mail_gmail_correctly_caught(monkeypatch):
    """The sweep exists because Gmail is wrong in ONE direction - a real invite that looks bulk.
    A message that sets List-Unsubscribe is the case where Gmail was right, and rescuing it turns
    the safety net into a junk firehose aimed at Telegram."""
    alerts, marked_read = _run_poll_with_fake_gmail(
        monkeypatch, [],
        spam_messages=[
            _gmail_message("spam-blast", "alerts@job-matches.example",
                           "YOUR INTERVIEW REQUEST AWAITING CONFIRMATION",
                           "Confirm your interview request now.",
                           extra_headers={"List-Unsubscribe": "<https://x.example/u>"}),
            _gmail_message("spam-invite", FITTERMAN_SENDER, FITTERMAN_SUBJECT,
                           FITTERMAN_SNIPPET, ics=FITTERMAN_ICS),
        ])

    assert len(alerts) == 1
    assert "raymondjames.com" in alerts[0]
    # The blast is left entirely alone - not alerted, and not marked read.
    assert marked_read == ["spam-invite"]


def test_inbox_query_excludes_gmail_categories_before_spending_the_budget(monkeypatch):
    """The filter has to run on GMAIL'S side. Every Python gate rejects a message only after it has
    already consumed one of the per-cycle slots, so a burst of job-board mail can starve a real
    interview out of the window entirely. This asserts the exclusions reach the actual query."""
    captured = {}

    def _capture(monkeypatch_target, **kwargs):
        pass

    for var in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"):
        monkeypatch.setenv(var, "fake")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(m, "get_gmail_access_token", lambda: "fake-token")

    class _Res:
        status_code = 200

        def json(self):
            return {"messages": []}

    def fake_get(url, **kwargs):
        if url.endswith("/messages"):
            params = kwargs.get("params") or {}
            q = params.get("q", "")
            # Three queries now run per cycle: the alert path (is:unread), the spam sweep
            # (label:SPAM) and the shadow sweep (newer_than:). Keyed by which pass issued it,
            # because capturing "the last non-SPAM query" silently started asserting against
            # the shadow sweep the moment it was added.
            if "label:SPAM" in q:
                captured["spam_q"] = q
            elif q.startswith("newer_than:"):
                captured["shadow_q"] = q
                captured["shadow_maxResults"] = params.get("maxResults")
            else:
                captured["q"] = q
                captured["maxResults"] = params.get("maxResults")
        return _Res()

    monkeypatch.setattr(m.requests, "get", fake_get)
    monkeypatch.setattr(m.requests, "post", lambda url, **kw: _Res())
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, text: None)
    m.check_inbound_gmail_replies()

    assert "-category:promotions" in captured["q"]
    assert "-category:social" in captured["q"]
    assert "is:unread" in captured["q"] and "-from:me" in captured["q"]
    # 10 was far below one day's real inbound volume at any sane cadence.
    assert captured["maxResults"] == 50

    # The shadow sweep must carry the SAME exclusions but NOT is:unread - dropping that term is
    # its entire reason for existing, and keeping the exclusions is what stops it filling the
    # tray with promotions the alert path deliberately skips.
    assert "is:unread" not in captured["shadow_q"]
    assert "-from:me" in captured["shadow_q"]
    assert "-category:promotions" in captured["shadow_q"]


def test_a_dead_refresh_token_tells_kevin_instead_of_going_quiet(monkeypatch):
    """The worst failure mode in the system: auth dies, every inbound alert stops, and silence is
    indistinguishable from a quiet inbox. Kevin finds out from a missed interview."""
    for var in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER"):
        monkeypatch.setenv(var, "fake")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(m, "get_gmail_access_token", lambda: None)
    monkeypatch.setattr(m, "should_send_alert", lambda key, hours=6: True)
    alerts = []
    monkeypatch.setattr(m, "send_telegram_message",
                        lambda cid, text: (alerts.append(text), 12345)[1])

    m.check_inbound_gmail_replies()

    assert len(alerts) == 1
    assert "Email poller failure" in alerts[0]
    assert "Gmail auth" in alerts[0]


def test_poller_failure_alert_is_debounced_across_restarts(monkeypatch):
    """Render restarts on every deploy, so the cooldown must be the DB-backed one - an in-memory
    dict would reset with the container and re-alert on each boot."""
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "12345")
    seen = []
    monkeypatch.setattr(m, "should_send_alert", lambda key, hours=6: seen.append((key, hours)) or False)
    alerts = []
    monkeypatch.setattr(m, "send_telegram_message",
                        lambda cid, text: (alerts.append(text), 12345)[1])

    m.report_poller_failure("Gmail auth", "boom")

    assert alerts == []
    assert seen == [("poller_failure:Gmail auth", m.POLLER_FAILURE_ALERT_COOLDOWN_HOURS)]


def test_transactional_robot_mail_from_kevins_telegram_never_alerts(monkeypatch):
    """The three alerts filling Kevin's Telegram on 2026-09-19, by name.

    None were reachable by the other two defences, which is why they needed a third:
      - they set no List-Unsubscribe (transactional mail is not bulk mail), so the bulk gate misses
      - Gmail files them Updates, not Promotions, so -category: exclusions miss them
    noreply-location-sharing@google.com is the specific shape the old blacklist could not see: it
    contains "noreply-", never "noreply@", so the substring test returned no match."""
    for sender in ("service@paypal.com",
                   "noreply-location-sharing@google.com",
                   "notifications@linkedin.com",
                   "do-not-reply@indeed.com"):
        passed, reason = m.passes_email_sender_blocks(sender)
        assert not passed, f"{sender} should be blocked as a robot mailbox"
        assert "blacklisted" in reason

    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("paypal", '"service@paypal.com" <service@paypal.com>',
                       "You sent a $302.00 USD payment",
                       "Kevin Miller, here's your receipt. You sent $302.00 USD to Sandra Miller."),
        _gmail_message("gloc", "Google Location Sharing <noreply-location-sharing@google.com>",
                       "You're sharing your real-time location with Kevin Miller",
                       "Kevin, To protect your privacy, this is a reminder that you're sharing."),
        _gmail_message("human", "dana@atwell.com", "Re: Operations Analyst",
                       "Thanks for reaching out Kevin, let me look into it and get back to you."),
    ])

    assert len(alerts) == 1, "only the human should reach Telegram"
    assert "dana@atwell.com" in alerts[0]
    assert sorted(marked_read) == ["gloc", "human", "paypal"]


def test_a_real_person_at_a_robot_domain_still_gets_through(monkeypatch):
    """The blacklist matches the ADDRESS, never the domain. Blocking service@paypal.com must not
    block a recruiter who happens to work at PayPal - that is the whole reason these are substring
    entries on the local part rather than domain bans."""
    passed, _ = m.passes_email_sender_blocks("jane.recruiter@paypal.com")
    assert passed

    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("real", "Jane Recruiter <jane.recruiter@paypal.com>",
                       "Re: Operations Analyst role",
                       "Hi Kevin, thanks for applying - do you have time to chat this week?")])
    assert len(alerts) == 1
    assert "jane.recruiter@paypal.com" in alerts[0]


def test_a_known_contact_still_reaches_kevin_through_a_bulk_platform(monkeypatch):
    """Some firms route ALL outbound mail through Mailchimp/HubSpot, so a recruiter's hand-written
    note carries List-Unsubscribe. That was dropped at the bulk gate AND refused by the Spam sweep,
    which is total silent loss - the exact failure this system exists to prevent.

    The CRM whitelist is the discriminator: an exact address match means Kevin is already
    corresponding with this person."""
    known = lambda sender: {"name": "Sarah Chen", "company": "TalentFirm",
                            "tab": "Carmen Cold", "sheet_uuid": "uuid-1"}
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("mc", "Sarah Chen <sarah@talentfirm.com>", "Interview availability?",
                       "Hi Kevin, we'd love to set up an interview. Are you free Thursday?",
                       extra_headers={"List-Unsubscribe": "<https://mailchimp/u>"})],
        crm_lookup=known)
    assert len(alerts) == 1
    assert "Interview" in alerts[0]


def test_the_bulk_override_does_not_let_newsletters_in(monkeypatch):
    """The override keys on an exact CRM address match, so nothing Kevin does not already know
    gains anything from it."""
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("news", "deals@leejeans.com", "40% off everything",
                       "Shop the fall sale now, free shipping on orders over fifty dollars.",
                       extra_headers={"List-Unsubscribe": "<https://leejeans/u>"}),
    ])
    assert alerts == []


def test_the_classifier_reads_past_gmails_200_char_snippet(monkeypatch):
    """Gmail's snippet caps around 200 chars and cuts mid-sentence, so a recruiter who opens with
    pleasantries and puts the ask in paragraph three was classified on the pleasantries alone."""
    body = (
        "Hi Kevin,\n\n"
        "I hope you're having a great week so far. I wanted to circle back after reviewing your "
        "application and say how much the team enjoyed reading about your background in operations "
        "and analytics. It's a strong fit for what we've been looking for over the last few months.\n\n"
        "Are you free Thursday for an interview with the hiring manager?\n\n"
        "Best,\nSarah")
    short_snippet = body[:200]
    # The ask is past the cutoff, so the snippet alone cannot see it.
    assert m.classify_inbound_ats_email("s@firm.com", "Following up", short_snippet)[0] == "GENERAL"
    assert m.classify_inbound_ats_email("s@firm.com", "Following up", body)[0] == "INTERVIEW_SET"

    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("deep", "Sarah <sarah@firm.com>", "Following up", body)])
    assert len(alerts) == 1
    assert "Interview Signal Detected" in alerts[0]


def test_the_body_reader_ignores_quoted_thread_history():
    """A reply repeats the whole thread. Matching "interview" inside Kevin's OWN earlier message
    would turn every ordinary reply into a false interview signal."""
    payload = _gmail_message("q", "a@b.com", "Re: hello", "x")["payload"]
    payload["parts"][0]["body"]["data"] = _b64url(
        "Thanks Kevin, I'll take a look and get back to you.\n\n"
        "On Tue, Sep 15, 2026 at 3:09 PM Kevin Miller wrote:\n"
        "> Hi Sarah, following up about the interview we discussed and the offer timeline.\n")
    text = m.extract_plain_body(payload)
    assert "I'll take a look" in text
    assert "interview" not in text.lower(), "quoted history must be cut"
    assert m.classify_inbound_ats_email("a@b.com", "Re: hello", text)[0] == "GENERAL"


def test_the_body_reader_falls_back_to_html_and_skips_attachments():
    """Senders that ship HTML only still have to be readable, and a PDF's bytes are not body text -
    many application confirmations carry an attachment list at the bottom."""
    payload = {"mimeType": "multipart/mixed", "headers": [], "parts": [
        {"mimeType": "text/html", "filename": "",
         "body": {"data": _b64url("<html><body><p>Hi Kevin,</p><p>Are you free Thursday?</p>"
                                  "<style>p{color:red}</style></body></html>")}},
        {"mimeType": "application/pdf", "filename": "resume.pdf",
         "body": {"data": _b64url("%PDF-1.4 binary garbage interview offer")}},
    ]}
    text = m.extract_plain_body(payload)
    assert "Are you free Thursday?" in text
    assert "color:red" not in text, "style blocks must be stripped"
    assert "PDF-1.4" not in text, "attachment bytes must never reach the classifier"


def test_a_short_human_reply_is_no_longer_dropped(monkeypatch):
    """The shortest replies are often the warmest - a busy human writing back types one line."""
    assert m.EMAIL_MIN_BODY_LENGTH <= 20
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("short", "Dana <dana@atwell.com>", "Re: Operations Analyst",
                       "Hi Kevin, got a sec?")])
    assert len(alerts) == 1


def test_a_human_reply_using_a_bulk_sounding_word_still_alerts(monkeypatch):
    """EMAIL_EXCLUDED_KEYWORDS was a substring test over subject+snippet, so a real person writing
    'just a quick alert that the role is still open' was dropped on the word 'alert'. Bulk is
    decided structurally now - List-Unsubscribe and the sender blacklist - not by vocabulary."""
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("kw", "Dana <dana@atwell.com>", "Re: Operations Analyst",
                       "Hi Kevin, just a quick alert that the role is still open - free this week?")])
    assert len(alerts) == 1


def test_removing_the_keyword_list_does_not_let_bulk_back_in(monkeypatch):
    """The keyword list blocked no junk that the structural gates miss."""
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("news", "news@economist.com", "The World in Brief",
                       "Also: Resilient revelry at Oktoberfest and more stories from this week.",
                       extra_headers={"List-Unsubscribe": "<https://economist.com/u>"}),
        _gmail_message("board", "noreply@jobleads.com", "Your daily job digest",
                       "Here are 5 new jobs matching your saved search for today. Apply now."),
    ])
    assert alerts == []


def test_an_ats_robot_mailbox_can_still_deliver_a_real_interview():
    """Workday, Greenhouse and Criteria send REAL interview invites and scheduling links from
    noreply@ addresses. The blacklist is a substring test on the address and outranks the Tier 1
    bypass, so those were blocked and marked read before anything could look at them.

    Kevin's two real interviews came from named humans, which is why this never surfaced - it would
    have the moment an employer ran scheduling through their ATS."""
    for addr in ("noreply@myworkday.com", "no-reply@greenhouse.io",
                 "noreply@us.greenhouse-mail.io", "noreply@criteriacorp.com",
                 "noreply@hirevue.com"):
        passed, reason = m.passes_email_sender_blocks(addr)
        assert passed, f"{addr} is a real interview channel: {reason}"


def test_the_ats_carve_out_does_not_reopen_the_robot_mailbox_hole():
    """The carve-out keys on the DOMAIN, so it must not rescue a robot mailbox anywhere else -
    including at an employer's own domain, which is an application receipt, not an invitation."""
    for addr in ("service@paypal.com", "noreply-location-sharing@google.com",
                 "noreply@creditkarma.com", "noreply@jobleads.com",
                 "noreply@thyssenkrupp.com", "notifications@linkedin.com"):
        passed, _ = m.passes_email_sender_blocks(addr)
        assert not passed, f"{addr} must stay blocked"


def test_an_ats_interview_invite_reaches_telegram_end_to_end(monkeypatch):
    """The carve-out only declines to block - the message still has to earn its alert."""
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("wd", "Workday <noreply@myworkday.com>",
                       "Interview Confirmation - Operations Analyst",
                       "Your interview is scheduled for Thursday at 2pm. Please confirm.")])
    assert len(alerts) == 1
    assert "Interview" in alerts[0]


def _clear_tray():
    with m.get_db_conn() as conn:
        conn.execute("DELETE FROM inbound_threads")
        conn.commit()


def test_the_real_tray_upserts_on_thread_id_and_survives_a_restart():
    """Exercises the actual SQLite path, not the in-memory fake the poll tests use. The tray is the
    ledger the notification path never had, so it has to be durable: a Render deploy restarts the
    container, and an in-memory tray would come back empty with every conversation forgotten."""
    _clear_tray()
    args = ("T1", "dana@atwell.com", "Dana", "Atwell", "Re: Operations Analyst",
            "preview text", "GENERAL", "unknown sender", "", False)
    assert m.record_inbound_thread(*args) == (True, 1)
    assert m.record_inbound_thread(*args) == (False, 2)
    assert m.record_inbound_thread(*args) == (False, 3)

    open_threads = m.get_open_inbound_threads()
    assert len(open_threads) == 1
    assert open_threads[0]["message_count"] == 3

    # A fresh connection is what a restarted container gets.
    with m.get_db_conn() as conn:
        row = conn.execute(
            "SELECT message_count, state FROM inbound_threads WHERE thread_id = 'T1'").fetchone()
    assert row == (3, "open")
    _clear_tray()


def test_closing_a_thread_removes_it_from_the_tray_and_a_new_reply_reopens_it():
    """'done' has to mean 'dealt with for now', not 'ignore forever' - when they write back, the
    conversation is open again."""
    _clear_tray()
    args = ("T2", "dana@atwell.com", "Dana", "Atwell", "Re: Role", "x", "GENERAL", "", "", False)
    m.record_inbound_thread(*args)
    assert m.close_inbound_thread("T2") is True
    assert m.get_open_inbound_threads() == []
    # Closing twice is a no-op, so a duplicate /done does not report a false success.
    assert m.close_inbound_thread("T2") is False

    m.record_inbound_thread(*args)
    assert len(m.get_open_inbound_threads()) == 1, "a new reply reopens the conversation"
    _clear_tray()


def test_tier1_conversations_sort_above_everything_else_in_the_tray():
    """The tray is read top-down under time pressure; an offer must never sit below a newsletter
    reply just because the reply arrived later."""
    _clear_tray()
    m.record_inbound_thread("T-general", "a@b.com", "A", "B", "Re: hello", "x", "GENERAL", "", "", False)
    m.record_inbound_thread("T-offer", "c@d.com", "C", "D", "Offer", "x", "OFFER_EXTENDED", "", "", True)
    assert [t["thread_id"] for t in m.get_open_inbound_threads()][0] == "T-offer"
    _clear_tray()


def test_the_shadow_pass_never_resurrects_a_closed_thread_or_inflates_its_count():
    """The failure that makes a shadow sweep dangerous. It re-lists the SAME already-read message
    every cycle, so if it wrote through record_inbound_thread's upsert, every thread Kevin closed
    with /done would reopen an hour later and message_count would climb forever. INSERT OR IGNORE
    is what makes re-running it free, and this is the test that holds that line."""
    _clear_tray()
    args = ("T-shadow", "debdas@aaalife.com", "Debdas", "AAA Life", "Re: Annuity",
            "sent your resume", "GENERAL", "read before poll", "", False)

    assert m.record_shadow_thread(*args) is True, "first sight creates the row"
    assert m.record_shadow_thread(*args) is False, "the same message again writes nothing"
    assert m.record_shadow_thread(*args) is False

    with m.get_db_conn() as conn:
        count, state = conn.execute(
            "SELECT message_count, state FROM inbound_threads WHERE thread_id = 'T-shadow'"
        ).fetchone()
    assert count == 1, "re-listing the same message is not a new message"

    assert m.close_inbound_thread("T-shadow") is True
    m.record_shadow_thread(*args)
    assert m.get_open_inbound_threads() == [], "a closed thread stays closed through later sweeps"
    _clear_tray()


def test_the_shadow_pass_yields_to_the_alert_path_and_never_clears_its_alerted_flag():
    """Both writers target one table. The alert path owns the row - it is the one that actually
    notified Kevin - so the shadow pass must never overwrite what it recorded."""
    _clear_tray()
    m.record_inbound_thread("T-both", "dana@atwell.com", "Dana", "Atwell", "Real subject",
                            "x", "INTERVIEW_SET", "crm contact", "uuid-1", True)
    m.mark_inbound_thread_alerted("T-both")

    assert m.record_shadow_thread("T-both", "dana@atwell.com", "Dana", "Atwell", "Shadow subject",
                                  "y", "GENERAL", "read before poll", "", False) is False

    with m.get_db_conn() as conn:
        subject, status, alerted, tier1 = conn.execute(
            "SELECT subject, status_label, alerted, is_tier1 FROM inbound_threads "
            "WHERE thread_id = 'T-both'").fetchone()
    assert subject == "Real subject" and status == "INTERVIEW_SET"
    assert alerted == 1 and tier1 == 1
    _clear_tray()


def test_a_reply_read_before_the_poll_still_lands_in_the_tray_without_alerting(monkeypatch):
    """The Debdas case, end to end. His reply arrived at 3:29 and was read at 3:44 - inside one
    poll interval - so the is:unread query could never list it: no alert, no tray row, no trace
    anywhere that it had happened. The shadow pass records it. It must NOT alert (Kevin has
    already read it) and must NOT mark anything read (that would break the alert path's retry)."""
    _clear_tray()
    msg = _gmail_message("debdas", "Debdas Patnaik <dpatnaik@aaalife.com>",
                         "Annuity Processing Specialist @ AAA Life Insurance Company",
                         "I have sent your resume to the recruiter who handles the role.")
    # The alert path sees nothing (the message is read); only the shadow query returns it.
    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [], shadow_messages=[msg])

    assert alerts == [], "mail Kevin already read must never generate a Telegram alert"
    assert marked_read == [], "the shadow pass must not touch UNREAD"

    tray = m.find_inbound_threads_by_sender("dpatnaik@aaalife.com")
    assert len(tray) == 1, "the conversation is now visible to /inbox and /trace"
    assert tray[0]["match_reason"] == "read before poll"
    assert tray[0]["alerted"] == 0, "it was recorded, not announced"
    _clear_tray()


def test_trace_names_a_missing_reply_anchor_as_the_reason_a_contact_is_still_cold(monkeypatch):
    """What /trace exists to answer. A contact who wrote back but whose note carries no reply
    anchor is still on the COLD ladder and will be bumped - and before /trace, the only way to
    discover that was to read the sheet by hand."""
    monkeypatch.setattr(m, "is_verified_crm_contact",
                        lambda s: {"name": "Debdas", "company": "AAA Life",
                                   "tab": "Carmen Cold", "sheet_uuid": "uuid-9"})

    class _Res:
        status_code = 200

        def json(self):
            return {"status": "success", "followups": [
                {"sheet_uuid": "uuid-9", "note": "[2026-09-23] Cold email sent.",
                 "next_followup": "2026-09-25", "date_added": "2026-09-23"}]}

    monkeypatch.setattr(m, "crm_get", lambda params, **kw: _Res())
    report = m.format_trace_report("dpatnaik@aaalife.com")
    assert "No reply anchor" in report
    assert "COLD" in report

    class _Replied(_Res):
        def json(self):
            return {"status": "success", "followups": [
                {"sheet_uuid": "uuid-9",
                 "note": f"[2026-09-23] {m.INBOUND_REPLY_NOTE_MARKER} (they wrote to Kevin).",
                 "next_followup": "2026-09-27", "date_added": "2026-09-23"}]}

    monkeypatch.setattr(m, "crm_get", lambda params, **kw: _Replied())
    report = m.format_trace_report("dpatnaik@aaalife.com")
    assert "Reply anchor" in report and "ENGAGED" in report


def test_a_microsoft_bookings_link_reads_as_an_interview_signal():
    """Debdas's signature carried an outlook.office.com/bookwithme link under the words 'feel free
    to book as per your convenience' - which matched no pattern, because 'book a time' is not what
    anyone actually writes. The URL is the half that cannot be a newsletter."""
    label, _ = m.classify_inbound_ats_email(
        "Debdas Patnaik <dpatnaik@aaalife.com>", "Annuity Processing Specialist",
        "Feel free to book as per your convenience using the link below. "
        "https://outlook.office.com/bookwithme/user/abc123@aaalife.com?anonymous")
    assert label == "INTERVIEW_SET"

    # The guard that keeps it honest: a decline mentioning a booking page is still a decline,
    # because rejection is matched before every interview pattern.
    label, _ = m.classify_inbound_ats_email(
        "recruiter@corp.com", "Update",
        "Unfortunately we are not moving forward. https://outlook.office.com/bookwithme/user/x")
    assert label == "REJECTION"


def test_the_tray_message_renders_ids_and_an_empty_state():
    """The rendered card is what Kevin actually reads, and the id under each entry is the handle
    /done takes. A multi-entry card must carry its own ids - swipe recovery takes the first one on
    the card, which would close the wrong conversation."""
    assert "Nothing open" in m.format_inbound_tray_message([])
    rendered = m.format_inbound_tray_message([
        {"thread_id": "abc123", "sender_name": "Andy Stemler", "company": "NextPath",
         "subject": "Raymond James Interview", "status_label": "INTERVIEW_SET",
         "message_count": 2, "is_tier1": 1},
    ])
    assert "abc123" in rendered
    assert "Andy Stemler" in rendered
    assert "2 msgs" in rendered
    assert "/done" in rendered


def test_tray_helpers_never_raise_when_the_database_is_unavailable(monkeypatch):
    """The tray is an enhancement to the alert path. If SQLite is unreachable, the alert still has
    to go out - a broken ledger must not become a broken notification."""
    def _boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m, "get_db_conn", _boom)

    # Falls back to 'treat it as a new thread', which is exactly the pre-tray behaviour.
    assert m.record_inbound_thread("T", "a@b.com", "A", "B", "s", "x", "GENERAL", "", "", False) == (True, 1)
    assert m.get_open_inbound_threads() == []
    assert m.close_inbound_thread("T") is False
    m.mark_inbound_thread_alerted("T")  # must not raise


def test_a_failed_telegram_send_leaves_the_mail_unread_to_retry(monkeypatch):
    """The bug that made every other fix untrustworthy.

    send_telegram_message returns None on failure and never raises, and the mark-read POST used to
    run unconditionally straight after it. So a 5s timeout or a second 429 meant: the alert was
    never seen, the message was no longer unread, and the next poll's is:unread query could never
    find it again. A real interview confirmation could vanish with one ERROR line in a log.

    Leaving it UNREAD *is* the retry - no queue, no backoff, just the next cycle."""
    monkeypatch.setattr(m, "report_poller_failure", lambda *a, **k: None)
    attempted, marked_read = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("stemler", STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET)],
        delivery_fails=True)

    assert len(attempted) == 1, "the alert was attempted"
    assert marked_read == [], "but an undelivered alert must NOT be marked read"


def test_a_delivered_alert_is_marked_read_exactly_once(monkeypatch):
    """The other half: a successful send must still clear UNREAD, or every cycle re-alerts."""
    _alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("stemler", STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET)])
    assert marked_read == ["stemler"]


def test_a_failed_send_in_the_spam_sweep_also_retries(monkeypatch):
    """Worth its own test because Spam is the one folder nobody browses: an undelivered alert that
    was marked read leaves the invite unreachable by every path at once."""
    monkeypatch.setattr(m, "report_poller_failure", lambda *a, **k: None)
    attempted, marked_read = _run_poll_with_fake_gmail(
        monkeypatch, [],
        spam_messages=[_gmail_message("spam-invite", FITTERMAN_SENDER, FITTERMAN_SUBJECT,
                                      FITTERMAN_SNIPPET, ics=FITTERMAN_ICS)],
        delivery_fails=True)
    assert len(attempted) == 1
    assert marked_read == []


def test_a_second_reply_on_one_thread_updates_the_tray_without_a_new_alert(monkeypatch):
    """Three replies on one conversation used to cost three notifications. The conversation is the
    unit Kevin acts on, so the tray row is what gets updated."""
    tray = {}
    msgs = []
    for i in range(3):
        msg = _gmail_message(f"r{i}", "dana@atwell.com", "Re: Operations Analyst",
                             "Thanks Kevin, following up again with more detail on the role.")
        msg["threadId"] = "SAME-THREAD"
        msgs.append(msg)

    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, msgs, tray_state=tray)

    assert len(alerts) == 1, "one conversation, one notification"
    assert tray["SAME-THREAD"]["message_count"] == 3, "but all three messages are recorded"
    # Every message still gets marked read - the later ones simply did not warrant an interrupt.
    assert sorted(marked_read) == ["r0", "r1", "r2"]


def test_an_interview_on_an_existing_thread_still_interrupts(monkeypatch):
    """Tier 1 is exempt from dedup. A conversation that has been running for a week and NOW
    contains an interview invitation is exactly the development worth interrupting for."""
    tray = {}
    first = _gmail_message("m1", "dana@atwell.com", "Re: Operations Analyst",
                           "Thanks Kevin, let me take a look and get back to you shortly.")
    first["threadId"] = "T"
    alerts_1, _ = _run_poll_with_fake_gmail(monkeypatch, [first], tray_state=tray)

    second = _gmail_message("m2", "dana@atwell.com", "Re: Operations Analyst",
                            "Good news - are you free Thursday for an interview with the team?")
    second["threadId"] = "T"
    alerts_2, _ = _run_poll_with_fake_gmail(monkeypatch, [second], tray_state=tray)

    assert len(alerts_1) == 1
    assert len(alerts_2) == 1, "the interview must break through thread dedup"
    assert "Interview Signal Detected" in alerts_2[0]


def test_the_tray_records_strangers_the_crm_path_structurally_cannot_hold(monkeypatch):
    """The organizer gap. A recruiter's first email is a stranger by definition, so it resolves to
    no sheet row - and the whole CRM path is keyed on sheet_uuid. Before the tray it got one alert
    and then fell out of the system entirely."""
    tray = {}
    _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("stemler", STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET)],
        tray_state=tray)

    row = tray["thread-stemler"]
    assert row["sender_email"] == "astemler@nextpathcp.com"
    assert row["sheet_uuid"] == "", "a stranger has no CRM row, and the tray holds it anyway"
    assert row["status_label"] == "INTERVIEW_SET"
    assert row["state"] == "open"


def test_ordinary_human_mail_alerts_without_touching_the_crm(monkeypatch):
    """Tier 2. The old code dropped this silently for not being an exact CRM match - which is what
    every first contact from a stranger looks like."""
    monkeypatch.setattr(m, "route_inbound_reply_to_crm",
                        lambda *a, **k: pytest.fail("no CRM writes for an unresolved sender"))
    monkeypatch.setattr(m, "record_application_outcome",
                        lambda *a, **k: pytest.fail("no outcome rows for an unresolved sender"))
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("human", "dana@atwell.com", "Re: Operations Analyst",
                       "Thanks for reaching out Kevin, let me look into it and get back to you.")])
    assert len(alerts) == 1
    assert "Not a known contact - no CRM changes were made." in alerts[0]


def test_verified_contact_still_gets_its_crm_writes(monkeypatch):
    """The unresolved-sender path must not have cost a real contact its routing."""
    routed = []
    monkeypatch.setattr(m, "route_inbound_reply_to_crm", lambda *a, **k: routed.append(a))
    alerts, _ = _run_poll_with_fake_gmail(
        monkeypatch,
        [_gmail_message("known", "dana@atwell.com", "Re: Operations Analyst",
                        "Thanks for reaching out Kevin, let me look into it and get back to you.")],
        crm_lookup=lambda sender: {"name": "Dana", "company": "Atwell", "tab": "Carmen Cold",
                                   "sheet_uuid": "uuid-dana"})
    assert len(alerts) == 1
    assert "No CRM changes were made" not in alerts[0]
    assert len(routed) == 1


def test_alert_shows_a_readable_name_for_a_from_header_with_a_display_name(monkeypatch):
    """name_from_email_local_part() takes a bare address; a From: header is usually not one, and
    splitting "Andy Stemler <astemler@..." on the @ produced "Andy stemler <astemler" on the CRM
    Match line. It only ever showed on thread participants before; now it is on every stranger."""
    assert m.display_name_from_sender("Andy Stemler <astemler@nextpathcp.com>") == "Andy Stemler"
    assert m.display_name_from_sender("Chris.Fitterman-Harris@raymondjames.com") == "Chris Fitterman Harris"
    assert m.display_name_from_sender("") == "Unknown"

    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("named", STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET)])
    assert "<b>CRM Match:</b> Andy Stemler @ Unknown" in alerts[0]


def test_email_max_age_default_is_derived_from_the_poll_cadence(monkeypatch):
    """300s against a poller running every EMAIL_POLL_HOURS hours is the bug that lost two real
    interviews. The default now tracks the cadence, with a 96h floor.

    The floor matters because the derived value moves the WRONG WAY when the cadence is tightened:
    at EMAIL_POLL_HOURS=1 the derived window is 2h, so the floor is the only thing standing between
    a faster poll and a narrower catch-up window than the slow one had."""
    assert m.default_email_max_age_seconds(96) == 691200   # 2 cycles of headroom dominates
    assert m.default_email_max_age_seconds(24) == 345600   # daily cadence floors at 4 days
    assert m.default_email_max_age_seconds(1) == 345600    # hourly must NOT shrink the window
    assert m.default_email_max_age_seconds(0.25) == 345600
    # What the module actually loaded with no EMAIL_MAX_AGE_SECONDS set in the environment.
    assert m.EMAIL_MAX_AGE_SECONDS == m.default_email_max_age_seconds(m.EMAIL_POLL_HOURS)
    assert m.EMAIL_MAX_AGE_SECONDS >= 345600
    # Still overridable from Render, which is where every env value lives.
    monkeypatch.setenv("EMAIL_MAX_AGE_SECONDS", "600")
    assert int(os.environ["EMAIL_MAX_AGE_SECONDS"]) == 600


def test_a_weekend_reply_is_dropped_by_the_24h_ceiling(monkeypatch):
    """REVERSAL, deliberate. This used to assert the opposite: a Friday reply that is 62h old by
    Monday used to alert, protected by default_email_max_age_seconds()'s 96h floor.

    INBOUND_ALERT_MAX_AGE_SECONDS now sits ABOVE the Tier 1 bypass and drops it. Kevin chose this
    with the loss understood - every alert carries the message's own date, so what does arrive is
    findable, and a notification does not need to come through twice. Set
    INBOUND_ALERT_MAX_AGE_HOURS=96 to restore the old behavior."""
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("wk", "Dana <dana@atwell.com>", "Re: Operations Analyst",
                       "Hi Kevin, thanks for following up - I'd love to keep talking about this.",
                       age_seconds=62 * 3600)])
    assert alerts == []

    # ...and the same message inside the window still alerts, so the ceiling is the only reason.
    fresh, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("wk2", "Dana <dana@atwell.com>", "Re: Operations Analyst",
                       "Hi Kevin, thanks for following up - I'd love to keep talking about this.",
                       age_seconds=6 * 3600)])
    assert len(fresh) == 1


def test_stale_backlog_is_still_dropped(monkeypatch):
    """Widening the window is not removing it: a month-old unread message is still backlog."""
    passed, reason = m.passes_email_prefilter(
        "dana@atwell.com", "Re: Operations Analyst",
        "Thanks for reaching out Kevin, let me look into it and get back to you shortly.",
        internal_date_ms=int((time.time() - 30 * 86400) * 1000))
    assert passed is False
    assert "too old" in reason


def test_classifier_catches_the_bare_word_interview_and_meeting_mechanics():
    """Every interview pattern used to require a phrase, so two emails whose subject line literally
    read "Interview" both scored GENERAL."""
    for subject, snippet in (
        (STEMLER_SUBJECT, STEMLER_SNIPPET),
        (FITTERMAN_SUBJECT, FITTERMAN_SNIPPET),
        ("Next steps", "Please RSVP to the meeting request below."),
        ("Chat", "Join Zoom Meeting https://zoom.us/j/12345678901"),
        ("Sync", "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc"),
        ("Meeting invitation", "Invitation to a meeting with our hiring team."),
    ):
        assert m.classify_inbound_ats_email("x@co.com", subject, snippet)[0] == "INTERVIEW_SET", subject


def test_rejection_still_wins_over_the_new_bare_interview_pattern():
    """The bare \\binterview\\b pattern is only safe because rejection is matched first. A decline
    that mentions interviewing must not buy itself a Tier 1 bypass."""
    for subject, snippet in (
        ("Your application", "Unfortunately, we will not be moving forward to interview."),
        ("Interview update", "Unfortunately we have decided to pursue other candidates."),
        ("Re: Interview", "The position has been filled, but we will keep your resume on file."),
    ):
        assert m.classify_inbound_ats_email("x@co.com", subject, snippet)[0] == "REJECTION", snippet


# ---- OFFER_EXTENDED: the Signal Advisors thread that classified GENERAL ----

# Kevin's real offer thread (Signal Advisors, May 2026). Every message classified GENERAL, so an
# actual job offer alerted with no badge and looked like any other reply.
OFFER_SENDER = "Kristina Oberly <kristina@signaladvisors.com>"
OFFER_THREAD = (
    ("Internship Offer: Join us this summer at Signal Advisors!",
     "Dear Kevin, Congratulations! We are thrilled to extend an offer to you to join Signal "
     "Advisors as an Intern on our Wealth team. The team was impressed by your interviews."),
    ("Re: Internship Offer: Join us this summer at Signal Advisors!",
     "That's great news! I'll send over the official DocuSign shortly."),
    ("Re: Welcome to Signal Advisors",
     "Hi Kevin, We are so excited to officially welcome you to Signal Advisors! For your first "
     "day on Thursday, May 28th, you'll be in the office with us in Detroit! Please bring your "
     "I-9 documentation."),
)


def test_real_offer_thread_classifies_as_offer():
    """Acceptance criteria: the four messages of a real offer thread, all of which used to be
    GENERAL. The onboarding mail counts too - 'bring your I-9, arrive 9:30' is as time-critical
    as the offer itself and never repeats the word 'offer'."""
    for subject, snippet in OFFER_THREAD:
        assert m.classify_inbound_ats_email(OFFER_SENDER, subject, snippet)[0] == "OFFER_EXTENDED", subject


def test_product_welcome_mail_is_not_an_offer():
    """Regression: "Welcome to Jobright!" was announced as a possible OFFER. A bare
    "welcome (you )?to \\w+" pattern matched every SaaS signup on earth, and because OFFER grants a
    Tier 1 bypass it routed marketing mail around the bulk filter that would otherwise have caught
    it. Product onboarding and job onboarding share most of their vocabulary, so each offer phrase
    must carry something a marketing blast would never say."""
    for subject, snippet in (
        ("Welcome to Jobright!",
         "Welcome to Jobright. Eric Cheng, CEO. I started Jobright to give job seekers from all "
         "backgrounds the technology and tools to present your best self to employers."),
        ("Welcome to LinkedIn Premium", "Get started with your onboarding checklist today."),
        ("Welcome to Notion", "Welcome to Notion. Here is how to get started with your workspace."),
        ("Thanks for Applying to RevSpring Inc!",
         "Thank you for your interest in RevSpring Inc. We have received your application."),
    ):
        assert m.classify_inbound_ats_email("x@y.com", subject, snippet)[0] != "OFFER_EXTENDED", subject


def test_rejection_still_wins_over_the_offer_patterns():
    """OFFER is matched after REJECTION for the same reason INTERVIEW is: a decline routinely
    contains the word 'offer', and announcing one as an offer is the worst possible error."""
    for subject, snippet in (
        ("Update", "Unfortunately we regret that we cannot offer you the position at this time."),
        ("Your application", "Unfortunately the role was filled before we could extend an offer."),
    ):
        assert m.classify_inbound_ats_email("x@co.com", subject, snippet)[0] == "REJECTION", snippet


def test_offer_outranks_interview_when_both_appear():
    """An offer letter almost always recaps the interviews that produced it, so it matched
    INTERVIEW_SET first and was announced as an interview signal. Offer is the later stage."""
    label, _ = m.classify_inbound_ats_email(
        OFFER_SENDER,
        "Internship Offer",
        "We are thrilled to extend an offer. The team was impressed by your interviews.",
    )
    assert label == "OFFER_EXTENDED"


def test_interview_classification_is_unchanged_by_the_offer_tier():
    """The offer patterns sit between rejection and interview - neither neighbour may regress."""
    assert m.classify_inbound_ats_email(STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET)[0] == "INTERVIEW_SET"
    assert m.classify_inbound_ats_email("x@co.com", FITTERMAN_SUBJECT, FITTERMAN_SNIPPET)[0] == "INTERVIEW_SET"


# ---- /e and /eh: what actually lands in the Telegram chat ----

def _stage_draft_messages(monkeypatch, command_label="/e"):
    """Run the real stage_outreach_draft() and return every Telegram message it produced."""
    sent, documents = [], []
    before = set(threading.enumerate())
    monkeypatch.setattr(m, "compile_resume_pdf_resilient", lambda *a, **k: b"%PDF-resume")
    monkeypatch.setattr(m, "create_gmail_draft", lambda **kw: (True, "ok", "draft-123"))
    monkeypatch.setattr(m, "log_daily_activity", lambda *a, **k: None)
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, text: sent.append(text))
    monkeypatch.setattr(
        m, "send_telegram_document",
        lambda cid, b, fn, cap, label: documents.append(
            {"filename": fn, "caption": cap, "bytes": b}) or True)
    job = {"employer_name": "Atwell", "job_title": "Operations Analyst", "track": "e",
           "bullet_indices": [0, 1, 2], "tone_mode": "conservative"}
    mapping = {"sheet_uuid": "uuid-e", "sheet_tab": "Tetiana Cold",
               "contact_name": "", "contact_company": "Atwell"}
    draft_id = m.stage_outreach_draft(
        1, mapping, job, "Atwell", "Operations Analyst", False,
        "dana@atwell.com", "🎯 <b>Apollo Email Locked:</b>", command_label)
    # The resume PDF is dispatched on a daemon thread so the card lands first. Join only the
    # threads THIS call started - joining every daemon would block on main's own background
    # workers and add ~30s to the suite.
    for t in set(threading.enumerate()) - before:
        t.join(timeout=5)
    return sent, documents, draft_id


def test_e_posts_the_draft_and_the_resume_but_never_the_cover_letter(monkeypatch):
    """/e is for staging the outreach draft. It used to also push the cover letter text AND a
    compiled letter PDF into the chat on every single use, burying the tap-to-copy body it exists
    to produce. /letter still renders the same letter on demand.

    The RESUME does belong here: it is what gets uploaded to a portal right after /e runs, and it
    is the same bytes already attached to the draft."""
    sent, documents, draft_id = _stage_draft_messages(monkeypatch)

    assert len(sent) == 1
    assert "Tap-to-Copy Email Body" in sent[0]
    assert "Open Draft in Gmail" in sent[0]
    assert draft_id == "draft-123"
    # Neither the letter text nor a letter PDF may reach the chat from this path.
    assert not any("Cover Letter" in text for text in sent)
    assert not any("Cover_Letter" in d["filename"] for d in documents)
    # Exactly one document, and it is the resume.
    assert len(documents) == 1
    assert documents[0]["filename"] == m.resume_pdf_filename("Atwell")
    assert "Resume" in documents[0]["caption"]


def test_eh_shares_the_tail_so_it_posts_the_same_resume(monkeypatch):
    """/e and /eh differ only in how the address is resolved. A file appearing on one and not the
    other would be the two commands drifting apart, which the shared tail exists to prevent."""
    sent, documents, _ = _stage_draft_messages(monkeypatch, command_label="/eh")
    assert len(sent) == 1
    assert not any("Cover Letter" in text for text in sent)
    assert len(documents) == 1
    assert documents[0]["filename"] == m.resume_pdf_filename("Atwell")


def _run_stage_outreach_draft(monkeypatch):
    """Drive stage_outreach_draft() with everything outbound stubbed. Returns
    (create_gmail_draft kwargs, telegram document filenames)."""
    captured, documents = {}, []
    monkeypatch.setattr(m, "compile_resume_pdf_resilient", lambda *a, **k: b"%PDF-resume")
    monkeypatch.setattr(m, "log_daily_activity", lambda *a, **k: None)
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, text: None)
    monkeypatch.setattr(m, "send_telegram_document",
                        lambda cid, b, filename, caption=None, command_label=None, **k:
                        documents.append(filename) or True)

    def fake_draft(**kwargs):
        captured.update(kwargs)
        return True, "ok", "draft-123"

    monkeypatch.setattr(m, "create_gmail_draft", fake_draft)
    job = {"employer_name": "Atwell", "job_title": "Operations Analyst", "track": "e"}
    m.stage_outreach_draft(1, {"sheet_uuid": "u", "sheet_tab": "Tetiana Cold"}, job, "Atwell",
                           "Operations Analyst", False, "dana@atwell.com", "hdr", "/e")
    return captured, documents


def test_e_does_not_attach_the_resume_to_the_outbound_email(monkeypatch):
    """The resume is NOT attached to the draft by default. For a recruiter it duplicates the copy
    already in their ATS (Kevin applies before he emails); for a peer the screener rules say a
    discovery email carries no resume at all. Either way it spends sender trust - on a SPF
    SOFTFAIL domain - for nothing."""
    monkeypatch.setattr(m, "RESUME_ATTACH_TO_EMAIL", False)
    captured, _ = _run_stage_outreach_draft(monkeypatch)
    assert captured["pdf_bytes"] is None


def test_the_resume_still_reaches_telegram_when_the_email_has_none(monkeypatch):
    """The gate is on the OUTBOUND attachment only. The Telegram copy is the file Kevin uploads
    to the ATS portal right after drafting, so removing it would break the actual workflow."""
    monkeypatch.setattr(m, "RESUME_ATTACH_TO_EMAIL", False)
    _, documents = _run_stage_outreach_draft(monkeypatch)
    assert documents == [m.resume_pdf_filename("Atwell")]


def test_resume_attachment_can_be_switched_back_on(monkeypatch):
    """RESUME_ATTACH_TO_EMAIL=true restores it, for the genuine cold case: a recruiter at a firm
    where no application exists yet, where the resume is new information."""
    monkeypatch.setattr(m, "RESUME_ATTACH_TO_EMAIL", True)
    captured, _ = _run_stage_outreach_draft(monkeypatch)
    assert captured["pdf_bytes"] == b"%PDF-resume"
    assert captured["pdf_filename"] == m.resume_pdf_filename("Atwell")


def test_letter_command_still_renders_the_cover_letter(monkeypatch):
    """Removing the letter from /e must not have removed the way to get one."""
    sent, documents = [], []
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-letter", "sheet_tab": "Tetiana Cold",
        "contact_name": "", "contact_company": "Atwell"})
    monkeypatch.setattr(m, "get_job_by_sheet_uuid", lambda u: {
        "employer_name": "Atwell", "job_title": "Operations Analyst", "track": "e"})
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, text: sent.append(text))
    monkeypatch.setattr(m, "send_cover_letter_pdf_async",
                        lambda cid, letter, comp, track, label: documents.append(comp))

    _dispatch("/letter", reply_to_message={"message_id": 42, "text": "Operations Analyst"})

    assert len(sent) == 1
    assert "Cover Letter - Atwell" in sent[0]
    assert documents == ["Atwell"]


def test_calendar_invite_without_a_parseable_start_says_so_rather_than_guessing():
    """An alert with no time is useful. An alert with an invented time is something Kevin would
    plan around, so extract_calendar_invite returns None instead of a fallback."""
    no_dtstart = "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nSUMMARY:Interview\r\nEND:VEVENT\r\n"
    payload = _gmail_message("x", FITTERMAN_SENDER, "Invitation", "Interview", ics=no_dtstart)["payload"]
    assert m.extract_calendar_invite(payload) == (True, None)

    unparseable = "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nDTSTART:whenever-works\r\nEND:VCALENDAR\r\n"
    payload = _gmail_message("y", FITTERMAN_SENDER, "Invitation", "Interview", ics=unparseable)["payload"]
    assert m.extract_calendar_invite(payload) == (True, None)

    plain = _gmail_message("z", "dana@atwell.com", "Re: role", "Thanks, will look into it.")["payload"]
    assert m.extract_calendar_invite(plain) == (False, None)


def test_calendar_invite_detected_from_method_request_without_an_ics_part():
    """RFC 5546: METHOD:REQUEST is the invitation itself. Some senders inline it rather than
    attaching a .ics, and a REPLY or CANCEL is not a new invitation."""
    payload = {"mimeType": "multipart/mixed", "headers": [], "parts": [
        {"mimeType": "text/plain", "filename": "",
         "body": {"data": _b64url("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nDTSTART:20260930T180000Z\r\n")}}]}
    assert m.extract_calendar_invite(payload) == (True, None)  # start lives in the ics part only

    cancelled = {"mimeType": "text/plain", "headers": [], "parts": [
        {"mimeType": "text/plain", "filename": "",
         "body": {"data": _b64url("BEGIN:VCALENDAR\r\nMETHOD:CANCEL\r\n")}}]}
    assert m.extract_calendar_invite(cancelled) == (False, None)


def test_decoys_report_does_not_claim_a_decoy_rate_before_any_dead_mark(clean_outcomes):
    m.record_application_outcome("u-applied-only", "applied", source="greenhouse", posted_hours=12)
    msg = m.format_decoy_metrics_message()
    assert "no <code>/dead</code> marks yet" in msg
    assert "0 of 1" in msg


# ---- /public/stats (counts-only aggregate for the portfolio site) ----

def _funnel_response(overall, status="success"):
    class _Res:
        status_code = 200
        def json(self):
            return {"status": status, "overall": overall}
    return _Res()


_FUNNEL = {"Matched": 40, "Applied": 30, "Replied": 6, "Screening": 2,
           "Interviewing": 3, "Offer": 1, "Rejected": 9}


@pytest.fixture(autouse=True)
def _clear_public_stats_cache():
    m._public_stats_cache["payload"] = None
    m._public_stats_cache["fetched_at"] = 0.0
    yield
    m._public_stats_cache["payload"] = None
    m._public_stats_cache["fetched_at"] = 0.0


def test_public_stats_rolls_current_status_buckets_forward(monkeypatch):
    """A row in Interviewing was applied to and replied to, so every count includes the
    stages past it. Rejected counts as an application but never as a reply."""
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: _funnel_response(_FUNNEL))
    with m.app.test_client() as client:
        res = client.get("/public/stats")
    assert res.status_code == 200
    body = res.get_json()
    assert body["interviews"] == 6           # Screening + Interviewing + Offer
    assert body["replies"] == 12             # Replied + the 6 interviews
    assert body["applications_logged"] == 51  # Applied + replies + Rejected, never Matched
    assert body["rejections"] == 9
    assert body["still_sourcing"] == 40


def test_public_stats_exposes_counts_only_and_no_pii(monkeypatch):
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: _funnel_response(_FUNNEL))
    with m.app.test_client() as client:
        body = client.get("/public/stats").get_json()
    non_counts = {"status", "start_date", "as_of", "cached", "stale"}
    for key, value in body.items():
        if key in non_counts:
            continue
        assert isinstance(value, int), f"{key} is not an aggregate count: {value!r}"


def test_public_stats_returns_503_rather_than_zeros_when_the_crm_is_down(monkeypatch):
    """Zeros from a failed fetch look exactly like invented numbers on the page. The site
    must be able to tell the difference, so an outage is a 503, not a count of 0."""
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: None)
    with m.app.test_client() as client:
        res = client.get("/public/stats")
    assert res.status_code == 503
    assert res.get_json()["status"] == "unavailable"


def test_public_stats_serves_a_stale_cache_before_serving_nothing(monkeypatch):
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: _funnel_response(_FUNNEL))
    with m.app.test_client() as client:
        client.get("/public/stats")
        m._public_stats_cache["fetched_at"] = 0.0  # expire it
        monkeypatch.setattr(m, "crm_get", lambda *a, **k: None)
        res = client.get("/public/stats")
    assert res.status_code == 200
    body = res.get_json()
    assert body["stale"] is True
    assert body["applications_logged"] == 51


def test_public_stats_days_running_counts_from_the_first_commit():
    now = m.datetime(2026, 9, 19, tzinfo=m.timezone.utc)
    assert m._public_stats_days_running(now) == 50  # 2026-07-31 -> 2026-09-19
    assert m._public_stats_days_running(m.datetime(2026, 7, 30, tzinfo=m.timezone.utc)) == 0


# ==============================================================================
# CARMEN COLD -> JOB ROW CONTACT BACKFILL (/fillcontacts)
# ==============================================================================


class _FollowupResp:
    """crm_post's contract here is status_code + .json(), which _FakeResp does not carry."""
    status_code = 200

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return {"status": "success", "followups": self._rows}


def _fillcontacts_crm(cold, jobs):
    """crm_post stub serving a Carmen Cold roster and a Tetiana Warm job list."""
    def _post(payload, *a, **k):
        tab = payload.get("tab")
        return _FollowupResp(cold if tab == "CC" else (jobs if tab == "TW" else []))
    return _post


def test_fillcontacts_copies_the_real_person_onto_a_job_row(monkeypatch):
    """A job row carries whatever resolve_target_email() guessed, while Carmen Cold holds the
    human actually emailed at that company. Both are keyed by company, so the join exists."""
    cold = [{"name": "Eina Assali", "company": "Affirm", "email": "eina.assali@affirm.com"}]
    jobs = [{"sheet_uuid": "u1", "company": "Affirm", "title": "Analyst", "email": "kjmiller406@gmail.com"}]
    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))

    updates, _ = m.backfill_job_contacts_from_carmen_cold(dry_run=True)
    assert len(updates) == 1
    assert updates[0]["new_email"] == "eina.assali@affirm.com"
    assert updates[0]["old_email"] == "kjmiller406@gmail.com"


def test_fillcontacts_never_copies_kevins_own_address_onto_a_job(monkeypatch):
    """The Slate Auto row in the live sheet has kjmiller406@gmail.com as its "contact" - that is
    Kevin, not a lead. company_domain_of() rejects consumer mail, so it can never propagate."""
    cold = [{"name": "Kjmiller", "company": "Slate Auto", "email": "kjmiller406@gmail.com"}]
    jobs = [{"sheet_uuid": "u1", "company": "Slate Auto", "title": "Ops", "email": ""}]
    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))

    updates, _ = m.backfill_job_contacts_from_carmen_cold(dry_run=True)
    assert updates == []


def test_fillcontacts_keeps_similar_company_names_distinct(monkeypatch):
    """normalize_company_for_match() strips trailing legal suffixes only, so "Crain" and "Crain
    Communications" stay separate rows with separate people. Collapsing them would write one
    company's contact onto another company's job."""
    cold = [
        {"name": "Awarner", "company": "Crain", "email": "awarner@crain.com"},
        {"name": "Lvezzetti", "company": "Crain Communications", "email": "lvezzetti@crain.com"},
    ]
    jobs = [{"sheet_uuid": "u1", "company": "Crain Communications", "title": "Financial Analyst", "email": ""}]
    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))

    updates, _ = m.backfill_job_contacts_from_carmen_cold(dry_run=True)
    assert len(updates) == 1
    assert updates[0]["new_email"] == "lvezzetti@crain.com"


def test_fillcontacts_leaves_an_existing_real_contact_alone(monkeypatch):
    """An address that is already a real person at a real company is the best record there is."""
    cold = [
        {"name": "Awarner", "company": "Crain", "email": "awarner@crain.com"},
        {"name": "Lvezzetti", "company": "Crain", "email": "lvezzetti@crain.com"},
    ]
    jobs = [{"sheet_uuid": "u1", "company": "Crain", "title": "Billing Ops", "email": "awarner@crain.com"}]
    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))

    updates, _ = m.backfill_job_contacts_from_carmen_cold(dry_run=True)
    assert updates == []


def test_fillcontacts_replaces_a_role_mailbox_and_reports_alternates(monkeypatch):
    """operations@ carries no more information than the guess already in the cell, so it is
    overwritable. Where a company has several contacts the first wins and the rest are reported
    rather than silently dropped."""
    cold = [
        {"name": "Awarner", "company": "Crain", "email": "awarner@crain.com"},
        {"name": "Lvezzetti", "company": "Crain", "email": "lvezzetti@crain.com"},
    ]
    jobs = [{"sheet_uuid": "u1", "company": "Crain", "title": "Billing Ops", "email": "operations@crain.com"}]
    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))

    updates, skipped = m.backfill_job_contacts_from_carmen_cold(dry_run=True)
    assert len(updates) == 1
    assert updates[0]["new_email"] == "awarner@crain.com"
    assert updates[0]["alternates"] == ["lvezzetti@crain.com"]
    assert skipped and skipped[0]["company"] == "Crain"


def test_fillcontacts_picks_the_first_contacted_person_deterministically(monkeypatch):
    """Kevin messages 1-3 people per company, so ties are the normal case rather than the edge.
    get_followups re-sorts by next-followup date and every fresh Carmen Cold row carries the same
    first-rung interval, so same-day contacts arrive in no meaningful order. Sorting on date_added
    means the job row shows the first person contacted and keeps showing them across re-runs."""
    dana = {"name": "Dana", "company": "Acme Group", "email": "dana@acme.com", "date_added": "2026-09-10"}
    sam = {"name": "Sam", "company": "Acme Group", "email": "sam@acme.com", "date_added": "2026-09-14"}
    uma = {"name": "Uma", "company": "Acme Group", "email": "uma@acme.com", "date_added": "2026-09-19"}
    jobs = [{"sheet_uuid": "j1", "company": "Acme Group", "title": "Ops Analyst", "email": ""}]

    for order in ([dana, sam, uma], [uma, sam, dana], [sam, uma, dana]):
        monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(order, jobs))
        updates, _ = m.backfill_job_contacts_from_carmen_cold(dry_run=True)
        assert updates[0]["new_email"] == "dana@acme.com", order
        assert updates[0]["alternates"] == ["sam@acme.com", "uma@acme.com"]

    # A row missing Column A must never displace a real dated contact.
    ghost = {"name": "Ghost", "company": "Acme Group", "email": "ghost@acme.com", "date_added": ""}
    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm([ghost, sam], jobs))
    updates, _ = m.backfill_job_contacts_from_carmen_cold(dry_run=True)
    assert updates[0]["new_email"] == "sam@acme.com"


def test_fillcontacts_does_not_churn_as_more_people_are_messaged(monkeypatch):
    """Messaging a second and third person at a company must not rewrite the job row each time.
    The first real contact is the record; the rest stay in Carmen Cold."""
    jobs = [{"sheet_uuid": "j1", "company": "Acme Group", "title": "Ops Analyst", "email": ""}]
    cold = [{"name": "Dana", "company": "Acme Group", "email": "dana@acme.com", "date_added": "2026-09-10"}]

    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))
    updates, _ = m.backfill_job_contacts_from_carmen_cold(dry_run=True)
    assert updates[0]["new_email"] == "dana@acme.com"

    # Sheet now reflects that write; two more people get messaged at the same company.
    jobs[0]["email"] = "dana@acme.com"
    cold.append({"name": "Sam", "company": "Acme Group", "email": "sam@acme.com", "date_added": "2026-09-14"})
    cold.append({"name": "Uma", "company": "Acme Group", "email": "uma@acme.com", "date_added": "2026-09-19"})

    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))
    updates, _ = m.backfill_job_contacts_from_carmen_cold(dry_run=True)
    assert updates == []


def test_auto_fill_commits_and_notifies(monkeypatch):
    """The scheduled wrapper has no preview step, so it must actually write and must tell Kevin.
    A silent write to a hand-curated sheet is how a wrong address survives unnoticed."""
    cold = [{"name": "Eina Assali", "company": "Affirm", "email": "eina.assali@affirm.com"}]
    jobs = [{"sheet_uuid": "u1", "company": "Affirm", "title": "Analyst", "email": "kjmiller406@gmail.com"}]
    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))
    enqueued, sent = [], []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p))
    monkeypatch.setattr(m, "update_job_target_email", lambda *a, **k: True)
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, text, *a, **k: sent.append(text) or 1)

    assert m.auto_fill_job_contacts_from_carmen_cold() == 1
    assert any(p.get("action") == "update_contact_email" for p in enqueued)
    assert sent and "eina.assali@affirm.com" in sent[0]


def test_auto_fill_is_quiet_and_idempotent_when_nothing_matches(monkeypatch):
    """Once a row carries a real contact it no longer qualifies, so the next cycle is a no-op.
    A 24h job that re-notified every run would train Kevin to ignore the alert."""
    cold = [{"name": "Eina Assali", "company": "Affirm", "email": "eina.assali@affirm.com"}]
    jobs = [{"sheet_uuid": "u1", "company": "Affirm", "title": "Analyst", "email": "eina.assali@affirm.com"}]
    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))
    sent = []
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, text, *a, **k: sent.append(text) or 1)
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: None)

    assert m.auto_fill_job_contacts_from_carmen_cold() == 0
    assert sent == []


def test_poll_cycle_runs_the_carmen_cold_fill_after_sent_capture(monkeypatch):
    """Ordering is load-bearing: capture_contacts_from_sent_mail() files today's person INTO
    Carmen Cold, and the fill reads Carmen Cold. Reversed, a contact waits a full extra cycle."""
    order = []
    monkeypatch.setattr(m, "check_inbound_gmail_replies", lambda *a, **k: order.append("replies"))
    monkeypatch.setattr(m, "capture_contacts_from_sent_mail", lambda *a, **k: order.append("capture"))
    monkeypatch.setattr(m, "backfill_contact_emails_from_sent_mail", lambda *a, **k: order.append("sent_backfill"))
    monkeypatch.setattr(m, "auto_fill_job_contacts_from_carmen_cold", lambda *a, **k: order.append("cold_fill"))

    m.scheduled_email_poll_job()
    assert order.index("capture") < order.index("cold_fill")
    assert order[-1] == "cold_fill"


def test_poll_cycle_survives_a_failing_carmen_cold_fill(monkeypatch):
    """One broken step must not take the whole nightly cycle down with it."""
    monkeypatch.setattr(m, "check_inbound_gmail_replies", lambda *a, **k: None)
    monkeypatch.setattr(m, "capture_contacts_from_sent_mail", lambda *a, **k: None)
    monkeypatch.setattr(m, "backfill_contact_emails_from_sent_mail", lambda *a, **k: None)

    def _boom():
        raise RuntimeError("CRM down")
    monkeypatch.setattr(m, "auto_fill_job_contacts_from_carmen_cold", _boom)

    m.scheduled_email_poll_job()  # must not raise


def test_fillcontacts_dry_run_writes_nothing(monkeypatch):
    """The preview must not touch Sheets - it reports against tabs Kevin curates by hand."""
    cold = [{"name": "Eina Assali", "company": "Affirm", "email": "eina.assali@affirm.com"}]
    jobs = [{"sheet_uuid": "u1", "company": "Affirm", "title": "Analyst", "email": ""}]
    monkeypatch.setattr(m, "crm_post", _fillcontacts_crm(cold, jobs))
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p))
    monkeypatch.setattr(m, "update_job_target_email", lambda *a, **k: True)

    m.backfill_job_contacts_from_carmen_cold(dry_run=True)
    assert enqueued == []

    m.backfill_job_contacts_from_carmen_cold(dry_run=False)
    actions = [p.get("action") for p in enqueued]
    assert "update_contact_email" in actions
    assert "append_note" in actions


# ---- /cold, /warm, /quick contact quick-add parsing ----

def test_quick_add_rejects_a_pasted_email_address():
    """The real misfire: "/cold kjmiller406@gmail.com" parsed as name="kjmiller406",
    company="gmail.com" and silently created two junk Carmen Cold rows. These commands take a
    company NAME; an address belongs to /e, which drafts and logs in one step."""
    assert m.parse_quick_command("/cold kjmiller406@gmail.com") is None
    assert m.parse_quick_command("/cold dana@signaladvisors.com") is None
    assert m.parse_quick_command("/warm bob@crain.com") is None
    assert m.parse_quick_command("/quick x@ncms.org") is None


def test_quick_add_still_parses_real_company_names():
    """The guard keys on a bare-domain shape, so company names with digits, hyphens and multiple
    words - the cases the parser was written for - must be untouched."""
    assert m.parse_quick_command("/cold Dana Reed@Signal Advisors 7 ops lead") == (
        "Dana Reed", "Signal Advisors", 7, "ops lead")
    assert m.parse_quick_command("/cold Sam@3M") == ("Sam", "3M", 5, "")
    assert m.parse_quick_command("/cold Jo@Web3 Labs 8 note here") == ("Jo", "Web3 Labs", 8, "note here")
    assert m.parse_quick_command("/cold Ann@1Password") == ("Ann", "1Password", 5, "")
    assert m.parse_quick_command("/cold Lee@7-Eleven") == ("Lee", "7-Eleven", 5, "")
    assert m.parse_quick_command("/cold Kim@Ford Motor Company") == ("Kim", "Ford Motor Company", 5, "")


# ---- Tier 1 interview-bypass false positives (real mail off Kevin's phone, 2026-09-20) ----

# Each of these arrived as "🎉 Interview Signal Detected!" on Kevin's phone in one morning. They
# are kept verbatim as the regression set: the next greedy pattern has to get past all of them.
_REAL_TIER1_FALSE_POSITIVES = [
    ("welcome@notify.chime.com", "Get paid up to 2 days early? Learn how with Chime.",
     "fee-free overdraft, early payday, set up qualifying direct deposit"),
    ("no-reply@usa.experian.com", "Kevin, your account is set up!",
     "Now it is time to take your credit to the next level. Sign in to explore new features."),
    ("support@turbotax.intuit.com", "TurboTax: A dedicated expert who will handle your taxes",
     "Schedule a call, an expert will handle the rest."),
    ("azure@promomail.microsoft.com", "Set up advanced security for your new Azure SQL database",
     "Learn how to protect your data using built-in tools in this tutorial"),
    ("noreply@send.calendly.com", "Updates to our Terms of Use",
     "Learn about updates to our Terms of Use, effective March 8."),
    ("recruiter@bloomberg.com", "Leo from Bloomberg just sent you a message on WayUp",
     "Last chance to RSVP and meet the team at Bloomberg"),
    ("support@urbansitter.com", "Welcome to UrbanSitter!",
     "Hi Kevin, I am part of the UrbanSitter Support Team"),
]

# The other half of the trade. Tightening patterns is only safe if these still clear Tier 1 -
# a lost interview is far more expensive than a spam alert.
_REAL_INTERVIEW_SIGNALS = [
    ("careers@plantemoran.com", "Interview Request - Trust Operations Analyst",
     "We would like to invite you to an interview next week."),
    ("dana.reed@signaladvisors.com", "Re: Operations Analyst",
     "Happy to chat! Do you have 30 minutes Thursday?"),
    ("recruiting@affirm.com", "Next steps with Affirm",
     "We would love to schedule a call with you about the role."),
    ("sarah@huntington.com", "Following up", "Can we set something up for Tuesday?"),
    ("talent@crain.com", "Your application", "You have been selected for an interview."),
    ("hr@ford.com", "Interview", "Please RSVP for your interview slot on Monday."),
    ("j.smith@oppenheimer.com", "quick question",
     "Let us grab 15 minutes this week - here is my calendly"),
]


def _reaches_tier1(sender, subject, body):
    """The real bypass decision: classifier says interview/offer AND the sender is not automated.
    Mirrors the two checks in check_inbound_gmail_replies()."""
    label, _ = m.classify_inbound_ats_email(sender, subject, body)
    if label not in ("INTERVIEW_SET", "OFFER_EXTENDED"):
        return False
    return not m.is_automated_sender(sender)


@pytest.mark.parametrize("sender,subject,body", _REAL_TIER1_FALSE_POSITIVES)
def test_marketing_mail_never_reaches_tier1(sender, subject, body):
    assert _reaches_tier1(sender, subject, body) is False


@pytest.mark.parametrize("sender,subject,body", _REAL_INTERVIEW_SIGNALS)
def test_real_interview_signals_still_reach_tier1(sender, subject, body):
    assert _reaches_tier1(sender, subject, body) is True


def test_hiring_role_mailboxes_are_not_treated_as_automated():
    """The whole reason is_automated_sender() has its own list instead of reusing
    _ROLE_MAILBOX_LOCALPARTS: careers@/recruiting@/talent@/hr@ are where real invitations come
    from, and denying them would recreate the silent loss the bypass exists to prevent."""
    for addr in ("careers@plantemoran.com", "recruiting@affirm.com",
                 "talent@crain.com", "hr@ford.com", "jobs@huntington.com"):
        assert m.is_automated_sender(addr) is False, addr


def test_automated_senders_are_detected_by_localpart_and_subdomain():
    for addr in ("no-reply@usa.experian.com", "welcome@notify.chime.com",
                 "notifications@x.com", "azure@promomail.microsoft.com",
                 "noreply@send.calendly.com", "Chime <welcome@notify.chime.com>"):
        assert m.is_automated_sender(addr) is True, addr
    # A human at a normal domain is never automated.
    for addr in ("dana.reed@signaladvisors.com", "j.smith@oppenheimer.com", "sarah@huntington.com"):
        assert m.is_automated_sender(addr) is False, addr


# ---- Hard age ceiling on inbound alerts ----

def test_age_ceiling_is_24h_and_outranks_the_prefilter_window():
    """Telegram stores every alert, so re-sending old mail cannot recover anything - it is either
    already on Kevin's phone or was deliberately skipped. This ceiling is unconditional; the
    prefilter's own EMAIL_MAX_AGE_SECONDS sits behind the Tier 1 bypass and is much wider."""
    assert m.INBOUND_ALERT_MAX_AGE_SECONDS == 24 * 3600
    assert m.INBOUND_ALERT_MAX_AGE_SECONDS < m.EMAIL_MAX_AGE_SECONDS


def test_age_ceiling_never_drops_below_two_poll_intervals(monkeypatch):
    """The 24h ceiling is only safe because the poller runs hourly. The max() guard means raising
    EMAIL_POLL_HOURS cannot silently create a window narrower than the cadence - the bug
    default_email_max_age_seconds() was written to document."""
    for poll_hours, floor in ((1.0, 24 * 3600), (24.0, 48 * 3600), (48.0, 96 * 3600)):
        computed = max(int(24 * 3600), int(poll_hours * 3600 * 2))
        assert computed >= floor
        assert computed >= poll_hours * 3600 * 2


# ---- JD vocabulary tracking: /gaps and /bullets ----

_SURETY_JD_M = """Hybrid Operations Analytics Associate - Surety. Reconcile bordereaux and premium
bookings, build reporting in Power BI, maintain the policy administration system, partner with
brokers and drive process improvement across the surety portfolio."""


def test_record_jd_terms_banks_vocabulary(monkeypatch):
    assert m.record_jd_terms(_SURETY_JD_M, 98) > 0
    with m.get_db_conn() as conn:
        row = conn.execute(
            "SELECT docs, fit_sum, hi_fit_docs FROM jd_term_yield WHERE term = 'surety'"
        ).fetchone()
    assert row == (1, 98, 1), "a 98-scoring JD must bank as one hi-fit doc"


def test_record_jd_terms_accumulates_across_postings():
    m.record_jd_terms(_SURETY_JD_M, 90)
    m.record_jd_terms(_SURETY_JD_M, 70)
    with m.get_db_conn() as conn:
        docs, fit_sum, hi = conn.execute(
            "SELECT docs, fit_sum, hi_fit_docs FROM jd_term_yield WHERE term = 'surety'"
        ).fetchone()
    assert (docs, fit_sum) == (2, 160)
    assert hi == 1, "only the 90 clears HI_FIT_THRESHOLD, not the 70"


def test_record_jd_terms_never_raises_on_bad_input():
    """Telemetry must never cost Kevin a card."""
    assert m.record_jd_terms(None, 50) == 0
    assert m.record_jd_terms("", None) == 0


def test_gaps_surface_market_terms_the_resume_lacks(monkeypatch):
    """The Skills 0% case: the JD says bordereaux, the bank says reconciliation."""
    monkeypatch.setattr(m, "get_filter", lambda k, d=None: ["reconciliation"] if k == "core_skills" else d)
    monkeypatch.setattr(m, "load_resume_bullet_tracks", lambda: {})
    monkeypatch.setattr(m, "get_resume_vocabulary", lambda: {"reconciliation"})
    for _ in range(3):
        m.record_jd_terms(_SURETY_JD_M, 95)
    terms = [g["term"] for g in m.get_jd_term_gaps(limit=40)]
    assert "surety" in terms
    assert "bordereaux" in terms


def test_gaps_exclude_terms_already_in_resume_copy(monkeypatch):
    monkeypatch.setattr(m, "get_resume_vocabulary", lambda: {"surety", "bordereaux"})
    for _ in range(3):
        m.record_jd_terms(_SURETY_JD_M, 95)
    terms = [g["term"] for g in m.get_jd_term_gaps(limit=40)]
    assert "surety" not in terms and "bordereaux" not in terms


def test_gaps_ignore_one_off_vocabulary(monkeypatch):
    """min_docs guards against rewriting a resume around a single weird listing."""
    monkeypatch.setattr(m, "get_resume_vocabulary", lambda: set())
    m.record_jd_terms("Unicorn wrangling and dragon taming specialist.", 99)
    terms = [g["term"] for g in m.get_jd_term_gaps(limit=40)]
    assert "unicorn" not in terms


def test_gaps_rank_high_fit_terms_first(monkeypatch):
    monkeypatch.setattr(m, "get_resume_vocabulary", lambda: set())
    for _ in range(2):
        m.record_jd_terms("Surety underwriting operations.", 95)
    for _ in range(2):
        m.record_jd_terms("Switchboard greeting duties.", 20)
    gaps = m.get_jd_term_gaps(limit=40)
    assert gaps[0]["hi_fit_docs"] >= gaps[-1]["hi_fit_docs"]
    top = [g["term"] for g in gaps[:6]]
    assert "surety" in top, "high-fit vocabulary must outrank low-fit vocabulary"


def test_resolve_bullet_track_key_accepts_shorthand():
    tracks = {"track_a_wealth_ops": [], "track_e_bizops": []}
    assert m.resolve_bullet_track_key("e", tracks) == "track_e_bizops"
    assert m.resolve_bullet_track_key("bizops", tracks) == "track_e_bizops"
    assert m.resolve_bullet_track_key("track_a_wealth_ops", tracks) == "track_a_wealth_ops"
    assert m.resolve_bullet_track_key("zzz", tracks) is None


def test_draft_bullets_passes_existing_bullets_as_ground_truth(monkeypatch):
    """The anti-fabrication gate: the real bullets must reach the prompt, and the system prompt
    must forbid inventing experience."""
    seen = {}

    def _fake(prompt, system_prompt=None, **kw):
        seen["prompt"] = prompt
        seen["system"] = system_prompt
        return json.dumps({"bullets": [{"bullet": "Reconciled custodial ledgers daily.",
                                        "covers": ["reconciliation"], "based_on": "x"}]})

    monkeypatch.setattr(m, "call_gemini_api", _fake)
    out = m.draft_bullets_for_gaps("track_a", ["Wrote nightly reconciliation scripts."],
                                   [{"term": "bordereaux"}])
    assert out and out[0]["bullet"].startswith("Reconciled")
    assert "Wrote nightly reconciliation scripts." in seen["prompt"]
    assert "forbidden from inventing" in seen["system"]


def test_draft_bullets_returns_nothing_without_ground_truth(monkeypatch):
    """No existing bullets means nothing to rephrase - it must NOT free-write a history."""
    monkeypatch.setattr(m, "call_gemini_api", lambda *a, **k: pytest.fail("must not call Gemini"))
    assert m.draft_bullets_for_gaps("track_a", [], [{"term": "surety"}]) == []


def test_draft_bullets_survives_garbage_model_output(monkeypatch):
    monkeypatch.setattr(m, "call_gemini_api", lambda *a, **k: "not json at all")
    assert m.draft_bullets_for_gaps("track_a", ["Did a thing."], [{"term": "surety"}]) == []


# ---- Orphaned sheet_uuid: a card must never point at a row that was never written ----

def _batch_resp(count, dispositions=None, msg="Batch inserted rows"):
    body = {"status": "success", "message": msg, "count": count}
    if dispositions is not None:
        body["dispositions"] = dispositions

    class _R:
        status_code = 200
        def json(self):
            return body
    return _R()


def _dispatch_env(monkeypatch, resp):
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "crm_post", lambda payload, **kw: resp)
    monkeypatch.setattr(m, "send_health_alert", lambda msg: None)
    monkeypatch.setattr(m.time, "sleep", lambda *a, **k: None)
    cards = []
    monkeypatch.setattr(m, "send_telegram_card",
                        lambda *a, **kw: cards.append(kw.get("sheet_uuid")))
    return cards


def _tier1_match(company, title, short_id, sent_uuid, score=90):
    return {
        "job": {"employer_name": company, "job_title": title, "job_apply_link": ""},
        "score": score, "reason": "r", "target_email": "a@b.c", "age_badge": "",
        "salary_str": "", "work_style": "", "overlap_pct": 0,
        "short_id": short_id, "sheet_uuid": sent_uuid, "is_clavicular": False,
    }


def test_a_guessed_email_is_blank_in_the_sheet_but_still_on_the_card(monkeypatch):
    """resolve_target_email() invents operations@<company>.com so the CARD has a recipient. That
    guess filled the Contact Email column with addresses nobody verified - indistinguishable from
    a confirmed one and noise to sort by."""
    payloads, cards = [], []
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "crm_post", lambda payload, **kw: payloads.append(payload) or _batch_resp(2))
    monkeypatch.setattr(m, "send_health_alert", lambda msg: None)
    monkeypatch.setattr(m.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(m, "send_telegram_card", lambda *a, **kw: cards.append(a[2]))

    tagged = _tier1_match("MAHLE", "Global Trade Data & BI Intern", "s1", "U-1")
    tagged["target_email"] = "operations@mahle.com [⚠️ Fallback Email]"
    untagged = _tier1_match("NBHS", "Business Ops", "s2", "U-2")
    untagged["target_email"] = "bizops@nbhs.com"      # real domain, invented mailbox, NO tag
    real = _tier1_match("Altarum", "BTA", "s3", "U-3")
    real["target_email"] = "kara.wise@altarum.org"

    m.dispatch_tier1_matches([tagged, untagged, real])

    rows = [r["row_data"] for p in payloads for r in p.get("rows", [])]
    emails = {r[1]: r[3] for r in rows}          # company -> Contact Email cell
    assert emails["MAHLE"] == "", "a tagged fallback must not reach the sheet"
    assert emails["NBHS"] == "", "an untagged role-mailbox guess must not reach the sheet either"
    assert emails["Altarum"] == "kara.wise@altarum.org", "a real address must survive"
    # The card still drafts to the guess - only the SHEET cell is blanked.
    assert "operations@mahle.com [⚠️ Fallback Email]" in cards


@pytest.mark.parametrize("addr", [
    "operations@kuehne-nagel.com",
    "operations@maximus.com",
    "operations@universallogistics.com",
    "operations@advantageagentservices.com",
    # The waterfall's placeholder-name output - a REAL domain, so the fallback tag never applies
    # and the role-mailbox list never matched it either. This is what /eh wrote into Tetiana Warm.
    "operations.lead@kuehne-nagel.com",
    "operations.lead@maximus.com [⚠️ Unverified]",
    "hiring.manager@maximus.com",
])
def test_no_generic_address_may_ever_reach_the_contact_email_column(addr):
    """Every one of these was sitting in Tetiana Warm's Contact Email column. None is a person.

    The old predicate only caught a bare role mailbox or the literal "[⚠️ Fallback Email]" tag,
    so an address the waterfall PATTERNED from the placeholder name "Operations Lead" - at the
    employer's real domain - passed both tests and was written as though it were confirmed.
    """
    assert m.is_guessed_contact_email(addr) is True, addr


@pytest.mark.parametrize("addr", [
    "msalk@inveniam.io", "jeffrey.cooley@cvshealth.com", "dpatnaik@aaalife.com",
    "craig.radomski@siemens.com", "kara.wise@altarum.org", "awarner@crain.com",
])
def test_a_real_person_is_never_mistaken_for_a_guess(addr):
    """The other half of the line: widening the guess test must not start blanking addresses
    Kevin actually confirmed, which would silently erase real contacts from the sheet."""
    assert m.is_guessed_contact_email(addr) is False, addr


@pytest.mark.parametrize("company,site,expected", [
    ("Computacenter", "https://computacenter.com", "operations@computacenter.com"),
    ("Raymond James", "https://raymondjames.com", "operations@raymondjames.com"),
    # _e_env's job title has no "wealth" keyword, so this resolves to operations@ rather than the
    # wealthops@ the real Waldron card produced. The mailbox prefix is not what is under test -
    # that a role mailbox at a REAL domain is refused, is.
    ("Waldron Private Wealth", "https://waldronprivatewealth.com",
     "operations@waldronprivatewealth.com"),
])
def test_the_exact_addresses_that_reached_the_sheet_are_now_blocked(monkeypatch, company, site,
                                                                    expected):
    """Each of these was written into a real CRM row by bare /e. All three are role mailboxes at
    the employer's genuine domain - untagged, so the old is_unverified_email() gate passed them."""
    saved = _e_env(monkeypatch, employer_website=site, company=company)

    _dispatch("/e", reply_to_message={"text": "card"})

    assert saved["drafted_to"] == expected, "the draft is still produced"
    assert saved["local"] == [] and saved["crm"] == [], f"{expected} must never be stored"


def test_eh_does_not_write_a_patterned_guess_to_the_sheet(monkeypatch):
    """/eh used to persist unconditionally - the reasoning being that spending provider credits
    made the result evidence. But when no provider answers, the waterfall falls back to a pattern
    built from the placeholder name, which is a guess with extra steps."""
    payloads = []
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, cid, label: {
        "sheet_uuid": "uuid-eh", "sheet_tab": "Tetiana Cold",
        "contact_name": "", "contact_company": "Kuehne+Nagel"})
    monkeypatch.setattr(m, "get_job_by_sheet_uuid", lambda u: {
        "job_title": "Sea Logistics Revenue Specialist 1", "employer_name": "Kuehne+Nagel",
        "employer_website": "https://kuehne-nagel.com", "job_id": "x"})
    monkeypatch.setattr(m, "rebuild_job_from_card", lambda job, txt: (job, False))
    monkeypatch.setattr(m, "_job_data_available", lambda job, mapping: True)
    monkeypatch.setattr(m, "resolve_email_waterfall",
                        lambda *a, **k: "operations.lead@kuehne-nagel.com [⚠️ Unverified]")
    monkeypatch.setattr(m, "log_email_enrichment_attempt", lambda *a, **k: None)
    monkeypatch.setattr(m, "update_job_target_email", lambda u, e: None)
    monkeypatch.setattr(m, "log_addressed_contact_to_carmen_cold", lambda *a, **k: None)
    monkeypatch.setattr(m, "stage_outreach_draft", lambda *a, **k: None)
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, txt, **k: None)
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: payloads.append(p))

    _dispatch("/eh", reply_to_message={"text": "card"})

    assert not [p for p in payloads if p.get("action") == "update_contact_email"], \
        "a patterned guess must never be written to the Contact Email column"


def test_a_blank_contact_email_is_still_eligible_for_backfill():
    """Blanking the cell must not opt the row out of the sent-mail back-fill."""
    assert m.is_guessed_contact_email("") is True
    assert m.is_guessed_contact_email("bizops@nbhs.com") is True
    assert m.is_guessed_contact_email("kara.wise@altarum.org") is False


def test_suppressed_row_card_is_repointed_at_the_live_row(monkeypatch):
    """THE BUG: Code.gs drops a duplicate row from the batch but reports overall success, so the
    card shipped carrying a uuid that was never written to any tab. Every later /warm, /apply and
    /n on it failed with 'No record found' forever.
    """
    cards = _dispatch_env(monkeypatch, _batch_resp(1, [
        {"sent_uuid": "SENT-DEAD", "status": "duplicate_suppressed", "existing_uuid": "LIVE-ROW"},
        {"sent_uuid": "SENT-OK", "status": "written", "existing_uuid": "SENT-OK"},
    ]))
    m.save_job_to_cache("s1", {"employer_name": "TEKsystems"}, "SENT-DEAD")

    m.dispatch_tier1_matches([
        _tier1_match("TEKsystems", "Operations Support Analyst", "s1", "SENT-DEAD"),
        _tier1_match("Other Co", "Ops Analyst", "s2", "SENT-OK"),
    ])

    assert "SENT-DEAD" not in cards, "a card must never carry a uuid with no row behind it"
    assert cards[0] == "LIVE-ROW", "the suppressed card must target the row that actually exists"
    assert cards[1] == "SENT-OK", "a written row's card is untouched"


def test_repointed_card_updates_the_local_cache(monkeypatch):
    """/stage and swipe recovery read the cache, so it must follow the card or they resolve the
    dead uuid."""
    _dispatch_env(monkeypatch, _batch_resp(0, [
        {"sent_uuid": "SENT-DEAD", "status": "duplicate_suppressed", "existing_uuid": "LIVE-ROW"},
    ]))
    m.save_job_to_cache("s1", {"employer_name": "TEKsystems"}, "SENT-DEAD")

    m.dispatch_tier1_matches([_tier1_match("TEKsystems", "Ops Analyst", "s1", "SENT-DEAD")])

    assert m.get_sheet_uuid_by_short_id("s1") == "LIVE-ROW"


def test_card_withheld_when_the_live_row_has_no_uuid(monkeypatch):
    """A hand-added sheet row with an empty column J gives nothing to resolve against - shipping a
    card whose every swipe fails is worse than shipping none."""
    alerts = []
    cards = _dispatch_env(monkeypatch, _batch_resp(0, [
        {"sent_uuid": "SENT-DEAD", "status": "duplicate_suppressed", "existing_uuid": ""},
    ]))
    monkeypatch.setattr(m, "send_health_alert", lambda msg: alerts.append(msg))

    sent = m.dispatch_tier1_matches([_tier1_match("TEKsystems", "Ops Analyst", "s1", "SENT-DEAD")])

    assert sent == 0 and cards == []
    assert any("no UUID" in a for a in alerts), "Kevin must be told why the card never arrived"


def test_dispatch_falls_back_cleanly_on_an_old_apps_script(monkeypatch):
    """A Code.gs deployment predating the dispositions field reports no per-row detail. Silence
    must NOT be read as 'suppressed' - the old behavior (card everything in a written batch) is
    the correct fallback until the script is redeployed."""
    cards = _dispatch_env(monkeypatch, _batch_resp(2, None))

    sent = m.dispatch_tier1_matches([
        _tier1_match("A Co", "Ops Analyst", "s1", "U1"),
        _tier1_match("B Co", "Ops Analyst", "s2", "U2"),
    ])

    assert sent == 2
    assert cards == ["U1", "U2"]


def test_batch_dispositions_do_not_leak_between_batches(monkeypatch):
    """Stale per-row state would re-point a later card at an unrelated row."""
    _dispatch_env(monkeypatch, _batch_resp(1, [
        {"sent_uuid": "OLD", "status": "duplicate_suppressed", "existing_uuid": "OLD-LIVE"},
    ]))
    m.dispatch_tier1_matches([_tier1_match("A Co", "Ops", "s1", "OLD")])
    assert m.get_batch_disposition("OLD") is not None

    # A second batch that reports nothing for the old uuid must clear it.
    monkeypatch.setattr(m, "crm_post", lambda payload, **kw: _batch_resp(1, [
        {"sent_uuid": "NEW", "status": "written", "existing_uuid": "NEW"},
    ]))
    m.dispatch_tier1_matches([_tier1_match("B Co", "Ops", "s2", "NEW")])
    assert m.get_batch_disposition("OLD") is None, "stale disposition leaked into a later batch"


def test_remap_cached_job_uuid_is_a_noop_on_missing_args():
    assert m.remap_cached_job_uuid("", "abc") is False
    assert m.remap_cached_job_uuid("s1", "") is False


# ---- Stale suppression: a deleted row must not block ingest forever ----

_AAA = ("AAA Life Insurance Company", "Annuity Processing Specialist")


def _tracked_env(monkeypatch, rows_by_tab):
    """Serve get_followups from a mutable dict so a test can 'delete' a row mid-flight."""
    class _R:
        status_code = 200
        def __init__(self, rows):
            self.rows = rows
        def json(self):
            return {"status": "success", "followups": self.rows}

    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "crm_post",
                        lambda payload, **kw: _R(rows_by_tab.get(payload.get("tab"), [])))
    m.invalidate_tracked_role_cache()


def test_deleted_row_stops_blocking_once_the_cache_refreshes(monkeypatch):
    company, title = _AAA
    sheet = {"TC": [{"company": company, "job_title": title}], "TW": [], "CL": []}
    _tracked_env(monkeypatch, sheet)
    assert m.is_role_tracked(company, title)

    sheet["TC"] = []                      # Kevin deletes the row by hand
    m.invalidate_tracked_role_cache()     # what /resync does
    assert not m.is_role_tracked(company, title)


def test_stale_cache_errs_open_when_sheets_stays_unreachable(monkeypatch):
    """THE BUG: on a failed refresh fetched_at is never advanced, so the stale set answered
    forever - /job insisted a role was 'already in the pipeline' against an empty tab."""
    company, title = _AAA
    _tracked_env(monkeypatch, {"TC": [{"company": company, "job_title": title}], "TW": [], "CL": []})
    assert m.is_role_tracked(company, title)

    monkeypatch.setattr(m, "crm_post", lambda payload, **kw: None)  # Sheets unreachable
    monkeypatch.setattr(m, "send_health_alert", lambda msg: None)

    # Within the stale bound the last good set still answers.
    m._TRACKED_ROLE_CACHE["fetched_at"] = time.time() - 400
    assert m.is_role_tracked(company, title), "a brief blip must not re-card everything"

    # Past it, suppression must give up rather than block a role Kevin cannot ingest.
    m._TRACKED_ROLE_CACHE["fetched_at"] = time.time() - (m._TRACKED_ROLE_CACHE_MAX_STALE_SECONDS + 60)
    assert not m.is_role_tracked(company, title), "stale suppression must err OPEN, not closed"


def test_stale_cache_expiry_alerts_that_dedup_is_off(monkeypatch):
    company, title = _AAA
    _tracked_env(monkeypatch, {"TC": [{"company": company, "job_title": title}], "TW": [], "CL": []})
    m.is_role_tracked(company, title)
    alerts = []
    monkeypatch.setattr(m, "crm_post", lambda payload, **kw: None)
    monkeypatch.setattr(m, "send_health_alert", lambda msg: alerts.append(msg))
    m._TRACKED_ROLE_CACHE["fetched_at"] = time.time() - (m._TRACKED_ROLE_CACHE_MAX_STALE_SECONDS + 60)

    m.is_role_tracked(company, title)

    assert any("running blind" in a for a in alerts), "Kevin must know dedup stopped"


def test_locate_tracked_role_finds_the_live_row(monkeypatch):
    company, title = _AAA
    _tracked_env(monkeypatch, {
        "TC": [], "TW": [{"company": company, "job_title": title, "status": "Applied",
                          "sheet_uuid": "U-LIVE"}], "CL": [],
    })
    where = m.locate_tracked_role(company, title)
    assert where and where["tab"] == "Tetiana Warm"
    assert where["status"] == "Applied" and where["sheet_uuid"] == "U-LIVE"


def test_locate_tracked_role_returns_none_for_a_cached_ghost(monkeypatch):
    """This is what distinguishes a real duplicate from a stale block, so the message can say so."""
    company, title = _AAA
    _tracked_env(monkeypatch, {"TC": [], "TW": [], "CL": []})
    assert m.locate_tracked_role(company, title) is None


def test_ingest_message_flags_a_stale_block(monkeypatch):
    """'Already in the pipeline' with nothing to open is a dead end when the row is gone."""
    company, title = _AAA
    monkeypatch.setattr(m, "is_role_tracked", lambda c, t: True)
    monkeypatch.setattr(m, "locate_tracked_role", lambda c, t: None)
    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: pytest.fail("must not score"))

    ok, message = m.ingest_manual_job(title=title, company=company)

    assert ok is False
    assert "stale" in message.lower()
    assert "/job!" in message and "/resync" in message


def test_ingest_message_names_the_row_for_a_real_duplicate(monkeypatch):
    company, title = _AAA
    monkeypatch.setattr(m, "is_role_tracked", lambda c, t: True)
    monkeypatch.setattr(m, "locate_tracked_role", lambda c, t: {
        "tab": "Tetiana Cold", "row_label": "#3", "status": "Matched", "sheet_uuid": "U1"})
    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: pytest.fail("must not score"))

    ok, message = m.ingest_manual_job(title=title, company=company)

    assert "Tetiana Cold" in message and "#3" in message and "Matched" in message


def test_forced_ingest_bypasses_the_tracked_gate(monkeypatch):
    """/job! is the escape hatch for when the gate is wrong."""
    company, title = _AAA
    monkeypatch.setattr(m, "is_role_tracked", lambda c, t: pytest.fail("gate must be skipped"))
    scored = []
    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: scored.append(job) or None)
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **kw: None)

    m.ingest_manual_job(title=title, company=company, force=True)

    assert scored, "a forced ingest must reach scoring"


# ---- /job! must force a card even when AI screening rejects ----

def _reject_env(monkeypatch, reason="Insurance sales role, not operations",
                track="", indices=None, score=0):
    monkeypatch.setattr(m, "evaluate_job_with_gemini",
                        lambda job: (False, score, reason, track, "", indices or [], 0, 0, 0, 0))
    for name, fn in (("record_jd_terms", lambda *a, **k: 0),
                     ("resolve_live_alumni_at_company", lambda c: None),
                     ("get_warm_crm_contacts", lambda: {}),
                     ("get_ghost_listing_penalty", lambda h: (0, "")),
                     ("log_metric_event", lambda *a, **k: None),
                     ("resolve_target_email", lambda *a, **k: "ops@example.com")):
        monkeypatch.setattr(m, name, fn)


_REJECTED_JOB = {
    "job_id": "ingest_x", "employer_name": "AAA-The Auto Club Group",
    "job_title": "Life Insurance Specialist - Michigan",
    "job_description": "Sell life insurance.", "job_apply_link": "https://x",
    "job_posted_at_datetime_utc": "2026-09-21T15:11:49Z",
}


def test_ai_rejection_still_blocks_an_unforced_candidate(monkeypatch):
    """The screener must keep working for /t and a plain /job."""
    _reject_env(monkeypatch)
    assert m.process_single_candidate(dict(_REJECTED_JOB)) is None


def test_forced_candidate_builds_a_card_despite_the_rejection(monkeypatch):
    """THE ASK: /job! means Kevin's judgment outranks the screener's."""
    _reject_env(monkeypatch)
    result = m.process_single_candidate(dict(_REJECTED_JOB), force=True)
    assert result, "a forced ingest must produce a dispatchable card"
    assert result["sheet_uuid"], "the card needs a real row id or every swipe fails"
    assert result["score"] >= 1, "score 0 would sort below every real match"


def test_forced_card_resolves_copy_when_the_rejection_left_routing_unset(monkeypatch):
    """A rejection can return an empty track and no bullet indices; the card still needs bullets."""
    _reject_env(monkeypatch, track="", indices=[])
    result = m.process_single_candidate(dict(_REJECTED_JOB), force=True)
    job = result["job"]
    assert job.get("track"), "a forced card must fall back to a real track"
    bullets = m.filter_ats_bullets(job.get("track"), job.get("bullet_indices"), job.get("tone_mode"))
    assert len(bullets) >= 3, "the resume block must not come out empty"


def test_forced_card_is_flagged_on_the_card_and_in_the_note(monkeypatch):
    """A forced card that looks identical to a scored one is a trap weeks later."""
    _reject_env(monkeypatch, reason="Insurance sales role, not operations")
    result = m.process_single_candidate(dict(_REJECTED_JOB), force=True)
    assert "FORCED" in result["age_badge"]
    assert "FORCED via /job!" in result["reason"]
    assert "Insurance sales role" in result["reason"], "the screener's objection must survive"
    assert result["job"].get("forced_override") is True


def test_forced_card_keeps_a_genuine_pass_untouched(monkeypatch):
    """force must not rewrite the routing or note of a role that actually passed."""
    monkeypatch.setattr(m, "evaluate_job_with_gemini",
                        lambda job: (True, 92, "Strong ops fit", "b", "conservative", [2, 3, 4], 0, 0, 0, 92))
    _reject_env(monkeypatch)
    monkeypatch.setattr(m, "evaluate_job_with_gemini",
                        lambda job: (True, 92, "Strong ops fit", "b", "conservative", [2, 3, 4], 0, 0, 0, 92))
    result = m.process_single_candidate(dict(_REJECTED_JOB), force=True)
    assert result["score"] == 92
    assert "FORCED" not in result["age_badge"]
    assert result["reason"] == "Strong ops fit"
    assert result["job"].get("forced_override") is None


def test_ingest_rejection_message_points_at_the_force_flag(monkeypatch):
    monkeypatch.setattr(m, "is_role_tracked", lambda c, t: False)
    monkeypatch.setattr(m, "process_single_candidate", lambda job, force=False: None)
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **kw: None)

    ok, message = m.ingest_manual_job(title="Life Insurance Specialist", company="AAA")

    assert ok is False
    assert "/job!" in message, "the rejection must tell Kevin how to override it"


def test_ingest_passes_force_through_to_scoring(monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "is_role_tracked", lambda c, t: False)
    monkeypatch.setattr(m, "process_single_candidate",
                        lambda job, force=False: seen.update(force=force) or None)
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **kw: None)

    m.ingest_manual_job(title="Life Insurance Specialist", company="AAA", force=True)

    assert seen.get("force") is True, "/job! must reach the screener as an override"


# ---- bare /e must not fill Contact Email with a guessed address ----

def test_fallback_addresses_are_flagged_unverified():
    """The guard /e relies on: a company-name guess must be distinguishable from a real address."""
    guess = m.resolve_target_email("AAA Life Insurance Company", "Annuity Specialist", None)
    assert m.is_unverified_email(guess), "a name-mangled guess must carry the fallback tag"
    real = m.resolve_target_email("Real Co", "Ops Analyst", "https://realco.com")
    assert not m.is_unverified_email(real), "an address off the real domain must NOT be flagged"


def _e_env(monkeypatch, employer_website=None, company="AAA Life Insurance Company"):
    """Drive the real /e handler with everything outbound stubbed. Returns what it tried to save."""
    saved = {"local": [], "crm": [], "headers": []}
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-e", "sheet_tab": "Tetiana Cold",
        "contact_name": "", "contact_company": company})
    monkeypatch.setattr(m, "get_job_by_sheet_uuid", lambda u: {
        "job_title": "Annuity Processing Specialist", "employer_name": company,
        "employer_website": employer_website, "job_id": "ingest_x"})
    monkeypatch.setattr(m, "rebuild_job_from_card", lambda job, txt: (job, False))
    monkeypatch.setattr(m, "_job_data_available", lambda job, mapping: True)
    monkeypatch.setattr(m, "update_job_target_email",
                        lambda uuid, email: saved["local"].append(email))
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: saved["crm"].append(p))
    monkeypatch.setattr(m, "log_addressed_contact_to_carmen_cold", lambda *a, **k: None)
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, txt, **k: None)

    def _stage(chat_id, mapping, job, comp, title, is_warm, email, header, label):
        saved["headers"].append(header)
        saved["drafted_to"] = email
    monkeypatch.setattr(m, "stage_outreach_draft", _stage)
    return saved


def test_bare_e_leaves_contact_email_blank_when_it_only_has_a_guess(monkeypatch):
    """THE ASK: bare /e with no real domain must not write operations@<mangled-name>.com into the
    Contact Email column - that filled the sheet with addresses nobody had verified."""
    saved = _e_env(monkeypatch, employer_website=None)

    _dispatch("/e", reply_to_message={"text": "card"})

    assert saved["local"] == [], "a guessed address must never reach the local cache"
    assert saved["crm"] == [], "a guessed address must never reach the sheet"


def test_bare_e_still_drafts_to_an_invented_role_mailbox_but_says_do_not_send(monkeypatch):
    """With no employer website, resolution invents BOTH the mailbox and the domain
    (operations@<mangled-name>.com) - the shape that got operations@mahle.com sent on 2026-09-23.

    This used to REFUSE to draft. That was an outage, not a guard: resolve_target_email() has no
    non-role output at all (operations@/bizops@/compliance@/wealthops@ are its only returns), so
    every job-alert row without a website answered "Draft Not Created" and bare /e was dead.

    The draft is not the dangerous step - sending is, and Kevin reads every draft first. So the
    copy is produced, the header says DO NOT SEND, and the address still never reaches the CRM.
    """
    saved = _e_env(monkeypatch, employer_website=None)

    _dispatch("/e", reply_to_message={"text": "card"})

    assert saved.get("drafted_to"), "the draft must still be produced - refusing here broke /e"
    assert saved["local"] == [], "a guessed address must never reach the local cache"
    assert saved["crm"] == [], "a guessed address must never reach the sheet"


def test_bare_e_never_persists_a_role_mailbox_even_at_the_real_domain(monkeypatch):
    """A REAL domain does not make operations@ a person, and this test used to assert the
    opposite - that operations@realco.com was "evidence" worth writing to the sheet.

    That rule is what filled Tetiana Warm with operations@computacenter.com,
    operations@raymondjames.com and wealthops@waldronprivatewealth.com. The gate was
    is_unverified_email(), which only reads the [⚠️ Fallback] tag - and that tag marks an invented
    DOMAIN, not an invented mailbox. A role mailbox at the employer's own domain carries no tag,
    so it read as verified and was written.

    The only test that decides this is is_guessed_contact_email(), the same one the discovery
    path uses to blank the cell. If /e used a weaker rule it could write what discovery refused.
    """
    saved = _e_env(monkeypatch, employer_website="https://realco.com", company="Real Co")

    _dispatch("/e", reply_to_message={"text": "card"})

    assert saved["drafted_to"] == "operations@realco.com", "it must still DRAFT to the guess"
    assert saved["local"] == [], "but a role mailbox must never reach the local cache"
    assert saved["crm"] == [], "and never reach the sheet"


def test_typed_address_always_persists(monkeypatch):
    """Typing the address IS the confirmation - it saves regardless of what resolution would say."""
    saved = _e_env(monkeypatch, employer_website=None)

    _dispatch("/e dana@weird-domain.io", reply_to_message={"text": "card"})

    assert saved["local"] == ["dana@weird-domain.io"]
    assert len(saved["crm"]) == 1


# ---- Job-link sweep: what it retires and what it refuses to ----

def _linkcheck_env(monkeypatch, rows, fetches):
    """rows: list of CRM records per tab call. fetches: {url: (code, final, text, err)}"""
    class _R:
        status_code = 200
        def __init__(self, rows): self.rows = rows
        def json(self): return {"status": "success", "followups": self.rows}

    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "crm_post",
                        lambda payload, **kw: _R(rows if payload.get("tab") == "TC" else []))
    monkeypatch.setattr(m, "fetch_job_link_state",
                        lambda url: fetches.get(url, (200, url, "<p>Apply now</p>", None)))
    queued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: queued.append(p))
    monkeypatch.setattr(m.time, "sleep", lambda *a, **k: None)
    return queued


def _job_row(uuid_v, status, link, company="Acme", role="Ops Analyst"):
    return {"sheet_uuid": uuid_v, "status": status, "job_link": link,
            "company": company, "job_title": role}


def test_sweep_retires_a_matched_row_with_a_dead_link(monkeypatch):
    queued = _linkcheck_env(
        monkeypatch,
        [_job_row("u-matched", "Matched", "https://co.com/j/1")],
        {"https://co.com/j/1": (404, "https://co.com/j/1", "", None)},
    )
    result = m.check_job_links(sleep_between=0)

    assert len(result["dead"]) == 1
    assert len(result["retired"]) == 1
    actions = [p.get("action") for p in queued]
    assert "append_note" in actions and "update_status" in actions
    move = [p for p in queued if p.get("action") == "update_status"][0]
    assert move["new_tab"] == "Died"


def test_sweep_never_retires_an_applied_row(monkeypatch):
    """THE SAFETY RULE: a dead posting after Kevin applied means they stopped sourcing, not that
    he was rejected. Auto-burying it would lose a live thread."""
    queued = _linkcheck_env(
        monkeypatch,
        [_job_row("u-applied", "Applied", "https://co.com/j/2")],
        {"https://co.com/j/2": (404, "https://co.com/j/2", "", None)},
    )
    result = m.check_job_links(sleep_between=0)

    assert len(result["dead"]) == 1, "it must still be REPORTED as dead"
    assert result["retired"] == [], "but nothing may be written"
    assert queued == [], "no CRM payload at all for an applied row"


def test_sweep_leaves_a_live_link_alone(monkeypatch):
    queued = _linkcheck_env(
        monkeypatch,
        [_job_row("u-live", "Matched", "https://co.com/j/3")],
        {"https://co.com/j/3": (200, "https://co.com/j/3", "<p>Apply now</p>", None)},
    )
    result = m.check_job_links(sleep_between=0)
    assert result["dead"] == [] and queued == []


def test_sweep_treats_an_unreachable_host_as_unknown(monkeypatch):
    """An outage must never retire rows."""
    queued = _linkcheck_env(
        monkeypatch,
        [_job_row("u-err", "Matched", "https://co.com/j/4")],
        {"https://co.com/j/4": (None, None, "", TimeoutError("boom"))},
    )
    result = m.check_job_links(sleep_between=0)
    assert result["unknown"] == 1 and result["dead"] == [] and queued == []


def test_sweep_caps_auto_retirement(monkeypatch):
    """Same reasoning as MAX_AUTO_KILLS_PER_RUN: overflow is reported, not written."""
    over = m.MAX_AUTO_RETIRE_PER_RUN + 3
    rows = [_job_row(f"u{i}", "Matched", f"https://co.com/j/{i}") for i in range(over)]
    fetches = {f"https://co.com/j/{i}": (404, f"https://co.com/j/{i}", "", None) for i in range(over)}
    _linkcheck_env(monkeypatch, rows, fetches)

    result = m.check_job_links(sleep_between=0)

    assert len(result["dead"]) == over
    assert len(result["retired"]) == m.MAX_AUTO_RETIRE_PER_RUN


def test_dead_links_are_recorded_and_readable(monkeypatch):
    _linkcheck_env(
        monkeypatch,
        [_job_row("u-rec", "Applied", "https://co.com/j/9", company="Huntington", role="FX Ops")],
        {"https://co.com/j/9": (404, "https://co.com/j/9", "", None)},
    )
    m.check_job_links(sleep_between=0)

    rows = m.get_dead_job_links()
    assert len(rows) == 1
    assert rows[0][1] == "Huntington" and rows[0][2] == "FX Ops"


def test_a_plain_human_reply_counts_toward_reply_rate(monkeypatch):
    """THE BUG: only interview/rejection/offer were ever recorded, so a mailbox holding a real
    reply still reported 0.0%. A GENERAL reply now writes its own 'reply' row, and reply rate is
    replied/applied - not interview/applied wearing the word "reply"."""
    uuid = "reply-metrics"
    m.record_application_outcome(uuid, "applied", company="Acme", source="jsearch:test",
                                 outreach_path="cold")
    m.record_application_outcome(uuid, "reply", company="Acme")

    metrics = m.get_outcome_metrics()
    src = metrics["by_source"]["jsearch:test"]
    assert src["applied"] == 1 and src["replied"] == 1
    assert src["reply_rate"] == 100.0, "a human answered - that is a reply"
    assert src["interview_rate"] == 0.0, "but it is NOT an interview"

    # And the cold path is now a measurable bucket of its own, not folded into "ats".
    assert metrics["by_outreach_path"]["cold"]["replied"] == 1


def test_a_rejection_is_still_a_reply(monkeypatch):
    """Excluding rejections would report a channel that only ever says no as producing 0 replies,
    which is the opposite of what reply rate is for."""
    uuid = "rejection-metrics"
    m.record_application_outcome(uuid, "applied", company="Acme", source="jsearch:rej")
    m.record_application_outcome(uuid, "rejection", company="Acme")

    src = m.get_outcome_metrics()["by_source"]["jsearch:rej"]
    assert src["replied"] == 1 and src["reply_rate"] == 100.0
    assert src["interview"] == 0


def test_brief_page_holds_the_depth_the_chat_used_to_dump(monkeypatch):
    """Four Telegram messages of counters became one page. The page must carry the numbers."""
    monkeypatch.setattr(m, "get_rolling_metric_counts", lambda days=7: {
        "listing_discovered": 2280, "ai_screened": 913, "gmail_draft_staged": 75,
        "applied": 46, "interview_set": 1})
    monkeypatch.setattr(m, "get_monthly_api_usage", lambda: {"hunter": 0, "prospeo": 0, "getprospect": 0})
    monkeypatch.setattr(m, "get_outcome_metrics", lambda: {"by_source": {
        "jsearch:learn4good.com": {"applied": 4, "interview": 0, "reply_rate": 0.0}}})
    monkeypatch.setattr(m, "scan_carmen_hot_conversations", lambda *a, **k: [
        {"name": "Beth Young", "company": "Altarum", "status": "Phone Screen",
         "when": "2026-09-19", "state": "overdue", "days": -3}])
    monkeypatch.setattr(m, "get_overdue_followups", lambda: [])
    monkeypatch.setattr(m, "get_dead_job_links", lambda **kw: [])
    monkeypatch.setattr(m, "load_followup_queue_snapshot", lambda d: None)
    monkeypatch.setattr(m, "get_filter", lambda k, default=None: [] if default is None else default)

    body, status = m.morning_brief_view()
    assert status == 200
    assert "Live Conversations (1)" in body and "1 need you now" in body
    assert "Beth Young" in body and "3d ago" in body
    assert "2280" in body and "913" in body and "1.3%" in body   # golden ratio 1/75
    assert "jsearch:learn4good.com" in body                      # publisher split is visible


def test_tuesday_hub_is_a_headline_plus_a_link_not_a_wall(monkeypatch):
    """The hub printed 14 lines of slow-moving counters, pushing the actionable one to the end."""
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, *a, **k: sent.append(t) or 1)
    monkeypatch.setattr(m, "get_rolling_metric_counts", lambda days=7: {
        "listing_discovered": 2280, "ai_screened": 913, "gmail_draft_staged": 75,
        "applied": 46, "interview_set": 0})
    monkeypatch.setattr(m, "get_monthly_api_usage", lambda: {"hunter": 0, "prospeo": 0, "getprospect": 0})
    monkeypatch.setattr(m, "get_overdue_followups", lambda: [])
    monkeypatch.setattr(m, "get_filter", lambda k, default=None: [] if default is None else default)

    m.send_tuesday_pipeline_executive_hub(1)

    assert len(sent) == 1, "the separate outcomes message should no longer be sent"
    msg = sent[0]
    assert "/brief" in msg and "46" in msg
    assert "Hunter.io" not in msg and "ATS boards" not in msg   # moved to the page
    assert len(msg.splitlines()) < 12


def test_standup_leads_with_an_overdue_conversation(monkeypatch):
    """A call that happened 3 days ago with no follow-up outranks a streak counter."""
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, *a, **k: sent.append(t) or 1)
    monkeypatch.setattr(m, "get_daily_activity", lambda d: {"drafts_staged": 2})
    monkeypatch.setattr(m, "calculate_active_day_streak", lambda: 5)
    monkeypatch.setattr(m, "get_overdue_followups", lambda: [])
    monkeypatch.setattr(m, "get_dead_job_links", lambda **kw: [])
    monkeypatch.setattr(m, "check_system_health", lambda: [])
    monkeypatch.setattr(m, "scan_carmen_hot_conversations", lambda *a, **k: [
        {"name": "Beth Young", "state": "overdue", "days": -3}])

    m.send_daily_standup(1)
    msg = sent[0]
    assert "Beth Young" in msg
    assert msg.index("Beth Young") < msg.index("Active Streak")
    assert "/brief" in msg


def test_dead_says_the_row_did_not_move(monkeypatch):
    """/dead is measurement only. Saying just "Marked dead" read as an archive, so a row left
    deliberately in place looked like a failed write."""
    sent = []
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, cid, label: {
        "sheet_uuid": "u-dead", "sheet_tab": "Tetiana Warm", "contact_company": "Huntington"})
    monkeypatch.setattr(m, "get_job_by_sheet_uuid", lambda u: {
        "employer_name": "Huntington", "job_title": "Foreign Exchange Ops Analyst 2",
        "job_id": "js_1", "job_apply_link": "https://huntington.com/j/1"})
    monkeypatch.setattr(m, "get_posted_hours_at_card", lambda u: None)
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, *a, **k: sent.append(t) or 1)
    monkeypatch.setattr(m, "edit_telegram_message", lambda *a, **k: None)
    enqueued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: enqueued.append(p) or True)
    outcomes = []
    monkeypatch.setattr(m, "record_application_outcome",
                        lambda u, s, **kw: outcomes.append((u, s)) or True)

    _dispatch("/dead")

    assert enqueued == [], "/dead must not move the row - /x does that"
    assert outcomes == [("u-dead", "dead_link")]
    assert "NOT moved" in sent[0] and "/x" in sent[0]


def test_links_points_at_x_not_dead_for_archiving(monkeypatch):
    """The /links footer told Kevin "/dead on the card retires it", which contradicts the code."""
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, *a, **k: sent.append(t) or 1)
    monkeypatch.setattr(m, "get_dead_job_links", lambda **kw: [
        ("u-app", "Huntington", "FX Ops Analyst 2", "http://x/1", "Applied", "HTTP 404", 0,
         datetime.now().date().isoformat())])
    _dispatch("/links")

    msg = sent[-1]
    assert "<code>/x</code> on the card archives one" in msg
    assert "/dead</code> only records the decoy" in msg
    # The bulk form is offered alongside the per-card swipe, not instead of it.
    assert "<code>/linksx</code>" in msg


def test_dead_since_label_reads_as_a_takedown_date():
    """first_dead_at was stored from the start and never shown. On an APPLIED row it is the
    useful number: when the company stopped sourcing."""
    today = datetime.now().date()
    assert "(today)" in m._dead_since_label(today.isoformat())
    assert "(yesterday)" in m._dead_since_label((today - timedelta(days=1)).isoformat())
    three = (today - timedelta(days=3)).isoformat()
    assert m._dead_since_label(three) == f"{three} (3d ago)"
    # A timestamp, not just a date, is what SQLite actually stores.
    assert "(today)" in m._dead_since_label(f"{today.isoformat()} 07:45:00")
    assert m._dead_since_label("") == "date unknown"
    assert m._dead_since_label("not-a-date") == "date unknown"


def test_links_separates_applied_rows_from_retired_ones(monkeypatch):
    """Huntington and Auto Warehousing were listed beside corpses. An applied row whose posting
    came down is a live thread to chase, not a dead row to read past."""
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, *a, **k: sent.append(t) or 1)
    today = datetime.now().date().isoformat()
    monkeypatch.setattr(m, "get_dead_job_links", lambda **kw: [
        ("u-app", "Huntington", "Foreign Exchange Ops Analyst 2", "http://x/1",
         "Applied", "HTTP 404", 0, today),
        ("u-ret", "Coric Equipment", "Treasury Analyst", "http://x/2",
         "Matched", "no longer available", 1, today),
    ])
    _dispatch("/links")

    msg = sent[-1]
    assert "You applied - posting came down (1)" in msg
    assert "Auto-retired to Died (1)" in msg
    # The applied row leads, carries its takedown date, and says what it means.
    assert msg.index("Huntington") < msg.index("Coric Equipment")
    assert "taken down" in msg and "(today)" in msg
    assert "not a rejection" in msg


def test_each_death_is_reported_only_once(monkeypatch):
    """The digest must not repeat the same dead posting every morning."""
    _linkcheck_env(
        monkeypatch,
        [_job_row("u-once", "Applied", "https://co.com/j/10")],
        {"https://co.com/j/10": (404, "https://co.com/j/10", "", None)},
    )
    m.check_job_links(sleep_between=0)

    fresh = m.get_dead_job_links(include_notified=False)
    assert len(fresh) == 1
    m.mark_dead_links_notified([r[0] for r in fresh])
    assert m.get_dead_job_links(include_notified=False) == []
    assert len(m.get_dead_job_links(include_notified=True)) == 1


def test_dry_run_writes_nothing_but_still_reports(monkeypatch):
    """/links check must be a true preview - it reports what WOULD be retired and touches nothing."""
    queued = _linkcheck_env(
        monkeypatch,
        [_job_row("u-dry", "Matched", "https://co.com/j/20")],
        {"https://co.com/j/20": (404, "https://co.com/j/20", "", None)},
    )
    result = m.check_job_links(auto_retire=False, sleep_between=0)

    assert len(result["dead"]) == 1, "a dry run still reports the dead link"
    assert result["retired"] == [], "a dry run retires nothing"
    assert queued == [], "a dry run queues no CRM write"


def test_dry_run_then_commit_actually_retires(monkeypatch):
    """The commit path still works after a dry run - the dry run must not mark anything done."""
    rows = [_job_row("u-two", "Matched", "https://co.com/j/21")]
    fetches = {"https://co.com/j/21": (404, "https://co.com/j/21", "", None)}
    queued = _linkcheck_env(monkeypatch, rows, fetches)

    m.check_job_links(auto_retire=False, sleep_between=0)
    assert queued == []

    result = m.check_job_links(auto_retire=True, sleep_between=0)
    assert len(result["retired"]) == 1
    assert [p.get("action") for p in queued] == ["append_note", "update_status"]


# ---- Command usage tracking ----

def test_normalize_strips_arguments():
    """'/f 7' and '/f 14' are the same command - the question is which ones Kevin reaches for."""
    assert m.normalize_command_name("/f 7") == "/f"
    assert m.normalize_command_name("/f 14") == "/f"
    assert m.normalize_command_name("/quick Dana @ Acme 5 note") == "/quick"


def test_normalize_keeps_the_force_bang():
    """/job! overrides the AI screener - counting it as /job would hide how often that happens."""
    assert m.normalize_command_name("/job! https://x.com/1") == "/job!"
    assert m.normalize_command_name("/job https://x.com/1") == "/job"


def test_normalize_handles_bot_suffix_and_case():
    assert m.normalize_command_name("/Help@MyBot") == "/help"


def test_normalize_rejects_non_commands():
    for junk in ("", None, "hello", "not /a command", "//", "/"):
        assert m.normalize_command_name(junk) == ""


def test_record_and_read_back_usage():
    for cmd in ("/t", "/t", "/t", "/e dana@x.com", "/links check"):
        m.record_command_usage(cmd)
    rows = m.get_command_usage(days=30)
    counts = {c: n for c, n, _ in rows}
    assert counts["/t"] == 3
    assert counts["/e"] == 1 and counts["/links"] == 1


def test_usage_is_sorted_most_used_first():
    for _ in range(5):
        m.record_command_usage("/t")
    m.record_command_usage("/health")
    rows = m.get_command_usage(days=30)
    assert rows[0][0] == "/t" and rows[0][1] == 5


def test_usage_window_excludes_older_events():
    """A 7-day window must not count a command used a month ago."""
    m.record_command_usage("/old")
    with m.get_db_conn() as conn:
        conn.execute("UPDATE command_usage SET used_at = datetime('now','-40 days') WHERE command = '/old'")
        conn.commit()
    m.record_command_usage("/new")

    week = {c for c, _n, _l in m.get_command_usage(days=7)}
    assert "/new" in week and "/old" not in week

    everything = {c for c, _n, _l in m.get_command_usage(days=None)}
    assert "/old" in everything and "/new" in everything


def test_usage_totals_match_the_window():
    m.record_command_usage("/t")
    m.record_command_usage("/t")
    m.record_command_usage("/health")
    total, distinct, _first = m.get_command_usage_totals(days=30)
    assert total == 3 and distinct == 2


def test_recording_never_raises_on_junk():
    """Telemetry must never break a command."""
    for junk in (None, "", "not a command", 12345):
        assert m.record_command_usage(junk) == ""


def test_every_dispatched_command_is_counted(monkeypatch):
    """The recorder sits at the single choke point, so a real dispatch must land in the table."""
    monkeypatch.setattr(m, "send_telegram_message", lambda *a, **k: None)
    m.process_webhook_payload_async({"message": {"chat": {"id": 1}, "text": "/health"}})
    counts = {c: n for c, n, _ in m.get_command_usage(days=30)}
    assert counts.get("/health", 0) >= 1


# ---- A rejected CRM read must not look like an empty sheet ----

def _crm_read(monkeypatch, body, status_code=200):
    class _R:
        def __init__(self):
            self.status_code = status_code
        def json(self):
            return body
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "crm_post", lambda payload, **kw: _R())
    m._CRM_READ_REJECTION_ALERTED.clear()
    alerts = []
    monkeypatch.setattr(m, "send_health_alert", lambda msg: alerts.append(msg))
    return alerts


def test_unauthorized_read_returns_empty_and_alerts(monkeypatch):
    """THE BUG: Apps Script answers 200 with {"status":"error"} when CRM_SHARED_SECRET does not
    match. Reading .get("followups", []) off that body gave [], indistinguishable from an empty
    tab - so /links reported "Checked 0 links" against a sheet full of rows."""
    alerts = _crm_read(monkeypatch, {"status": "error", "message": "Unauthorized"})

    assert m.fetch_networking_cards("TC", qty=None) == []
    assert any("rejected" in a.lower() for a in alerts), "a rejected read must alert"
    assert any("CRM_SHARED_SECRET" in a for a in alerts), "and must name the actual fix"


def test_rejected_read_alerts_only_once_per_process(monkeypatch):
    """The sweep reads three tabs per pass; three identical alerts would be noise."""
    alerts = _crm_read(monkeypatch, {"status": "error", "message": "Unauthorized"})
    for _ in range(5):
        m.fetch_networking_cards("TC", qty=None)
    assert len(alerts) == 1


def test_a_genuinely_empty_tab_does_not_alert(monkeypatch):
    """An empty tab is a normal state - only a REJECTION is newsworthy."""
    alerts = _crm_read(monkeypatch, {"status": "success", "followups": []})
    assert m.fetch_networking_cards("TC", qty=None) == []
    assert alerts == []


def test_a_successful_read_still_returns_rows(monkeypatch):
    _crm_read(monkeypatch, {"status": "success", "followups": [{"company": "Acme"}]})
    rows = m.fetch_networking_cards("TC", qty=None)
    assert len(rows) == 1 and rows[0]["company"] == "Acme"


def test_non_200_read_returns_empty_without_crashing(monkeypatch):
    _crm_read(monkeypatch, {}, status_code=500)
    assert m.fetch_networking_cards("TC", qty=None) == []


def test_sweep_over_a_rejected_crm_checks_nothing(monkeypatch):
    """The downstream symptom Kevin saw: 0 links checked, and nothing written."""
    _crm_read(monkeypatch, {"status": "error", "message": "Unauthorized"})
    queued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: queued.append(p))
    monkeypatch.setattr(m, "fetch_job_link_state",
                        lambda url: pytest.fail("must not fetch when the CRM read failed"))

    result = m.check_job_links(sleep_between=0)

    assert result["checked"] == 0 and result["dead"] == [] and queued == []


def test_probe_reports_a_rejection_verbatim(monkeypatch):
    """The probe must report what the CRM SAID, not a guess at why."""
    _crm_read(monkeypatch, {"status": "error", "message": "Unauthorized"})
    monkeypatch.setattr(m, "CRM_SHARED_SECRET", "abc123")
    out = m.probe_crm_read()
    assert "HTTP 200" in out and "status: error" in out and "Unauthorized" in out
    assert "abc123" not in out, "the secret itself must never be printed"


def test_probe_flags_a_missing_secret_on_our_side(monkeypatch):
    """If CRM_SHARED_SECRET is unset on Render the bot sends NO secret - a distinct cause from
    a mismatch, and the one a 'go check the secret matches' message never surfaces."""
    _crm_read(monkeypatch, {"status": "error", "message": "Unauthorized"})
    monkeypatch.setattr(m, "CRM_SHARED_SECRET", None)
    assert "secret sent: NO" in m.probe_crm_read()


def test_probe_reports_a_healthy_read(monkeypatch):
    _crm_read(monkeypatch, {"status": "success", "followups": [{"company": "Acme"}]})
    monkeypatch.setattr(m, "CRM_SHARED_SECRET", "abc")
    out = m.probe_crm_read()
    assert "status: success" in out and "rows returned: 1" in out


def test_probe_survives_a_non_json_body(monkeypatch):
    class _R:
        status_code = 200
        text = "<html>Google sign-in</html>"
        def json(self): raise ValueError("not json")
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://fake")
    monkeypatch.setattr(m, "crm_post", lambda p, **k: _R())
    assert "non-JSON body" in m.probe_crm_read()


def test_probe_handles_no_response(monkeypatch):
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://fake")
    monkeypatch.setattr(m, "crm_post", lambda p, **k: None)
    assert "No response at all" in m.probe_crm_read()


def test_get_followups_contract_covers_what_the_sweep_reads():
    """THE BUG: get_followups never returned the Job Link, so check_job_links read every row and
    checked none - "Checked 0 links" against a sheet full of postings, which read as a clean
    sweep. Code.gs and main.py are separate files with no shared type, so the only thing keeping
    this contract honest is a test that reads both.
    """
    import re, pathlib
    root = pathlib.Path(m.__file__).parent
    gs = (root / "Code.gs").read_text(encoding="utf-8")
    py = (root / "main.py").read_text(encoding="utf-8")

    block = gs[gs.index('if (action === "get_followups")'):]
    push = block[block.index("results.push({"):]
    push = push[:push.index("});")]
    returned = set(re.findall(r"^\s*([a-z_]+):", push, re.M))

    sweep = py[py.index("def check_job_links"):][:3000]
    read = set(re.findall(r'rec\.get\("([a-z_]+)"', sweep))

    missing = read - returned
    assert not missing, f"check_job_links reads fields get_followups never returns: {sorted(missing)}"
    assert "job_link" in returned, "the sweep cannot work without the posting URL"


def test_sweep_skips_rows_without_a_usable_link(monkeypatch):
    """A row with no Job Link is not an error - it is just not checkable."""
    _linkcheck_env(
        monkeypatch,
        [{"sheet_uuid": "u1", "status": "Matched", "job_link": "", "company": "A", "job_title": "R"},
         {"sheet_uuid": "", "status": "Matched", "job_link": "https://x.com/1", "company": "B", "job_title": "R"},
         {"sheet_uuid": "u3", "status": "Matched", "job_link": "not-a-url", "company": "C", "job_title": "R"}],
        {},
    )
    result = m.check_job_links(sleep_between=0)
    assert result["checked"] == 0, "none of these three are checkable"


def test_sweep_checks_a_row_that_has_both_uuid_and_link(monkeypatch):
    """The positive control for the bug above - a well-formed row MUST be checked."""
    _linkcheck_env(
        monkeypatch,
        [{"sheet_uuid": "u1", "status": "Matched", "job_link": "https://co.com/j/1",
          "company": "Acme", "job_title": "Ops Analyst"}],
        {"https://co.com/j/1": (200, "https://co.com/j/1", "<p>Apply now</p>", None)},
    )
    result = m.check_job_links(sleep_between=0)
    assert result["checked"] == 1


# ---- Per-command help intercepts before dispatch (/cmd/) ----

def test_trailing_slash_help_intercepts_before_the_real_handler(monkeypatch):
    """`/w/` must explain the warm radar, not RUN it. The help lookup sits above every handler in
    process_webhook_payload_async, so this drives the real webhook entry point rather than calling
    lookup_command_help() directly - the whole risk is a dispatch-order mistake, which a unit test
    on the helper cannot see."""
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, txt, **k: sent.append(txt))

    ran = []
    monkeypatch.setattr(m, "run_warm_radar_scan", lambda *a, **k: ran.append("radar"))

    m.process_webhook_payload_async({"message": {"chat": {"id": 1}, "text": "/w/"}})
    assert ran == [], "/w/ executed the warm radar instead of explaining it"
    assert sent and "Warm radar" in sent[-1]


def test_bare_command_still_reaches_its_handler(monkeypatch):
    """The other half of the contract: adding the help layer must not shadow a real invocation.
    /edit with no arguments has a distinctive usage reply, so it proves dispatch got through."""
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, txt, **k: sent.append(txt))
    m.process_webhook_payload_async({"message": {"chat": {"id": 1}, "text": "/edit"}})
    assert sent and "Usage:" in sent[-1], "/edit no longer reaches its own handler"


def test_edit_usage_lists_the_slot_codes_that_actually_exist(monkeypatch):
    """The usage string drifted once already - it advertised C0-C2/W0-W1 long after both pools
    grew to six, and never learned about R. Pin it against the real banks so the next resize
    cannot leave the help lying."""
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, txt, **k: sent.append(txt))
    m.process_webhook_payload_async({"message": {"chat": {"id": 1}, "text": "/edit"}})
    usage = sent[-1]

    banks = m.load_outreach_templates()
    for prefix, pool_key in (("C", "cold_ops"), ("W", "warm_alumni"),
                             ("B", "followup_bumps"), ("R", "reactivation")):
        top = len(banks[pool_key]) - 1
        assert f"{prefix}0-{prefix}{top}" in usage, \
            f"/edit usage does not advertise {prefix}0-{prefix}{top} for {pool_key}"
        # And the top slot must really resolve, so the advertised range is not a lie.
        assert m.resolve_edit_target(f"{prefix}{top}") is not None


# ==============================================================================
# Eight-track expansion: registry, pools, and the non-finance resumes
# ==============================================================================
# These exist because a Rivian logistics posting rendered a wealth-operations resume: there was no
# non-finance track, so Gemini had to route it to `a`. The guards below are the ones that would
# have caught it, plus the two pool bullets that were silently never rendering.

_SECTION_BANNERS = (
    "SUMMARY", "PROFESSIONAL EXPERIENCE", "TECHNICAL PROJECTS",
    "EDUCATION & CREDENTIALS", "SKILLS & SYSTEMS",
)


def _markup_section(markup, banner):
    """The body of one rendered resume section, banner line excluded."""
    marker = "[" + banner + "]"
    start = markup.index(marker) + len(marker)
    later = [markup.index("[" + b + "]") for b in _SECTION_BANNERS
             if b != banner and markup.find("[" + b + "]") > start]
    return markup[start:min(later)] if later else markup[start:]


def _routed_bullet_lines(markup):
    """Only the FIRST job's bullets - the ones the routed track actually controls. Later jobs are
    Kevin's real employment history and legitimately say SEC, RIA and custodial."""
    section = _markup_section(markup, "PROFESSIONAL EXPERIENCE")
    lines = []
    for raw in section.splitlines():
        line = raw.strip()
        if line.startswith("#v(") and lines:
            break  # spacer before the second job header
        if line.startswith("- "):
            lines.append(line)
    return "\n".join(lines)


def _track_controlled_text(markup):
    """Everything on the page the track letter chooses: summary, routed bullets, skills footer."""
    return "\n".join((
        _markup_section(markup, "SUMMARY"),
        _routed_bullet_lines(markup),
        _markup_section(markup, "SKILLS & SYSTEMS"),
    ))


def test_no_pool_bullet_is_silently_dropped():
    """resume_engine drops any pool bullet >=0.80 similar to a static bullet of a job it is NOT
    attributed to, so such a bullet never renders and its pool quietly ships one entry short. Two
    were live when the eight-track work started (track_c[2] at 0.881, track_d[1] at 1.000). This is
    the regression guard for a bug that was already in production, not a hypothetical.

    Widened with source tags rather than narrowed: the comparison set is now per bullet (every job
    except its own, plus the project bullets, which were previously unguarded entirely), so a
    correctly-attributed ABC bullet is no longer measured against ABC's own statics.
    """
    evidence = m.load_evidence_bank()
    bank = json.load(open(resume_engine.RESUME_BULLETS_BANK_PATH, encoding="utf-8"))
    dropped = []
    for pool, bullets in bank.items():
        for i, entry in enumerate(bullets):
            text = resume_engine.bullet_text(entry)
            src = resume_engine.bullet_source_job(entry, evidence)
            for o in resume_engine._duplicate_check_targets(evidence, src):
                ratio = difflib.SequenceMatcher(None, text.lower(), str(o).lower()).ratio()
                if ratio >= 0.80:
                    dropped.append(f"{pool}[{i}] source_job={src} ratio={ratio:.3f}")
    assert dropped == [], f"these pool bullets can never render: {dropped}"


def test_logistics_track_resume_never_says_custodial():
    """THE Rivian regression test. Track f is what a Carrier Operations Analyst posting routes to,
    and the part of the page the track controls - summary, routed bullets, skills footer - must
    carry no wealth-operations vocabulary.

    Deliberately scoped to the track-controlled regions rather than the whole markup: the later
    jobs and the certificates are Kevin's real history and do say SEC, RIA and Schwab. A recruiter
    expects a finance work history. What lost the Rivian resume was the SUMMARY claiming a
    custodial-accounts persona, which is exactly what this asserts against.

    Salesforce is legitimate cross-industry tooling and is deliberately NOT in this list.
    """
    banned = r"custodial|advisor|Schwab|Fidelity|Wealthscape|ACAT|\bRIA\b|\bSEC\b|FinCEN|Form D|401\(k\)|broker"
    for tone in ("conservative", "tech"):
        markup = resume_engine.render_typst_markup("Rivian", "f", [0, 1, 2, 3], tone)
        hits = sorted({h.group(0) for h in re.finditer(banned, _track_controlled_text(markup), re.I)})
        assert hits == [], f"track f resume ({tone}) still reads as wealth ops: {hits}"


def test_new_tracks_claim_no_trucking_experience():
    """Kevin has no trucking, freight, warehouse or inventory experience. A resume that implies
    otherwise gets him into an interview he cannot survive, which is worse than not routing there
    at all. Checked over the ENTIRE page, since none of this vocabulary belongs anywhere on it."""
    banned = (r"\bcarriers?\b|freight|\bTMS\b|\btrucks?\b|\brail\b|\bocean\b|\bdocks?\b|"
              r"\blanes?\b|dispatch|OTIF|bill of lading|warehouse|inventory")
    for track in ("f", "g"):
        for tone in ("conservative", "tech"):
            markup = resume_engine.render_typst_markup("Rivian", track, [0, 1, 2, 3], tone)
            hits = sorted({h.group(0) for h in re.finditer(banned, markup, re.I)})
            assert hits == [], f"track {track} ({tone}) claims logistics experience: {hits}"


def test_registry_and_banks_cover_the_same_tracks():
    """The point of track_registry.py: adding a ninth track is a data edit. Forgetting one of the
    two JSON banks used to fall back to track a silently - the cover-letter half of the same bug
    that produced the Rivian resume."""
    bullets = json.load(open(resume_engine.RESUME_BULLETS_BANK_PATH, encoding="utf-8"))
    letters = m.load_cover_letter_templates()
    for letter, pool_key in track_registry.TRACK_BULLET_POOL_KEYS.items():
        assert pool_key in bullets, f"track {letter}: {pool_key} missing from resume_bullets_bank"
        assert pool_key in letters, f"track {letter}: {pool_key} missing from cover_letter_templates"
        assert bullets[pool_key], f"{pool_key} is empty"
        assert letters[pool_key], f"{pool_key} letter pool is empty"
    orphans = [k for k in bullets if k not in track_registry.TRACK_BULLET_POOL_KEYS.values()]
    assert orphans == [], f"bullet pools no track routes to: {orphans}"


def test_all_eight_tracks_compile_to_a_pdf():
    """Every track x tone really renders. Also catches an unescaped Typst character in a new
    summary or bullet, which fails at compile time and nowhere earlier."""
    for track in track_registry.TRACK_LETTERS:
        for tone in ("conservative", "tech"):
            pdf = resume_engine.compile_resume_pdf("Acme Group, Inc.", track, [0, 1, 2], tone)
            assert isinstance(pdf, bytes) and pdf.startswith(b"%PDF"), f"track={track} tone={tone}"


def test_routing_marker_round_trips_every_track():
    """The card's own marker is the durable copy of Gemini's routing. Its regex was [a-e], so a
    card routed to f/g/h did not match at all and /draft degraded to default routing with no
    error."""
    for track in track_registry.TRACK_LETTERS:
        text = f"\U0001F9ED <code>{track}|tech|0,2,5|3</code>"
        parsed = m._parse_routing_from_card_text(text)
        assert parsed.get("track") == track, f"{track} did not round-trip: {parsed}"
        assert parsed.get("tone_mode") == "tech"
        assert parsed.get("bullet_indices") == [0, 2, 5]
        assert parsed.get("outreach_template_id") == 3


def test_screener_response_accepts_every_registry_track():
    """track was a Literal["a".."e"], so Gemini answering "f " or "logistics" raised a
    ValidationError and cost the whole card rather than one routing decision."""
    from response_schema import GeminiJobScreenerResponse as Screener

    for track in track_registry.TRACK_LETTERS:
        assert Screener(score=70, track=track).track == track
    assert Screener(score=70, track="F ").track == "f"
    assert Screener(score=70, track="logistics").track == "f"
    assert Screener(score=70, track="zzz").track == track_registry.DEFAULT_TRACK
    assert Screener(score=70, track=None).track == track_registry.DEFAULT_TRACK
    # Whatever comes back must resolve to a real pool - that is the whole contract.
    for value in ("a", "h", "F ", "zzz", None, 7):
        assert track_registry.pool_key_for(Screener(score=70, track=value).track)


def test_title_override_routes_automation_to_h_and_bi_to_d():
    ov = track_registry.override_track_for_title
    assert ov("a", "RPA Analyst") == "h"
    assert ov("a", "Business Data Analyst") == "d"
    # h is checked before d: automation work with a reporting title word is still h.
    assert ov("a", "Automation & Analytics Analyst") == "h"
    # The existing f/g rules are unchanged.
    assert ov("a", "Carrier Operations Analyst") == "f"
    assert ov("a", "Supply Chain Analyst") == "g"
    # f/g/h are already non-finance and never overridden.
    assert ov("f", "RPA Analyst") == "f"
    assert ov("h", "Data Analyst") == "h"
    # Both finance guards veto the override.
    assert ov("a", "Data Analyst", employer="Flagstar Bank") == "a"
    assert ov("a", "Data Analyst", tone_mode="conservative") == "a"


def test_no_title_rule_routes_to_the_engineering_track_b():
    """Track b is engineering-framed. No title may select it - see the 1-24 rule in the prompt."""
    assert "b" not in {letter for _, letter in track_registry._TITLE_TRACK_RULES}
    for title in ("Data Engineer", "ETL Developer", "Data Pipeline Engineer"):
        assert track_registry.override_track_for_title("a", title) != "b", title


def test_evaluate_job_with_gemini_returns_the_title_overridden_track(monkeypatch):
    """Drive the real choke point: Gemini says track a for an RPA Analyst at a non-finance
    employer; the tuple process_single_candidate persists must carry h."""
    monkeypatch.setattr(m, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: default if default is not None else [])
    monkeypatch.setattr(m, "call_gemini_api", lambda *a, **k: json.dumps(
        {"score": 75, "reason": "fit", "track": "a", "tone_mode": "tech",
         "bullet_indices": [0, 1, 2], "linkedin_template_id": 0, "outreach_template_id": 0}))
    result = m.evaluate_job_with_gemini({"job_title": "RPA Analyst", "employer_name": "Acme Manufacturing",
                                         "job_description": "Automate back-office workflows.",
                                         "job_city": "Detroit"})
    assert result[3] == "h"


# Every job title in Tetiana Cold + Tetiana Warm on 2026-09-26, with the track
# override_track_for_title("a", title) returned the day the h and d title rules landed. Frozen on
# purpose: this catches drift in the title rules, it does not re-derive them. If a rule change moves
# one of these, the diff should be a decision, not a surprise. Distribution at freeze:
# a=58, d=7, f=4, g=7, h=3 (before the h/d rules: a=68, f=4, g=7).
_LIVE_CRM_TITLE_TRACKS = (
    ('Business Technology Analyst - Data Automation and AI', 'h'),
    ('Client Service & Operations Associate', 'a'),
    ('Admin Specialist Tax & Consulting', 'a'),
    ('Customer Support Specialist', 'a'),
    ('Billing Operations Analyst', 'a'),
    ('Services Operations Specialist', 'a'),
    ('Annuity Processing Specialist', 'a'),
    ('Analyst', 'a'),
    ('Revenue Cycle Analytics Process Improvement', 'd'),
    ('Operations Analyst', 'a'),
    ('MP&L MMP Business Process Analyst', 'a'),
    ('Application Systems Analyst', 'a'),
    ('Wealth Operations & Compliance Associate', 'a'),
    ('Financial Analyst', 'a'),
    ('IT Business Systems Analyst', 'a'),
    ('Client Services Associate', 'a'),
    ('Data Analyst Growth Marketing', 'd'),
    ('Wealth Management Client Service Associate', 'a'),
    ('EHR Clinical Analyst', 'a'),
    ('Wealth Management Client Associate', 'a'),
    ('Marketing Operations', 'a'),
    ('Operations Support Analyst', 'a'),
    ('Associate Operations Business Analyst - Surety', 'a'),
    ('Entry Level Client Onboarding Specialist', 'a'),
    ('Business Operations Specialist', 'a'),
    ('Regional Partner Operations Analyst', 'a'),
    ('Life Insurance Specialist', 'a'),
    ('Client Onboarding and Operations Specialist', 'a'),
    ('EFM - Analyst Accounting Operations', 'a'),
    ('Core Business Analyst', 'a'),
    ('Hybrid Operations & Analytics Associate', 'd'),
    ('Jr. Operations Specialist - IRA', 'a'),
    ('Service Operations Specialist', 'a'),
    ('Revenue Operations Systems Administrator', 'h'),
    ('Client Service Associate - Wealth Management', 'a'),
    ('Wealth Advisor Assistant', 'a'),
    ('Supply Chain Analyst - Data-Driven Optimization', 'g'),
    ('Foreign Trade Intern', 'a'),
    ('Global Trade Data Lt Intern', 'a'),
    ('Financial Analyst (Hybrid)', 'a'),
    ('Payments Implementation Specialist', 'a'),
    ('Operations Compliance Analyst', 'a'),
    ('Data Analyst', 'd'),
    ('Operations Specialist - Real Estate', 'a'),
    ('Consultant - Business Analyst', 'a'),
    ('Sea Logistics Revenue Specialist 1', 'f'),
    ('Product & Partnership Operations Specialist', 'a'),
    ('Finance & Functional Analyst', 'a'),
    ('Workforce Analyst', 'a'),
    ('Human Resources Information System Analyst', 'a'),
    ('IT Operations Analyst', 'a'),
    ('Systems Business Analyst - Supply Chain Co-Op 2026', 'g'),
    ('Customs Compliance Analyst', 'a'),
    ('Electronic Data Interchange Coordinator', 'a'),
    ('Supply Chain Analyst', 'g'),
    ('PC Renewal Operations Analyst', 'a'),
    ('Value Stream Mapping Operations Analyst', 'a'),
    ('Senior Portfolio Analyst', 'a'),
    ('Carrier Operations Analyst', 'f'),
    ('Business Systems Analyst', 'a'),
    ('Client Relationship Assistant', 'a'),
    ('Treasure Analyst', 'a'),
    ('Business Efficiency Continuous Improvement Associate Consultant', 'a'),
    ('Remote Data Analyst - Revenue Ops', 'd'),
    ('Baseball Analytics Associate', 'd'),
    ('Change Control Analyst', 'a'),
    ('NASCO/FACETS Systems Business Analyst', 'a'),
    ('Business Data Analyst', 'd'),
    ('Logistics Operations Specialist', 'f'),
    ('Strategic Sourcing Analyst - Capital', 'g'),
    ('RPA Analyst', 'h'),
    ('Strategic Sourcing Analyst: Data Insight & Contracts', 'g'),
    ('US E-Consulting Services - Retirement and Wealth Provider Solutions Analyst', 'a'),
    ('Supply Chain Inventory Analyst', 'g'),
    ('Supply Chain Operations Analyst', 'g'),
    ('Business Systems Analyst Patent Office', 'a'),
    ('Junior Business Systems Analyst: IT & Process Improvement', 'a'),
    ('Operations Specialist: On-Time Production', 'a'),
    ('Automotive Logistics Ops Specialist', 'f'),
)


def test_live_crm_titles_route_to_their_frozen_tracks():
    assert len(_LIVE_CRM_TITLE_TRACKS) == 79
    drift = [(title, expected, track_registry.override_track_for_title("a", title))
             for title, expected in _LIVE_CRM_TITLE_TRACKS
             if track_registry.override_track_for_title("a", title) != expected]
    assert drift == [], f"title routing drifted (title, frozen, now): {drift}"


def test_engineer_titles_never_route_to_track_b():
    """Track b is engineering-framed; Kevin is an operations person who builds his own tools. The
    screener scores these titles 1-24, and no title rule may hand one an engineering resume -
    from any starting track, under either tone."""
    for title in ("Data Engineer", "Software Engineer", "Salesforce Developer", "DevOps Engineer",
                  "Solutions Architect", "ETL Developer", "Data Pipeline Engineer"):
        for start in track_registry.TRACK_LETTERS:
            for tone in ("tech", "conservative", None):
                routed = track_registry.override_track_for_title(start, title, tone_mode=tone)
                assert routed == start or routed != "b", (title, start, tone, routed)


def test_every_track_lands_in_both_the_bullet_and_cover_letter_banks():
    """A track must not half-land: its pool key needs real content in BOTH banks. Read from disk
    rather than through the loaders, whose fallbacks would hide a missing pool."""
    root = os.path.dirname(os.path.abspath(m.__file__))
    with open(os.path.join(root, "resume_bullets_bank.json"), encoding="utf-8") as f:
        bullets = json.load(f)
    with open(os.path.join(root, "templates", "cover_letter_templates.json"), encoding="utf-8") as f:
        letters = json.load(f)
    assert len(track_registry.TRACK_LETTERS) == 8
    for letter in track_registry.TRACK_LETTERS:
        key = track_registry.pool_key_for(letter)
        assert isinstance(bullets.get(key), list) and len(bullets[key]) >= 15, (letter, key)
        assert isinstance(letters.get(key), list) and len(letters[key]) >= 4, (letter, key)


def test_normalize_track_degrades_garbage_to_the_default():
    assert track_registry.normalize_track("f ") == "f"
    for junk in ("", None, "zzz", 7, "  ", "track b please"):
        assert track_registry.normalize_track(junk) == track_registry.DEFAULT_TRACK, junk


def test_skills_footers_only_name_banked_systems():
    """resume_engine's own docstring says every skill named in a footer must already exist in
    evidence_bank's technical_skills. Nothing enforced that until now, which is how a non-finance
    track could quietly start claiming TMS, WMS or SAP - tools Kevin has never used."""
    banked = [s.lower() for s in m.load_evidence_bank().get("technical_skills", [])]
    unbanked = []
    for letter, data in track_registry.TRACK_REGISTRY.items():
        for _label, desc in data["skills"]:
            for item in (i.strip().rstrip(".") for i in desc.split(",")):
                if item in track_registry.CAPABILITY_TERMS:
                    continue
                if any(skill in item.lower() for skill in banked):
                    continue
                unbanked.append(f"track {letter}: {item!r}")
    assert unbanked == [], f"footer names a system not in evidence_bank: {unbanked}"


def test_edit_addresses_every_new_pool():
    """/edit slot codes were T[A-E]; TF0 and TH14 returned None, so the three new pools were
    unreachable from Telegram."""
    for letter, pool_key in track_registry.TRACK_BULLET_POOL_KEYS.items():
        target = m.resolve_edit_target(f"T{letter.upper()}0")
        assert target is not None, f"T{letter.upper()}0 does not resolve"
        assert target[1] == pool_key
    assert m.resolve_edit_target("TF0")[1] == "track_f_operations_logistics"
    assert m.resolve_edit_target("TH14")[1:] == ("track_h_technical_systems", 14)
    # An unknown letter must stay unknown rather than silently editing track a.
    assert m.resolve_edit_target("TI0") is None
    assert m.resolve_edit_target("TZ3") is None


def test_cover_letter_renders_for_every_track():
    """Every registry track assembles a well-formed letter from its own pool, including f/g/h."""
    for track in track_registry.TRACK_LETTERS:
        for tone in ("conservative", "tech"):
            letter = m.generate_cover_letter("Acme Group, Inc.", "Operations Analyst",
                                             track, 0, "Detroit, MI", tone)
            ctx = f"track={track} tone={tone}"
            assert letter.startswith("Dear Acme Group Hiring Team,"), ctx
            assert letter.endswith("\n\nBest regards,\nKevin Miller"), ctx
            assert "{" not in letter and "}" not in letter, ctx


# ---------------------------------------------------------------------------------------------
# Source-attributed routed bullets. Before these, a routed bullet always rendered under job 0,
# so track g claimed Kevin cross-referenced payroll across 70+ manufacturing plants as a Wealth
# Operations Specialist at a Detroit wealth firm in 2026. That work is ABC Technologies, 2024.
# ---------------------------------------------------------------------------------------------

def _experience_blocks(markup):
    """The rendered Professional Experience section as {company: [bullet, ...]}.

    Parsed out of the real Typst markup rather than from a renderer return value, because what
    matters is which employer a claim is printed under - per CLAUDE.md, the write path.
    """
    blocks, current = {}, None
    for line in markup.splitlines():
        header = re.match(r"^\*[^*]+\* \| ([^#]+?) #h\(1fr\)", line)
        if header:
            current = header.group(1).strip()
            blocks[current] = []
        elif line.startswith("#line(") and current:
            current = None
        elif line.startswith("- ") and current:
            blocks[current].append(line[2:])
    return blocks


def test_tagged_bullet_renders_under_its_own_employer():
    """THE defect. track_g[0] is ABC Technologies' work (evidence_bank.json:66, the 70+
    manufacturing facilities checklist), and it has to print under ABC's heading, not Signal's."""
    bank = resume_engine.load_resume_bullets_bank()
    entry = bank["track_g_supply_chain"][0]
    text = resume_engine.bullet_text(entry)
    assert resume_engine.bullet_source_job(entry, m.load_evidence_bank()) == 3

    markup = resume_engine.render_typst_markup("Acme Manufacturing", "g", [0, 1, 2, 3], "tech")
    blocks = _experience_blocks(markup)
    assert text in blocks["ABC Technologies"], "the 70+ plants bullet is not under ABC"
    assert text not in blocks["Signal Advisors"], "the 70+ plants bullet still claims Signal's job"
    # Signal must still carry content - an employer heading with nothing under it looks like a bug.
    assert blocks["Signal Advisors"], "Signal Advisors rendered as a bare heading"


def test_untagged_bullet_still_renders_under_job_zero():
    """110 of the 120 entries are bare strings and must be completely unaffected."""
    bank = resume_engine.load_resume_bullets_bank()
    entry = bank["track_a_wealth_ops"][0]
    assert isinstance(entry, str), "this test needs an untagged entry"
    blocks = _experience_blocks(resume_engine.render_typst_markup("Acme", "a", [0, 1, 2], "conservative"))
    assert entry in blocks["Signal Advisors"]


def test_bullet_helpers_never_raise_on_a_malformed_entry():
    """/edit writes a bare string back into the bank from Kevin's phone, so these helpers see
    whatever a hand edit produces. A loader that raised would take the resume renderer down at the
    moment he is trying to apply to something."""
    evidence = m.load_evidence_bank()
    n_jobs = len(evidence["experience"])
    junk = [None, 42, 3.5, [], {}, True, {"text": None}, {"text": ["a"]},
            {"source_job": 2}, {"text": "x", "source_job": "3"}, {"text": "x", "source_job": True},
            {"text": "x", "source_job": 1.0}, {"text": "x", "source_job": -1},
            {"text": "x", "source_job": n_jobs}, {"text": "x", "source_job": 999},
            {"text": "x", "source_job": 3, "replaces": "1"},
            {"text": "x", "source_job": 3, "replaces": 99},
            {"text": "x", "source_job": 3, "replaces": -2}]
    for entry in junk:
        assert isinstance(resume_engine.bullet_text(entry), str), entry
        src = resume_engine.bullet_source_job(entry, evidence)
        assert isinstance(src, int) and 0 <= src < n_jobs, entry
        sub = resume_engine.bullet_replaces_static(entry, evidence)
        assert isinstance(sub, int) and sub >= -1, entry
    # A bare string is the pre-tag contract and means Signal Advisors, forever.
    assert resume_engine.bullet_source_job("some bullet", evidence) == 0
    assert resume_engine.bullet_text("some bullet") == "some bullet"


def test_no_pool_bullet_duplicates_a_project_bullet():
    """_bullets_of_other_jobs covered experience[1:] only, so a pool bullet matching a PROJECT
    bullet was unguarded and rendered twice on one page under two headings. track_h[4] was live at
    0.8067 before it was reworded. Measured zero now - this exists so the class cannot recur."""
    evidence = m.load_evidence_bank()
    bank = resume_engine.load_resume_bullets_bank()
    project_bullets = [b for p in evidence.get("projects", []) for b in p.get("bullets", [])]
    assert project_bullets, "no project bullets to guard against"
    # The guard must actually LOOK at them. Measuring ratios here by hand passes either way, which
    # let a mutation that dropped projects from _duplicate_check_targets go undetected.
    for src in range(len(evidence["experience"])):
        targets = resume_engine._duplicate_check_targets(evidence, src)
        for pb in project_bullets:
            assert pb in targets, f"project bullet is unguarded for source_job {src}"
    hits = []
    for pool, bullets in bank.items():
        for i, entry in enumerate(bullets):
            text = resume_engine.bullet_text(entry).lower()
            for pb in project_bullets:
                ratio = difflib.SequenceMatcher(None, text, pb.lower()).ratio()
                if ratio >= resume_engine._DUPLICATE_BULLET_RATIO:
                    hits.append(f"{pool}[{i}] ratio={ratio:.4f}")
    assert hits == [], f"these render twice on one page: {hits}"


def test_correctly_attributed_bullet_is_not_dropped_as_a_duplicate():
    """The guard had to become source-aware in both directions at once.

    A bullet tagged to ABC that resembles ABC's own statics is attribution working, so it must
    survive. The SAME text tagged to Signal is the original defect and must be dropped.
    """
    evidence = m.load_evidence_bank()
    abc_static = evidence["experience"][3]["bullets"][1]
    tagged = {"text": abc_static, "source_job": 3}
    untagged = abc_static

    assert not resume_engine._is_duplicate_of_other_job(
        resume_engine.bullet_text(tagged),
        resume_engine._duplicate_check_targets(
            evidence, resume_engine.bullet_source_job(tagged, evidence))), \
        "a correctly attributed ABC bullet was dropped for matching ABC's own statics"
    assert resume_engine._is_duplicate_of_other_job(
        resume_engine.bullet_text(untagged),
        resume_engine._duplicate_check_targets(
            evidence, resume_engine.bullet_source_job(untagged, evidence))), \
        "the same text claiming Signal's job was NOT dropped"


def test_a_routed_bullet_never_sits_beside_the_static_it_restates():
    """Appending under the right employer created a second defect: the pool bullet is usually a
    rephrasing of one of that employer's statics, so ABC printed the checklist claim twice in two
    wordings. `replaces` makes the substitution explicit, since similarity cannot decide it -
    same-claim pairs measure 0.52-0.72 against their own statics and different claims 0.41-0.45."""
    evidence = m.load_evidence_bank()
    bank = resume_engine.load_resume_bullets_bank()
    checked = 0
    for pool, bullets in bank.items():
        for i, entry in enumerate(bullets):
            sub = resume_engine.bullet_replaces_static(entry, evidence)
            if sub < 0:
                continue
            src = resume_engine.bullet_source_job(entry, evidence)
            assert src > 0, f"{pool}[{i}] replaces a static of job 0, whose statics never render"
            company = evidence["experience"][src]["company"]
            superseded = evidence["experience"][src]["bullets"][sub]
            track = pool.split("_")[1]
            blocks = _experience_blocks(
                resume_engine.render_typst_markup("Acme", track, [i, 0, 1], "tech"))
            rendered = blocks[company]
            assert resume_engine.bullet_text(entry) in rendered, f"{pool}[{i}] did not render"
            assert superseded not in rendered, f"{pool}[{i}] rendered beside the static it restates"
            checked += 1
    assert checked == 10, f"expected 10 authored substitutions, found {checked}"


def test_every_track_still_compiles_to_one_page():
    """The regression source tags could plausibly cause. Redistributing bullets across employers
    adds lines only if a job keeps its statics AND gains a bullet, which `replaces` prevents - so
    the total is never higher than before. Asserted on the real PAGE COUNT, not on byte length,
    because a two-page resume still produces plausible-looking bytes."""
    from pypdf import PdfReader
    for track in track_registry.TRACK_LETTERS:
        for tone in ("conservative", "tech"):
            pdf = resume_engine.compile_resume_pdf("Acme Group, Inc.", track, [0, 1, 2, 3], tone)
            pages = len(PdfReader(io.BytesIO(pdf)).pages)
            assert pages == 1, f"track={track} tone={tone} rendered {pages} pages"


def test_worst_case_tag_distribution_still_fits_one_page():
    """The pathological routing the suite would otherwise never reach: every routed index tagged
    away from job 0, so Signal falls back to statics while a later job carries extra bullets."""
    from pypdf import PdfReader
    tagged_only = {"g": [0, 4, 6], "d": [1, 4, 8, 10], "c": [2, 10, 12]}
    for track, indices in tagged_only.items():
        for tone in ("conservative", "tech"):
            markup = resume_engine.render_typst_markup("Acme", track, indices, tone)
            blocks = _experience_blocks(markup)
            assert blocks["Signal Advisors"], "Signal rendered empty when every bullet moved away"
            pdf = resume_engine.compile_resume_pdf("Acme", track, indices, tone)
            pages = len(PdfReader(io.BytesIO(pdf)).pages)
            assert pages == 1, f"track={track} tone={tone} indices={indices} -> {pages} pages"


def test_bare_string_and_tagged_entry_both_survive_a_phone_edit():
    """Drives the real /edit write path (update_template_entry) against a temp copy of the bank and
    reads it back through the renderer. A bare string must stay a bare string, and a tagged entry
    must KEEP its source_job - otherwise one phone edit silently moves the bullet back under Signal
    Advisors and undoes the whole fix."""
    import shutil, tempfile
    original = resume_engine.RESUME_BULLETS_BANK_PATH
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "resume_bullets_bank.json")
        shutil.copy(original, path)

        ok, _ = m.update_template_entry(path, "track_g_supply_chain", 0,
                                        "Reworded the tagged bullet from a phone.")
        assert ok
        ok, _ = m.update_template_entry(path, "track_a_wealth_ops", 0,
                                        "Reworded the bare bullet from a phone.")
        assert ok

        with open(path, encoding="utf-8") as f:
            reloaded = json.load(f)
        evidence = m.load_evidence_bank()
        tagged = reloaded["track_g_supply_chain"][0]
        bare = reloaded["track_a_wealth_ops"][0]
        assert resume_engine.bullet_text(tagged) == "Reworded the tagged bullet from a phone."
        assert resume_engine.bullet_source_job(tagged, evidence) == 3, "the phone edit stripped the tag"
        assert resume_engine.bullet_replaces_static(tagged, evidence) == 1
        assert isinstance(bare, str), "an untagged entry must stay a plain string"

        # And it still renders, under the right employer.
        resume_engine.RESUME_BULLETS_BANK_PATH = path
        try:
            blocks = _experience_blocks(
                resume_engine.render_typst_markup("Acme", "g", [0, 1, 2], "tech"))
        finally:
            resume_engine.RESUME_BULLETS_BANK_PATH = original
        assert "Reworded the tagged bullet from a phone." in blocks["ABC Technologies"]


def test_a_later_job_keeps_its_other_statics_when_a_routed_bullet_lands_on_it():
    """The rule differs by job, and this is the half that is easy to get wrong.

    Job 0 has 14 statics that have never rendered, so routed bullets REPLACE them. Jobs 1-3 have
    exactly 3 each and always render all 3, so a routed bullet APPENDS. Applying job 0's replace
    rule uniformly would delete two of ABC's real claims in order to add one - the page would get
    shorter and quieter, and no existing assertion would notice.
    """
    evidence = m.load_evidence_bank()
    bank = resume_engine.load_resume_bullets_bank()
    entry = bank["track_g_supply_chain"][0]
    src = resume_engine.bullet_source_job(entry, evidence)
    sub = resume_engine.bullet_replaces_static(entry, evidence)
    assert src == 3 and sub == 1

    blocks = _experience_blocks(
        resume_engine.render_typst_markup("Acme", "g", [0, 1, 2, 3], "tech"))
    abc = blocks["ABC Technologies"]
    survivors = [b for i, b in enumerate(evidence["experience"][3]["bullets"]) if i != sub]
    for static in survivors:
        assert static in abc, f"ABC lost a real claim it has always made: {static[:60]}"
    assert resume_engine.bullet_text(entry) in abc
    assert len(abc) == len(evidence["experience"][3]["bullets"]),         "ABC's bullet count moved - append plus one substitution should leave it unchanged"


# ---------------------------------------------------------------------------------------------
# Generic-inbox greeting. Four live sends on 2026-09-24 opened "Hi Rivian," / "Hi GPAC," /
# "Hi Koch," / "Hi UAW," because both job-card writers passed the company POSITIONALLY into
# save_message_mapping's contact_name slot. Three readers took that as proof of a human.
# ---------------------------------------------------------------------------------------------

def test_generic_inbox_greeting_degrades_to_bare_hi():
    """The rendered email a recipient reads - not a helper's return value. A mapping row carrying
    the company in contact_name must still open on a bare "Hi,", which is the right register for
    the shared inbox (operations@rivian.com) these actually go to."""
    for company in ("Rivian", "GPAC", "Koch", "UAW Retiree Medical Benefits Trust"):
        job = {"employer_name": company, "job_title": "Carrier Operations Analyst",
               "outreach_template_id": 1}
        mapping = {"contact_name": company, "contact_company": "", "sheet_tab": "Pipeline_Candidates"}
        body = m.resolve_outreach_body(job, mapping, "Carrier Operations Analyst", company, False)
        assert body.startswith("Hi,\n"), f"{company}: {body.splitlines()[0]!r}"
        first_word = company.split()[0]
        assert not body.startswith(f"Hi {first_word}"), company
    # A real person is untouched - this must not have become "never greet anyone by name".
    job = {"employer_name": "Rivian", "job_title": "Ops Analyst", "outreach_template_id": 1}
    mapping = {"contact_name": "Dana Reyes", "contact_company": "Rivian", "sheet_tab": "Pipeline_Candidates"}
    assert m.resolve_outreach_body(job, mapping, "Ops Analyst", "Rivian", False).startswith("Hi Dana,")


def test_a_job_card_never_records_the_company_as_its_contact_name(monkeypatch, tmp_path):
    """Drives the real writer: send_telegram_card -> save_message_mapping -> SQLite, then reads the
    row back through the same accessor a swipe-reply uses. Asserting on the PERSISTED row, per
    CLAUDE.md - the previous shape passed every test while writing "Rivian" into contact_name."""
    monkeypatch.setattr(m, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **k: None)

    class _Res:
        status_code = 200
        @staticmethod
        def json():
            return {"result": {"message_id": 918273}}

    monkeypatch.setattr(m.requests, "post", lambda *a, **k: _Res())
    job = {"employer_name": "Rivian Automotive, Inc.", "job_title": "Carrier Operations Analyst",
           "job_apply_link": "https://x/y", "track": "f"}
    m.send_telegram_card(job, 83, "operations@rivian.com", "", "$60k", "On-Site", 40,
                         "sk1", sheet_uuid="uuid-rivian-1")

    row = m.get_mapping_from_message_id(918273)
    assert row is not None
    assert row["contact_name"] == "", f"company leaked into contact_name: {row['contact_name']!r}"
    # The company still gets recorded - in its own column, and UNESCAPED.
    assert row["contact_company"] == "Rivian Automotive, Inc."

    # And the email that mapping renders opens on a bare "Hi,".
    body = m.resolve_outreach_body(job, row, "Carrier Operations Analyst", "Rivian Automotive, Inc.", False)
    assert body.startswith("Hi,\n")


def test_a_warm_radar_card_keeps_the_real_contact_name(monkeypatch):
    """The mirror case. send_warm_radar_card had a contact_name parameter and stored the company
    over it, so a genuine warm referral lost its name in the same slot a cold card gained a fake
    one. Both directions have to be right or the greeting is wrong one way or the other."""
    monkeypatch.setattr(m, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(m, "TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(m, "log_metric_event", lambda *a, **k: None)

    class _Res:
        status_code = 200
        @staticmethod
        def json():
            return {"result": {"message_id": 918274}}

    monkeypatch.setattr(m.requests, "post", lambda *a, **k: _Res())
    job = {"employer_name": "Signal Advisors", "job_title": "Ops Analyst", "job_apply_link": "#"}
    m.send_warm_radar_card(job, "Dana Reyes", "Active relationship", "uuid-warm-1")

    row = m.get_mapping_from_message_id(918274)
    assert row["contact_name"] == "Dana Reyes"
    assert row["contact_company"] == "Signal Advisors"


def test_crm_contact_is_a_person_screens_the_employer_but_keeps_real_names():
    """The reader-side guard. sheet_row_map is durable SQLite, so every card sent before the writer
    was fixed still carries a company in contact_name - fixing only the writer would leave every
    card already in Kevin's Telegram greeting "Hi Rivian," forever."""
    assert not m.crm_contact_is_a_person("Rivian", "Rivian")
    assert not m.crm_contact_is_a_person("Rivian", "Rivian Automotive, Inc.")   # suffix-stripped match
    assert not m.crm_contact_is_a_person("Rivian Automotive, Inc.", "Rivian")   # and the reverse
    assert not m.crm_contact_is_a_person("UAW Retiree Medical Benefits Trust",
                                         "UAW Retiree Medical Benefits Trust")
    assert not m.crm_contact_is_a_person("", "Rivian")
    assert m.crm_contact_is_a_person("Dana Reyes", "Rivian")
    assert m.crm_contact_is_a_person("Dana Reyes", "")          # no company to compare against
    # A person whose surname happens to be the company is still a person - only a leading-fragment
    # match counts, so "Dana Rivian" is not screened out.
    assert m.crm_contact_is_a_person("Dana Rivian", "Rivian")


def test_apply_on_a_job_card_is_not_recorded_as_a_warm_application(monkeypatch):
    """The channel-attribution half of the company-as-contact-name bug.

    /apply chose outreach_path on `mapping.get("contact_name")` being truthy. Job cards stored the
    company there, so EVERY portal application was recorded "warm" - the one measurement the
    three-way split exists to make, reading 100% warm regardless of what Kevin did. Drives the real
    /apply handler and asserts on the kwargs that reach record_application_outcome.
    """
    outcomes = []
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-path", "sheet_tab": "Tetiana Cold",
        # exactly what a card written before the fix left behind
        "contact_name": "Rivian", "contact_company": ""})
    monkeypatch.setattr(m, "get_job_by_sheet_uuid", lambda u: {
        "job_title": "Carrier Operations Analyst", "employer_name": "Rivian",
        "job_id": "gh_x", "target_email": "operations@rivian.com [⚠️ Fallback Email]"})
    for name in ("send_telegram_message", "edit_telegram_message", "log_metric_event",
                 "log_daily_activity", "add_company_cooldown", "upsert_company_identity",
                 "enqueue_crm_payload"):
        monkeypatch.setattr(m, name, lambda *a, **k: None)
    monkeypatch.setattr(m, "record_application_outcome",
                        lambda uuid_val, status, **kw: outcomes.append(kw) or True)

    _dispatch("/apply")

    assert len(outcomes) == 1
    assert outcomes[0]["outreach_path"] == "ats", outcomes[0]
    assert outcomes[0]["outreach_path"] != "warm"


def test_apply_still_records_warm_for_a_real_named_contact(monkeypatch):
    """The other direction: a genuine named person must still count as warm, or the fix has just
    moved the measurement error somewhere else."""
    outcomes = []
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-path2", "sheet_tab": "Tetiana Cold",
        "contact_name": "Dana Reyes", "contact_company": "Rivian"})
    monkeypatch.setattr(m, "get_job_by_sheet_uuid", lambda u: {
        "job_title": "Ops Analyst", "employer_name": "Rivian", "job_id": "gh_x",
        "target_email": "dana@rivian.com"})
    for name in ("send_telegram_message", "edit_telegram_message", "log_metric_event",
                 "log_daily_activity", "add_company_cooldown", "upsert_company_identity",
                 "enqueue_crm_payload"):
        monkeypatch.setattr(m, name, lambda *a, **k: None)
    monkeypatch.setattr(m, "record_application_outcome",
                        lambda uuid_val, status, **kw: outcomes.append(kw) or True)

    _dispatch("/apply")

    assert outcomes[0]["outreach_path"] == "warm", outcomes[0]


def test_draft_does_not_spend_provider_credits_resolving_the_company_as_a_person(monkeypatch):
    """/draft branched on a truthy contact_name to decide "named CRM contact - run the email
    waterfall". With the company sitting in that field, every job card ran a paid multi-provider
    lookup for a person called "Rivian". Asserts the waterfall is never entered."""
    calls = []
    monkeypatch.setattr(m, "resolve_reply_mapping", lambda msg, chat_id, label: {
        "sheet_uuid": "uuid-draft", "sheet_tab": "Tetiana Cold",
        "contact_name": "Rivian", "contact_company": ""})
    monkeypatch.setattr(m, "get_job_by_sheet_uuid", lambda u: {
        "job_title": "Carrier Operations Analyst", "employer_name": "Rivian",
        "outreach_template_id": 1, "track": "f", "bullet_indices": [0, 1, 2]})
    monkeypatch.setattr(m, "resolve_email_waterfall",
                        lambda *a, **k: calls.append(a) or "rivian@rivian.com")
    monkeypatch.setattr(m, "resolve_target_email", lambda *a, **k: "operations@rivian.com")
    monkeypatch.setattr(m, "compile_resume_pdf_resilient", lambda *a, **k: b"%PDF-")
    monkeypatch.setattr(m, "create_gmail_draft", lambda **k: (True, "ok", "d1"))
    bodies = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, **k: bodies.append(t))
    for name in ("log_email_enrichment_attempt", "log_daily_activity", "increment_api_usage_counter",
                 "send_telegram_document"):
        monkeypatch.setattr(m, name, lambda *a, **k: None, raising=False)

    _dispatch("/draft")

    assert calls == [], f"the email waterfall ran for a company name: {calls}"
    # And the drafted body is addressed to nobody in particular, which is correct for a shared inbox.
    assert any("Hi," in b for b in bodies), bodies
    assert not any("Hi Rivian" in b for b in bodies), bodies


# ---------------------------------------------------------------------------------------------
# Routing: the email's self-description bound to the resume track, and a title-pattern override
# for the non-finance roles Gemini kept filing under a finance persona. Six of six live sends on
# 2026-09-24 described Kevin as a custodial-reconciliation person whatever the role was.
# ---------------------------------------------------------------------------------------------

def _load_outreach_bank():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "outreach_templates.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _screen(monkeypatch, title, employer, track, tone_mode="tech", outreach_template_id=1,
            description="Operations role.", score=80):
    """Drive the REAL evaluate_job_with_gemini with a canned Gemini payload, then the REAL
    process_single_candidate, and hand back the job dict as it was persisted. Per CLAUDE.md the
    assertion target is what the next run reads back, not the screener's return value."""
    payload = json.dumps({
        "score": score, "reason": "fit", "track": track, "tone_mode": tone_mode,
        "bullet_indices": [0, 1, 2], "linkedin_template_id": 0,
        "outreach_template_id": outreach_template_id,
    })
    monkeypatch.setattr(m, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(m, "call_gemini_api", lambda prompt, system: payload)
    monkeypatch.setattr(m, "get_filter", lambda key, default=None: default if default is not None else [])
    monkeypatch.setattr(m, "resolve_live_alumni_at_company", lambda *a, **k: None)
    monkeypatch.setattr(m, "get_warm_crm_contacts", lambda: {})
    monkeypatch.setattr(m, "get_ghost_listing_penalty", lambda job_hash: (0, ""))
    monkeypatch.setattr(m, "resolve_target_email", lambda *a, **k: "ops@example.com")
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda payload: True)
    monkeypatch.setattr(m, "record_jd_terms", lambda *a, **k: None, raising=False)
    result = m.process_single_candidate({
        "job_title": title, "employer_name": employer, "job_id": f"gh_{title[:6]}",
        "job_description": description, "job_city": "Detroit",
    })
    assert result is not None, "the canned score should have passed the gate"
    return result


def test_carrier_operations_title_routes_to_f(monkeypatch):
    """Rivian, a truck manufacturer, hiring a Carrier Operations Analyst. Gemini returned track e
    and a custodial-reconciliation email. Track f exists for exactly this and was not used."""
    result = _screen(monkeypatch, "Carrier Operations Analyst", "Rivian", track="e",
                     description="Manage carrier relationships, freight tendering and dispatch exceptions.")
    assert result["job"]["track"] == "f", result["job"]["track"]
    # And the email that ships with it must be one of f's, not the custodial one Gemini asked for.
    assert result["job"]["outreach_template_id"] in track_registry.allowed_outreach_template_ids("f")
    assert "custodial" not in result["outreach_email"].lower()


def test_strategic_sourcing_title_routes_to_g(monkeypatch):
    """Trinity Health, a hospital system, Strategic Sourcing Analyst - procurement, routed to e."""
    result = _screen(monkeypatch, "Strategic Sourcing Analyst", "Trinity Health", track="e",
                     description="Strategic sourcing and supplier negotiation across the system.")
    assert result["job"]["track"] == "g", result["job"]["track"]
    assert "custodial" not in result["outreach_email"].lower()


def test_a_finance_employer_is_never_overridden_by_a_title_pattern(monkeypatch):
    """The guard on the override. "Strategic Sourcing Analyst" at a bank is procurement AT A BANK,
    and a multi-site manufacturing resume is the wrong document for it - overriding there would
    trade one misroute for another. Two independent signals, tested separately below."""
    # (1) the employer's name says financial services, even though tone_mode says tech
    kept = _screen(monkeypatch, "Strategic Sourcing Analyst", "Comerica Bank", track="e",
                   tone_mode="tech", description="Sourcing for the bank's vendor programs.")
    assert kept["job"]["track"] == "e", "a bank was overridden onto a manufacturing track"
    # (2) Gemini's own read that the employer's business is financial services
    kept2 = _screen(monkeypatch, "Freight Operations Analyst", "Opaque Holdings", track="a",
                    tone_mode="conservative")
    assert kept2["job"]["track"] == "a"


def test_an_already_non_finance_track_is_left_alone(monkeypatch):
    """f, g and h are already the non-finance tracks. Gemini read the description and this has not,
    so its choice among them outranks a title regex - g must not be dragged to f by "freight"."""
    result = _screen(monkeypatch, "Freight Data Analyst", "Rivian", track="g")
    assert result["job"]["track"] == "g"


def test_logistics_track_never_sends_a_custodial_email(monkeypatch):
    """The whole point. Whatever cold_ops id Gemini routes, a track f/g/h send must not describe
    Kevin through a financial-services lens - it is the resume's own argument that gets contradicted."""
    banned = ("custodial", "broker dealer", "broker-dealer", "fiduciary", "brokerage")
    for track in ("f", "g", "h"):
        for gemini_id in range(8):
            body = m.generate_cold_email(
                "Operations Analyst", "Rivian",
                template_id=track_registry.coerce_outreach_template_id(track, gemini_id))
            low = body.lower()
            for word in banned:
                assert word not in low, f"track {track}, gemini id {gemini_id}: {word!r} in {body!r}"


def test_every_track_allows_at_least_one_email_id():
    """An empty allowed set would make coerce_outreach_template_id raise on allowed[0] - on the
    screening path, where an exception costs the whole card."""
    cold = _load_outreach_bank()["cold_ops"]
    for track in track_registry.TRACK_LETTERS:
        allowed = track_registry.allowed_outreach_template_ids(track)
        assert allowed, track
        assert all(0 <= i < len(cold) for i in allowed), f"{track} names a cold_ops index that does not exist"
        assert len(set(allowed)) == len(allowed), f"{track} lists a duplicate"
    # An unknown track must still resolve rather than raising.
    assert track_registry.allowed_outreach_template_ids("zzz")


def test_gemini_id_outside_the_allowed_set_snaps_to_the_track_default():
    for track in track_registry.TRACK_LETTERS:
        allowed = track_registry.allowed_outreach_template_ids(track)
        for legal in allowed:
            # a legal pick is HONORED - Gemini read the description and this did not
            assert track_registry.coerce_outreach_template_id(track, legal) == legal, (track, legal)
        for illegal in [i for i in range(8) if i not in allowed]:
            assert track_registry.coerce_outreach_template_id(track, illegal) == allowed[0], (track, illegal)
        # total on junk, because this runs inside the screening path
        for junk in (None, "3", 3.0, True, -1, 99, [], {}):
            assert track_registry.coerce_outreach_template_id(track, junk) == allowed[0], (track, junk)


def test_the_snapped_email_id_is_what_gets_persisted(monkeypatch):
    """Not the coercion function's return value - the id written onto the cached job, which is what
    /draft, /e and /stage re-render from on every later pass."""
    result = _screen(monkeypatch, "Carrier Operations Analyst", "Rivian", track="f",
                     outreach_template_id=1)   # cold_ops[1] is the custodial one, illegal for f
    assert result["job"]["outreach_template_id"] == track_registry.allowed_outreach_template_ids("f")[0]
    assert result["job"]["outreach_template_id"] == 6


def test_the_prompt_names_a_concrete_trigger_for_every_non_finance_track():
    """The prompt gave Gemini a bare list of eight labels, and "operations" appears in both e and
    f, so the labels alone could not separate them."""
    prompt = m.build_system_prompt()
    assert "{" not in track_registry.TRACK_TRIGGER_GUIDANCE  # nothing to interpolate, so nothing to leak
    assert track_registry.TRACK_TRIGGER_GUIDANCE in prompt
    for trigger in ("carrier", "freight", "dispatch", "supply chain", "procurement",
                    "strategic sourcing", "plant", "automation", "integrations"):
        assert trigger in prompt.lower(), trigger
    # The tone_mode line used to define the axis only in finance terms, so a manufacturer matched
    # neither branch of the one instruction sitting closest to the output format.
    tone_line = [l for l in prompt.splitlines() if l.startswith('"tone_mode"')][0]
    assert "manufactur" in tone_line.lower(), tone_line


# --- Inbound sender screen (2026-09-26) -----------------------------------------------------------
# Kevin: "only have messages that are important for the hiring process / important people I've had
# calls with come through Telegram". Both halves: the junk stops, and a cold recruiter still alerts.
_JUNK_OFF_KEVINS_PHONE = [
    ("invoice+statements@mail.anthropic.com", "Your receipt from Anthropic, PBC"),
    ("community@legal.io", "Kevin, nearly half of lawyers feel worse off than a year ago"),
    ("discover@services.discover.com", "We've received your payment"),
    ("discover@card-e.em.discover.com", "Reminder: Prepare to manage your account with Capital One"),
    ("support@email.career.io", "Welcome to Career.io!"),
    ("system@successfactors.com",
     "Welcome and thank you for creating your account with Dana Incorporated!"),
    ("indeedapply@indeed.com", "Indeed Application: Data Analyst"),
    ("ejko.fa.sender.2@workflow.mail.us2.cloud.oracle.com",
     "Your recent job application for Business Efficiency Analyst"),
]
_JUNK_BODY = "Thanks for being with us. This message was sent to you about your account activity."


def test_all_eight_junk_senders_reach_the_tray_but_not_telegram(monkeypatch):
    """WRITE PATH: drives the real poller against the real SQLite tray and reads it back the way
    /inbox does. Screened means 'no Telegram', not 'discarded'."""
    _clear_tray()
    messages = [_gmail_message(f"junk{i}", sender, subject, _JUNK_BODY)
                for i, (sender, subject) in enumerate(_JUNK_OFF_KEVINS_PHONE)]
    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, messages, real_tray=True)

    assert alerts == []
    assert sorted(marked_read) == sorted(f"junk{i}" for i in range(8))
    tray = {t["thread_id"]: t for t in m.get_open_inbound_threads()}
    assert sorted(tray) == sorted(f"thread-junk{i}" for i in range(8))
    assert all(t["match_reason"].startswith("screened: ") for t in tray.values())
    _clear_tray()


def test_a_cold_recruiter_with_no_interview_vocabulary_still_alerts(monkeypatch):
    """Not in the CRM, a new thread, and nothing in the text that looks like hiring. If any rule
    screens this, the rule is wrong."""
    _clear_tray()
    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("sarah", "Sarah Chen <sarah.chen@sanctuarywealth.com>", "Re: Kevin Miller",
                       "Got your note, passing this to our team.")], real_tray=True)
    assert len(alerts) == 1
    assert "sarah.chen@sanctuarywealth.com" in alerts[0]
    assert marked_read == ["sarah"]
    assert [t["match_reason"] for t in m.get_open_inbound_threads()] == ["unknown sender"]
    _clear_tray()


def test_a_verified_crm_contact_on_an_automated_address_still_alerts(monkeypatch):
    contact = {"name": "Dana", "company": "Career.io", "tab": "Carmen Cold", "sheet_uuid": ""}
    monkeypatch.setattr(m, "record_application_outcome", lambda *a, **k: None)
    monkeypatch.setattr(m, "route_inbound_reply_to_crm", lambda *a, **k: None)
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("crm", "support@email.career.io", "Following up on our call",
                       "Great talking yesterday - sending the details we discussed.")],
        crm_lookup=lambda sender: contact)
    assert len(alerts) == 1
    assert "New Gmail Reply" in alerts[0]


def test_a_thread_kevin_started_is_never_screened(monkeypatch):
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("thr", "discover@card-e.em.discover.com", "Re: intro",
                       "Looping you in with the hiring manager here.")], thread_started=True)
    assert len(alerts) == 1
    assert "thread participant" in alerts[0]


def test_a_domain_match_does_not_exempt_a_receipt(monkeypatch):
    """invoice@mail.anthropic.com resolving to a tracked Anthropic role is still a receipt."""
    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("rcpt", "invoice+statements@mail.anthropic.com",
                       "Your receipt from Anthropic, PBC", _JUNK_BODY)],
        domain_match=lambda sender: {"name": "Anthropic", "company": "Anthropic",
                                     "tab": "Carmen Cold", "sheet_uuid": ""})
    assert alerts == []
    assert marked_read == ["rcpt"]


def test_tier1_from_an_unknown_sender_still_alerts_under_strict(monkeypatch):
    assert m.INBOUND_ALERT_MODE == "strict"
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("fit", FITTERMAN_SENDER, FITTERMAN_SUBJECT, FITTERMAN_SNIPPET,
                       ics=FITTERMAN_ICS, age_seconds=3600),
        _gmail_message("stem", STEMLER_SENDER, STEMLER_SUBJECT, STEMLER_SNIPPET, age_seconds=3600)])
    assert len(alerts) == 2
    assert all("No CRM changes were made" in a for a in alerts)


def test_an_ats_rejection_still_alerts_and_ats_account_setup_does_not(monkeypatch):
    """noreply@myworkday.com is automated, so it never gets Tier 1 - its only route to Telegram is
    the hiring-verdict carve-out. A rejection keeps it; a welcome email does not."""
    monkeypatch.setattr(m, "route_rejection_to_died", lambda *a, **k: "")
    alerts, marked_read = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("rej", "Workday <noreply@myworkday.com>", "Update on your application",
                       "Unfortunately, we have decided to move forward with other candidates."),
        _gmail_message("wel", "Workday <noreply@myworkday.com>", "Welcome to our careers site",
                       "Thanks for creating your candidate account."),
    ])
    assert len(alerts) == 1
    assert "Rejection" in alerts[0]
    assert sorted(marked_read) == ["rej", "wel"]


def test_permissive_mode_restores_alerting_every_stranger(monkeypatch):
    monkeypatch.setattr(m, "INBOUND_ALERT_MODE", "permissive")
    alerts, _ = _run_poll_with_fake_gmail(monkeypatch, [
        _gmail_message("perm", "community@legal.io", "Kevin, nearly half of lawyers feel worse off",
                       _JUNK_BODY)])
    assert len(alerts) == 1


# ---- Job-link sweep: per-tab budget, opaque split, standup reasons ----

def _linkcheck_env_by_tab(monkeypatch, rows_by_tab, fetches):
    class _R:
        status_code = 200
        def __init__(self, rows): self.rows = rows
        def json(self): return {"status": "success", "followups": self.rows}
    monkeypatch.setattr(m, "CRM_WEBHOOK_URL", "https://script.google.com/fake")
    monkeypatch.setattr(m, "crm_post", lambda payload, **kw: _R(rows_by_tab.get(payload.get("tab"), [])))
    fetched = []
    def _fetch(url):
        fetched.append(url)
        return fetches.get(url, (200, url, "<p>Apply now</p>", None))
    monkeypatch.setattr(m, "fetch_job_link_state", _fetch)
    queued = []
    monkeypatch.setattr(m, "enqueue_crm_payload", lambda p: queued.append(p))
    monkeypatch.setattr(m.time, "sleep", lambda *a, **k: None)
    return queued, fetched


def test_sweep_budget_reaches_every_tab_when_tetiana_cold_is_huge(monkeypatch):
    """THE BUG: `checked` was global, so a 50-row TC spent all 40 and TW/CL got zero."""
    rows = {
        "TC": [_job_row(f"tc{i}", "Matched", f"https://co.com/tc/{i}") for i in range(50)],
        "TW": [_job_row(f"tw{i}", "Applied", f"https://co.com/tw/{i}") for i in range(9)],
        "CL": [_job_row(f"cl{i}", "Applied", f"https://co.com/cl/{i}") for i in range(3)],
    }
    _q, fetched = _linkcheck_env_by_tab(monkeypatch, rows, {})

    result = m.check_job_links(limit=40, sleep_between=0)

    assert result["checked"] == 40 == len(fetched)
    assert result["per_tab"] == {"TC": 28, "TW": 9, "CL": 3}
    assert any("/tw/" in u for u in fetched) and any("/cl/" in u for u in fetched)


def test_sweep_counts_opaque_separately_from_transient(monkeypatch):
    rows = {"TC": [
        _job_row("u-cio", "Matched", "https://career.io/job/x"),
        _job_row("u-li", "Matched", "https://www.linkedin.com/jobs/view/1"),
        _job_row("u-to", "Matched", "https://co.com/j/9"),
    ]}
    fetches = {
        "https://career.io/job/x": (202, "https://career.io/job/x", "<div id=root></div>", None),
        "https://www.linkedin.com/jobs/view/1": (200, "https://www.linkedin.com/jobs/view/1", "<p>Sign in</p>", None),
        "https://co.com/j/9": (None, None, "", TimeoutError("boom")),
    }
    queued, _f = _linkcheck_env_by_tab(monkeypatch, rows, fetches)

    result = m.check_job_links(sleep_between=0)

    assert result["opaque"] == 2 and result["unknown"] == 1
    assert result["dead"] == [] and queued == []


def test_sweep_still_fetches_linkedin_because_it_404s_honestly(monkeypatch):
    """Measured 2026-09-26: live LinkedIn job ids answer 200, missing ones 404. Skipping the fetch
    for opaque hosts would throw away the only dead signal on a LinkedIn row."""
    rows = {"TC": [_job_row("u-li", "Matched", "https://www.linkedin.com/jobs/view/1")]}
    queued, fetched = _linkcheck_env_by_tab(
        monkeypatch, rows,
        {"https://www.linkedin.com/jobs/view/1": (404, "https://www.linkedin.com/jobs/view/1", "", None)})

    result = m.check_job_links(sleep_between=0)

    assert fetched == ["https://www.linkedin.com/jobs/view/1"]
    assert len(result["retired"]) == 1


def test_links_summary_breaks_out_opaque_transient_and_per_tab(monkeypatch):
    rows = {"TC": [_job_row("u-cio", "Matched", "https://career.io/job/x")],
            "TW": [_job_row("u-tw", "Applied", "https://co.com/tw/1")]}
    _linkcheck_env_by_tab(monkeypatch, rows,
                          {"https://career.io/job/x": (202, "https://career.io/job/x", "", None)})
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, *a, **k: sent.append(t) or 1)

    _dispatch("/links check")

    summary = next(t for t in sent if "checked" in t)
    assert "2 checked" in summary
    assert "1 opaque (never classifiable)" in summary and "0 unknown (transient)" in summary
    assert "TC 1 · TW 1 · CL 0" in summary


def test_standup_names_each_auto_retired_row_and_why(monkeypatch):
    """Write path: the sweep records the verdict, the NEXT morning's standup reads it back."""
    rows = {"TC": [_job_row("u-slate", "Matched", "https://www.jobleads.com/us/job/x",
                            company="Slate", role="Automotive Logistics Ops Specialist")]}
    _linkcheck_env_by_tab(monkeypatch, rows,
                          {"https://www.jobleads.com/us/job/x": (404, "https://www.jobleads.com/us/job/x", "", None)})
    m.check_job_links(sleep_between=0)

    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, *a, **k: sent.append(t) or 1)
    monkeypatch.setattr(m, "get_daily_activity", lambda d: {"drafts_staged": 0})
    monkeypatch.setattr(m, "calculate_active_day_streak", lambda: 1)
    monkeypatch.setattr(m, "get_overdue_followups", lambda: [])
    monkeypatch.setattr(m, "check_system_health", lambda: [])
    monkeypatch.setattr(m, "scan_carmen_hot_conversations", lambda *a, **k: [])

    m.send_daily_standup(1)

    assert "Slate - Automotive Logistics Ops Specialist (HTTP 404)" in sent[0]
    assert "u-slate" not in {r[0] for r in m.get_dead_job_links(include_notified=False)}


# ==============================================================================
# /public/dashboard - read-only, None-not-zero, and the document_compiled write path
# ==============================================================================

@pytest.fixture
def dashboard_env(monkeypatch):
    """Empty metric tables, cold caches, and a CRM that answers funnel_stats."""
    with m.get_db_conn() as conn:
        for table in ("pipeline_metrics", "inbound_threads", "application_outcomes"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    for cache, keys in ((m._public_dashboard_cache, ("payload", "fetched_at")),
                        (m._drafting_timing_cache, ("result", "measured_at"))):
        cache[keys[0]] = None
        cache[keys[1]] = 0.0
    monkeypatch.setattr(m, "crm_get", lambda *a, **k: _funnel_response(_FUNNEL))
    yield
    m._public_dashboard_cache["payload"] = None
    m._drafting_timing_cache["result"] = None


def _db_fingerprint():
    """Row count and max rowid of every table: any INSERT/UPDATE-by-replace/DELETE moves one."""
    with m.get_db_conn() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: conn.execute(f"SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM {t}").fetchone()
                for t in tables}, conn.total_changes


def test_read_only_db_makes_sqlite_refuse_writes(dashboard_env):
    with m.read_only_db():
        assert m.log_metric_event("ai_screened") is False  # the INSERT fails at the database
    assert m.get_metric_count("ai_screened") == 0
    assert m.log_metric_event("ai_screened") is True       # and the flag does not leak out
    assert m.get_metric_count("ai_screened") == 1


def test_public_dashboard_performs_zero_writes(dashboard_env, monkeypatch):
    """Seeded with real rows (including a cached role, so the drafting timing actually runs),
    the route must leave every table untouched and never post to the CRM or Telegram."""
    for _ in range(3):
        m.log_metric_event("ai_screened")
    m.save_job_to_cache("dash1", dict(_CARD_JOB, track="a", bullet_indices=[0], fit_score=88,
                                      tone_mode="conservative", outreach_template_id=0),
                        sheet_uuid="uuid-dash-1")
    def _forbidden(*a, **k):
        raise AssertionError("public dashboard attempted an outbound write")
    for name in ("crm_post", "send_telegram_message", "send_telegram_document", "create_gmail_draft",
                 "record_application_outcome", "log_daily_activity"):
        monkeypatch.setattr(m, name, _forbidden)
    before, _ = _db_fingerprint()
    with m.app.test_client() as client:
        res = client.get("/public/dashboard")
    after, _ = _db_fingerprint()
    assert res.status_code == 200
    assert before == after
    body = res.get_json()
    assert body["roles_screened"] == 3
    assert body["drafting"]["median_ms"] > 0 and body["drafting"]["runs"] == 3


def test_public_dashboard_empty_dataset_is_honest(dashboard_env):
    """Nothing logged yet: counts are real zeros, but anything with no data behind it is None
    or empty - never a fabricated median, timeline or timing."""
    with m.app.test_client() as client:
        body = client.get("/public/dashboard").get_json()
    assert body["roles_screened"] == 0
    assert body["documents_compiled"] == 0 and body["documents_since"] is None
    assert body["median_fit_score"] is None and body["scored_roles"] == 0
    assert body["timeline"] == []
    assert body["drafting"] is None
    assert body["dead_links"] == {"detected": 0, "retired": 0, "this_week": 0}
    assert body["live_conversations"] == 0


def test_public_dashboard_reports_none_not_zero_when_the_database_is_unreadable(dashboard_env, monkeypatch):
    monkeypatch.setattr(m, "DB_PATH", os.path.join(os.path.dirname(_TMP_DB_PATH), "no_such_dir", "x.db"))
    body = m.build_public_dashboard()
    for key in ("roles_screened", "documents_compiled", "live_conversations", "dead_links",
                "median_fit_score", "drafting"):
        assert body[key] is None, key
    assert body["applications_sent"] == 51  # the CRM half still reports


def test_compiled_resume_is_counted_and_read_back_by_the_dashboard(dashboard_env):
    """Write path: a real /e-style compile logs document_compiled, and the NEXT dashboard read
    reports it with the date counting began."""
    pdf = m.compile_resume_pdf_resilient(None, "Acme Capital", "a", [0], "/e")
    assert pdf
    body = m.build_public_dashboard()
    assert body["documents_compiled"] == 1
    assert body["documents_since"] == m.datetime.now(m.timezone.utc).strftime("%Y-%m-%d")


def test_public_dashboard_live_conversations_exclude_rejections(dashboard_env):
    with m.get_db_conn() as conn:
        conn.executemany(
            "INSERT INTO inbound_threads (thread_id, status_label, state) VALUES (?, ?, ?)",
            [("t1", "GENERAL", "open"), ("t2", "INTERVIEW_SET", "open"),
             ("t3", "REJECTION", "open"), ("t4", "GENERAL", "done")])
        conn.commit()
    assert m.build_public_dashboard()["live_conversations"] == 2


def test_public_dashboard_omits_the_funnel_by_default(dashboard_env, monkeypatch):
    monkeypatch.delenv("PUBLIC_FUNNEL_STATS", raising=False)
    assert m.build_public_dashboard()["funnel"] is None


def test_public_dashboard_funnel_carries_offers_and_rates(dashboard_env, monkeypatch):
    monkeypatch.setenv("PUBLIC_FUNNEL_STATS", "true")
    funnel = m.build_public_dashboard()["funnel"]
    assert funnel["offers"] == 1 and funnel["interviews"] == 6 and funnel["replies"] == 12
    assert funnel["reply_rate_pct"] == round(100 * 12 / 51, 1)


# ==============================================================================
# Site visitor analytics - privacy, bots, retention, and what the NEXT standup reads back
# ==============================================================================
import hashlib as _hashlib
import importlib.util as _importlib_util

_ANALYTICS_TOKEN = "test-analytics-token"
_HUMAN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
# LinkedIn's in-app browser is a PERSON reading the page; only LinkedInBot is an unfurler.
_LINKEDIN_APP_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 "
                    "(KHTML, like Gecko) Mobile/15E148 [LinkedInApp]/9.30.1234")

# Real user-agent strings, as the crawlers publish them.
_BOT_AGENTS = [
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "crawler"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1; "
     "+http://www.google.com/bot.html) Chrome/128.0.0.0 Safari/537.36", "crawler"),
    ("Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)", "crawler"),
    ("DuckDuckBot/1.1; (+http://duckduckgo.com/duckduckbot.html)", "crawler"),
    ("Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)", "crawler"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; "
     "+https://openai.com/gptbot)", "crawler"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; "
     "+claudebot@anthropic.com)", "crawler"),
    ("Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)", "crawler"),
    ("Mozilla/5.0 (compatible; SemrushBot/7~bl; +http://www.semrush.com/bot.html)", "crawler"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/13.1.1 Safari/605.1.15 (Applebot/0.1; +http://www.apple.com/go/applebot)", "crawler"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "HeadlessChrome/128.0.0.0 Safari/537.36", "crawler"),
    ("LinkedInBot/1.0 (compatible; Mozilla/5.0; Apache-HttpClient +http://www.linkedin.com)", "preview:LinkedIn"),
    ("facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)", "preview:Facebook"),
    ("Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)", "preview:Slack"),
    ("Twitterbot/1.0", "preview:X"),
    ("Render/1.0", "health"),
    ("Mozilla/5.0+(compatible; UptimeRobot/2.0; http://www.uptimerobot.com/)", "health"),
    ("curl/8.7.1", "health"),
    ("python-requests/2.32.3", "health"),
    ("Go-http-client/2.0", "health"),
    ("", "health"),
]


@pytest.fixture
def analytics_env(monkeypatch):
    with m.get_db_conn() as conn:
        conn.execute("DELETE FROM site_visits")
        conn.commit()
    monkeypatch.setenv("ANALYTICS_INGEST_TOKEN", _ANALYTICS_TOKEN)
    monkeypatch.setenv("ANALYTICS_ENABLED", "true")
    monkeypatch.delenv("ANALYTICS_IGNORE_IPS", raising=False)
    m._visitor_salt.update(day=None, salt=None)
    m._analytics_last_prune["at"] = 0.0
    yield


def _ingest(views, token=_ANALYTICS_TOKEN):
    with m.app.test_client() as client:
        return client.post("/analytics/ingest", json={"views": views},
                           headers={"X-Analytics-Token": token})


def _view(ip="203.0.113.7", ua=_HUMAN_UA, path="/", referrer="", is_self=False, ts=None):
    v = {"ip": ip, "ua": ua, "path": path, "referrer": referrer, "self": is_self}
    if ts:
        v["ts"] = ts
    return v


def _stored_rows():
    with m.get_db_conn() as conn:
        return conn.execute(
            "SELECT id, visited_at, path, visitor_id, referrer, bot FROM site_visits ORDER BY id").fetchall()


def _standup_text(monkeypatch):
    """Drive the REAL send_daily_standup and return what it would post to Telegram."""
    sent = []
    monkeypatch.setattr(m, "send_telegram_message", lambda cid, t, *a, **k: sent.append(t) or 1)
    monkeypatch.setattr(m, "get_daily_activity", lambda d: {"drafts_staged": 0})
    monkeypatch.setattr(m, "calculate_active_day_streak", lambda: 0)
    monkeypatch.setattr(m, "get_overdue_followups", lambda: [])
    monkeypatch.setattr(m, "get_dead_job_links", lambda **kw: [])
    monkeypatch.setattr(m, "check_system_health", lambda: [])
    monkeypatch.setattr(m, "scan_carmen_hot_conversations", lambda *a, **k: [])
    m.send_daily_standup(1)
    assert len(sent) == 1
    return sent[0]


def test_ingest_rejects_a_missing_or_wrong_token(analytics_env):
    assert _ingest([_view()], token="wrong").status_code == 403
    assert _stored_rows() == []


def test_no_raw_ip_or_user_agent_is_ever_persisted(analytics_env):
    ip = "203.0.113.7"
    assert _ingest([_view(ip=ip, referrer="https://www.linkedin.com/messaging/thread/2-XYZ/")]).status_code == 200
    (row,) = _stored_rows()
    flat = " ".join(str(c) for c in row)
    assert ip not in flat and "Chrome/128" not in flat and "2-XYZ" not in flat
    visitor_id = row[3]
    # Not any unsalted digest of the address...
    for algo in ("md5", "sha1", "sha256"):
        for msg in (ip, f"{ip}|{_HUMAN_UA}"):
            assert not _hashlib.new(algo, msg.encode()).hexdigest().startswith(visitor_id)
    # ...only reproducible WITH today's in-memory salt, and not with any other.
    today = m.datetime.now().date()
    assert visitor_id == m.daily_visitor_id(ip, _HUMAN_UA, m.visitor_salt_for(today))
    assert visitor_id != m.daily_visitor_id(ip, _HUMAN_UA, b"\x00" * 32)
    assert row[4] == "linkedin.com"  # a bare host, never the referring URL


def test_salt_rotates_daily_and_forgets_yesterday(analytics_env):
    day1, day2 = date(2026, 9, 25), date(2026, 9, 26)
    m.record_site_visits([_view()], day=day1)
    salt1 = m.visitor_salt_for(day1)
    m.record_site_visits([_view()], day=day2)
    ids = [r[3] for r in _stored_rows()]
    assert ids[0] != ids[1]                        # same IP, different days, different ids
    assert m.visitor_salt_for(day1) != salt1       # yesterday's salt is gone for good
    m.record_site_visits([_view(), _view(path="/job-engine")], day=day2)
    assert len({r[3] for r in _stored_rows()[2:]}) == 1  # but same-day pageviews group


@pytest.mark.parametrize("ua,expected", _BOT_AGENTS)
def test_real_crawler_agents_are_classified(ua, expected):
    assert m.visit_bot_flag(ua, "198.51.100.1", False) == expected


@pytest.mark.parametrize("ua", [
    _HUMAN_UA, _LINKEDIN_APP_UA,
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Mobile/15E148 Safari/604.1"])
def test_real_browsers_are_people(ua):
    assert m.visit_bot_flag(ua, "198.51.100.1", False) is None


def test_bots_are_excluded_from_visitors_and_counted_separately(analytics_env, monkeypatch):
    views = [_view(ip=f"198.51.100.{i}", ua=ua) for i, (ua, _) in enumerate(_BOT_AGENTS)]
    views.append(_view(ip="192.0.2.10", ua=_HUMAN_UA, referrer="https://www.linkedin.com/feed/"))
    assert _ingest(views).get_json()["stored"] == len(views)
    line = _standup_text(monkeypatch).split("🌐", 1)[1].splitlines()[0]
    assert "1 visitor (24h)" in line
    assert f"{len(_BOT_AGENTS)} bot hits filtered" in line
    assert "link previews: LinkedIn ×1" in line


def test_kevin_is_self_by_console_session_or_ignore_list(analytics_env, monkeypatch):
    monkeypatch.setenv("ANALYTICS_IGNORE_IPS", "192.0.2.99, 192.0.2.98")
    _ingest([_view(ip="192.0.2.99"), _view(ip="192.0.2.50", is_self=True)])
    assert [r[5] for r in _stored_rows()] == ["self", "self"]


def test_rows_past_the_retention_window_are_pruned(analytics_env):
    old = (m.datetime.now(m.timezone.utc) - m.timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%S")
    fresh = (m.datetime.now(m.timezone.utc) - m.timedelta(days=89)).strftime("%Y-%m-%dT%H:%M:%S")
    m.record_site_visits([_view(ts=old), _view(ts=fresh)])
    assert len(_stored_rows()) == 2
    _ingest([_view()])  # ingest itself prunes (at most hourly) - no separate job
    stamps = [r[1] for r in _stored_rows()]
    assert len(stamps) == 2 and all(s >= fresh.replace("T", " ") for s in stamps)


def test_analytics_disabled_stores_nothing_and_drops_the_line(analytics_env, monkeypatch):
    monkeypatch.setenv("ANALYTICS_ENABLED", "false")
    res = _ingest([_view()])
    assert res.status_code == 200 and res.get_json()["stored"] == 0
    assert _stored_rows() == []
    assert "🌐" not in _standup_text(monkeypatch)


def test_standup_still_sends_when_the_analytics_read_fails(analytics_env, monkeypatch):
    def _boom(*a, **k):
        raise m.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m, "get_site_visit_rows", _boom)
    msg = _standup_text(monkeypatch)
    assert "Daily Standup" in msg and "Active Streak" in msg
    assert "visitor log unavailable this morning" in msg


def test_quiet_day_renders_an_honest_line(analytics_env, monkeypatch):
    assert "🌐 montelattice.com: quiet - no visitors in 24h" in _standup_text(monkeypatch)


def test_ingested_visits_are_what_the_next_standup_reads_back(analytics_env, monkeypatch):
    """Write path, engine side: real ingest requests -> SQLite -> the real standup render."""
    _ingest([
        _view(ip="192.0.2.1", referrer="https://www.linkedin.com/in/someone/"),
        _view(ip="192.0.2.1", path="/job-engine", referrer="https://montelattice.com/"),
        _view(ip="192.0.2.2", referrer="android-app://com.linkedin.android/"),
        _view(ip="192.0.2.3", path="/job-engine", referrer="https://www.google.com/"),
        _view(ip="192.0.2.4"),
        _view(ip="66.249.66.1", ua=_BOT_AGENTS[0][0]),
    ])
    msg = _standup_text(monkeypatch)
    assert ("🌐 montelattice.com: via <b>linkedin.com</b> ×2, google.com ×1 · 4 visitors (24h)"
            " · 2 → Job Engine · 1 bot hit filtered") in msg
    assert msg.index("Overdue Actions") < msg.index("🌐") < msg.index("Full brief")


_SITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "montelattice-site")


@pytest.mark.skipif(not os.path.exists(os.path.join(_SITE_DIR, "visits.py")),
                    reason="montelattice-site checkout not beside this repo")
def test_site_pageviews_reach_the_standup_end_to_end(analytics_env, monkeypatch):
    """Write path across both services: real GETs through the SITE's Flask app and its
    before_request hook, the forwarder's real batch, the ENGINE's real ingest route, then the
    real standup render. Only the network hop is replaced by calling the engine's test client."""
    site_dir = os.path.abspath(_SITE_DIR)
    monkeypatch.syspath_prepend(site_dir)
    monkeypatch.setenv("EVIDENCE_BANK_SOURCE",
                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence_bank.json"))
    spec = _importlib_util.spec_from_file_location("montelattice_site_main", os.path.join(site_dir, "main.py"))
    site = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(site)
    import jobstats  # the site's modules, via the prepended path
    import visits
    monkeypatch.setattr(jobstats, "fetch", lambda: None)          # page content is not under test
    monkeypatch.setattr(visits, "_ensure_worker", lambda: None)   # drained by hand below

    browser = site.app.test_client()
    browser.get("/", headers={"User-Agent": _HUMAN_UA, "X-Forwarded-For": "192.0.2.21",
                              "Referer": "https://www.linkedin.com/messaging/thread/2-abc/"})
    browser.get("/job-engine", headers={"User-Agent": _HUMAN_UA, "X-Forwarded-For": "192.0.2.21",
                                        "Referer": "https://montelattice.com/"})
    browser.get("/", headers={"User-Agent": _BOT_AGENTS[0][0], "X-Forwarded-For": "66.249.66.1"})
    browser.get("/static/css/tokens.css", headers={"User-Agent": _HUMAN_UA})  # not a pageview

    batch = visits.drain(wait=0.1)
    assert len(batch) == 3
    assert _ingest(batch).get_json()["stored"] == 3
    msg = _standup_text(monkeypatch)
    assert ("🌐 montelattice.com: via <b>linkedin.com</b> ×1 · 1 visitor (24h) · 1 → Job Engine"
            " · 1 bot hit filtered") in msg


def test_main_opens_no_bare_connections_to_the_live_database():
    """Every live-DB connection goes through get_db_conn(). The only other sqlite3.connect calls
    are the backup routine's, which open the BACKUP file and close it in a finally."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8").read()
    callers = []
    for match in re.finditer(r"sqlite3\.connect\(([^)]*)\)", src):
        fn = re.findall(r"^def (\w+)", src[:match.start()], re.M)[-1]
        callers.append((fn, match.group(1).split(",")[0].strip()))
    live = [(fn, arg) for fn, arg in callers if "DB_PATH" in arg]
    assert {fn for fn, _ in live} == {"get_db_conn"}, callers
    assert all(arg == "dest_path" for fn, arg in callers if fn != "get_db_conn"), callers
