# job-outreach-engine

<!-- AUTO-STATS:START -->
![Lines of source](https://img.shields.io/badge/source-5985_lines-c9a24b)
![Tests](https://img.shields.io/badge/tests-54-4a8a5c)
<!-- AUTO-STATS:END -->

An AI-assisted job search pipeline: sources listings from multiple job boards, screens/tailors
outreach with Gemini against a strict evidence bank (no hallucinated experience), logs everything
to a Google Sheets CRM, and runs entirely through a swipe-reply Telegram bot. Designed to be run
continuously (APScheduler + Flask webhook server), not as a one-off script.

## Architecture

```
main.py            Orchestration: Flask routes, Telegram bot, CRM sync, Gmail, AI calls, scheduling.
pipeline_utils.py   Pure helpers (no I/O): dork builders, scoring/dedup/formatting. Unit-tested.
resume_engine.py    Deterministic Typst->PDF resume compiler from the local bullet bank.
Code.gs             Google Apps Script Web App - the CRM backend (Sheets tabs) main.py talks to.
templates/          Editable JSON banks for cold/warm/LinkedIn outreach copy (live-reloaded).
resume_bullets_bank.json   Track-based (a-e) resume bullet pools, resolved deterministically.
evidence_bank.json  Single source of truth for real experience/skills fed into every AI prompt.
test_pipeline_utils.py     Unit tests for pipeline_utils.py (pytest, no network/DB required).
```

Gemini is strictly a *classifier/router*: it returns a fit score, a track letter, and integer
template indices. It never authors resume bullets or outreach prose directly - Python resolves
those deterministically from the JSON banks. This keeps every candidate-facing word traceable to
a human-edited source of truth.

## Setup

1. `pip install -r requirements.txt`
2. Deploy `Code.gs` as a Google Apps Script Web App (Execute as: Me, Access: Anyone) bound to your
   CRM spreadsheet. Copy its `/exec` URL into `CRM_WEBHOOK_URL`.
3. In the Apps Script project, set **Project Settings > Script Properties > `CRM_SHARED_SECRET`**
   to a random string, and set the same value as `CRM_SHARED_SECRET` below. This is the only auth
   on the webhook - without it, anyone with the URL can read/write your CRM.
4. Set the environment variables below (`.env`, shell profile, or your host's secrets manager).
5. Run locally: `python main.py` (Flask dev server) or via `gunicorn main:app` in production.

### Required/optional environment variables

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Yes | Swipe-reply UI and all operator notifications. |
| `GEMINI_API_KEY` | Yes | AI screening/routing. |
| `CRM_WEBHOOK_URL` | Yes | Apps Script Web App URL (Google Sheets CRM). |
| `CRM_SHARED_SECRET` | Strongly recommended | Auth token validated by `Code.gs`. |
| `RAPIDAPI_KEY` or `OPENWEBNINJA_KEY` | Yes (one) | JSearch job sourcing. |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` / `GMAIL_USER` | Optional | Gmail draft creation + inbound reply polling. |

Run `/health` in Telegram at any time to see current pipeline telemetry. A daily 08:30 digest also
surfaces **Config Health Warnings** automatically if any of the above go missing.

## Operational runbook

- **Backups:** `jobs_cache.db` (SQLite: job cache, CRM outbox, metrics) is snapshotted to
  `backups/` every Sunday 03:00 via APScheduler (`backup_sqlite_db()`), keeping the last 8 weekly
  snapshots. This is local-disk only - if you move hosts, copy `backups/` (or ship it somewhere
  durable) before decommissioning the old machine.
- **CRM webhook failures / Gemini outages / zero job listings in a run:** automatically alert to
  Telegram via `send_health_alert()`. If Telegram itself is misconfigured, check `logging` output.
- **Secret rotation:** rotate `CRM_SHARED_SECRET` in both the Apps Script Script Properties and
  your environment together (a mismatch fails closed - the webhook returns `Unauthorized`).
- **Editing outreach copy without a redeploy:** use the Telegram `/edit` command, or edit
  `templates/*.json` / `resume_bullets_bank.json` directly - both are hot-reloaded on every use.

## Tests

```
python -m pytest test_pipeline_utils.py -v
```

Covers dork builders, priority normalization, dedup hashing, salary/work-style extraction, age
badges, and smart CRM tab routing - all pure functions in `pipeline_utils.py`, no network/DB/Flask
dependency required.
