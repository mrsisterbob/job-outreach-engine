# job-outreach-engine

[![Tests](https://github.com/mrsisterbob/job-outreach-engine/actions/workflows/tests.yml/badge.svg)](https://github.com/mrsisterbob/job-outreach-engine/actions/workflows/tests.yml)
<!-- AUTO-STATS:START -->
![Lines of source](https://img.shields.io/badge/source-18887_lines-c9a24b)
![Tests](https://img.shields.io/badge/tests-844-4a8a5c)
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
templates/          Editable JSON banks for cold/warm/LinkedIn outreach + cover letter copy (live-reloaded).
resume_bullets_bank.json   Track-based (a-h) resume bullet pools, resolved deterministically.
                    An entry is a plain string, or {"text", "source_job", "replaces"} when its
                    claim belongs to an employer other than the most recent one.
evidence_bank.json  Single source of truth for real experience/skills fed into every AI prompt.
test_pipeline_utils.py     Unit tests for pipeline_utils.py (pytest, no network/DB required).
```

Gemini is strictly a *classifier/router*: it returns a fit score, a track letter, and integer
template indices. It never authors resume bullets, outreach prose, or cover letter copy directly -
Python resolves those deterministically from the JSON banks. This keeps every candidate-facing word
traceable to a human-edited source of truth.

The cover letter (`/letter`) reuses the *same* track + tone routing as the resume PDF, so the two
never argue different cases for the same job: `track` picks the body paragraph from
`cover_letter_templates.json`, and `tone_mode` picks whether the automation work is framed as
engineering (`tech`) or as process discipline (`conservative`).

Gemini's routing is then **repaired deterministically** in `evaluate_job_with_gemini()`, because it
chooses `track` and `outreach_template_id` independently and nothing used to make them agree - a
freight role could ship a logistics resume under an email describing Kevin as a custodial
reconciliation person, which is what six of six sends did on 2026-09-24. Two corrections, both in
`track_registry.py` and both logged so the rate stays visible:

- `ALLOWED_EMAIL_IDS_BY_TRACK` lists the `cold_ops` indices each track may send. Gemini's pick is
  honored when it is in the set, and otherwise snaps to the track's first allowed id. Tracks f, g
  and h are limited to entries 6 and 7, the only two that claim no financial-services background.
- `override_track_for_title()` moves a finance-framed track to f or g when the job TITLE names
  carrier/freight/dispatch or supply-chain/procurement/plant work - unless the employer is itself a
  financial-services business, checked both through `tone_mode` and the employer's name.

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
- **Inbound mail alerting (`/poll`, `check_inbound_gmail_replies`):** the poller now decides
  *bulk vs. human*, not *spam vs. not* - the query is `label:INBOX` and Gmail files spam under a
  separate label, so nothing reaching this code was called spam by Google. Three defaults changed,
  all still overridable from the Render dashboard:
  - `EMAIL_MAX_AGE_SECONDS` is no longer a fixed `300`. It derives from the poll cadence -
    `max(EMAIL_POLL_HOURS * 3600 * 2, 345600)`, i.e. a **96h floor**. A 5-minute window against a
    poller that runs every 24h discarded essentially all inbound mail. The floor is 96h rather
    than 24h because the derived value moves the **wrong way** when the cadence is tightened:
    switching to `EMAIL_POLL_HOURS=1` for fresher alerts silently shrank the window from 48h to
    24h. A recruiter replying Friday evening is 62h old by Monday if the container spun down over
    the weekend - Tier 1 skips this gate so interviews were safe, but an ordinary human reply was
    dropped and marked read. Four days covers a long weekend plus a holiday Monday.
  - `EMAIL_REQUIRED_KEYWORDS` now defaults to **empty** (the gate is off). It blocked 8 of 10
    messages in a real production poll, including a recruiter confirming an interview. Set it in
    Render to restore the old behaviour verbatim.
  - Bulk mail is identified by the **`List-Unsubscribe`** header instead. A newsletter sets it; a
    person typing an email does not. **Exception:** a sender that resolves to an exact CRM contact
    is let through anyway - some firms route all outbound mail through Mailchimp/HubSpot, so a
    recruiter's hand-written note carries the header, and dropping it was total silent loss (the
    Spam sweep refuses bulk too). The whitelist is the discriminator, not the wording.
  - **Tier 1 override:** a calendar invite (`text/calendar` part or `METHOD:REQUEST`) or an
    interview signal alerts *always* - past the age gate, the bulk rules and the CRM whitelist.
    It still respects `EMAIL_SENDER_BLACKLIST` and `EMAIL_BLOCK_DOMAINS`. A sender with no CRM row
    gets the alert and **no CRM writes** - the alert says so on its face.
    **Exception:** a message carrying `List-Unsubscribe` cannot take the bypass. Job-board blasts
    like `"Application status update - YOUR INTERVIEW REQUEST AWAITING YOUR CONFIRMATION"` match
    `\binterview\b` and were skipping every gate behind it. The same rule applies in the Spam
    sweep, which now refuses to resurrect bulk mail Gmail filed correctly.
  - `EMAIL_QUERY_EXCLUSIONS` (default `-category:promotions -category:social -category:forums`)
    is applied **in the Gmail query itself**. This is the only filter layer that runs *before* the
    per-cycle message budget is spent - every Python gate rejects a message that has already taken
    a slot, so a burst of job-board mail could starve a real interview out of the window.
  - `EMAIL_POLL_MAX_RESULTS` (default **50**, was a hardcoded 10). `messages.list` costs 5 quota
    units regardless of the value and `messages.get` 5 units each, against a 1.2M/day ceiling, so
    50 is not meaningfully more expensive - and 10/cycle was far below one day's inbound volume.
  - `EMAIL_SENDER_BLACKLIST` now covers the **robot-mailbox** families the other two gates
    structurally cannot see. Transactional mail (PayPal receipts, Google location notices) sets no
    `List-Unsubscribe` and is filed *Updates*, not *Promotions* - so neither the bulk gate nor the
    category exclusions touch it. `noreply-location-sharing@google.com` is the shape that exposed
    it: it contains `noreply-`, never `noreply@`. Entries match the **address**, not the domain,
    so `jane@paypal.com` still reaches you while `service@paypal.com` does not.
  - **Reading depth:** the classifier reads the real message body (`extract_plain_body`, up to
    `CLASSIFIER_BODY_CHARS`=2000), not Gmail's ~200-char `snippet`. The body was already in memory
    - the fetch is `format=full` so `.ics` parts are visible - and was being discarded, so an ask
    sitting in paragraph three was classified on the opening pleasantries. Prefers `text/plain`,
    falls back to stripped HTML, skips attachments (a PDF's bytes are not body text), and cuts the
    quoted thread tail so `interview` inside Kevin's *own* earlier message cannot fake a signal.
    The alert still **shows** the short snippet - the compact card is deliberate.
  - **`EMAIL_EXCLUDED_KEYWORDS` now defaults to empty.** It was a substring test over
    subject+snippet, so a real person writing *"just a quick alert that the role is still open"*
    died on `alert`, and *"I'll unsubscribe you from the list but wanted to reply personally"* died
    on `unsubscribe`. It blocked no junk the structural gates miss: newsletters carry
    `List-Unsubscribe` and robot mailboxes are caught by the sender blacklist.
  - **`EMAIL_MIN_BODY_LENGTH` 50 -> 12.** The shortest replies are often the warmest - a busy human
    types one line. 50 dropped *"Hi Kevin, got a sec?"*; 12 still drops an empty auto-ack.
  - **Delivery is confirmed before a message is marked read.** `send_telegram_message` returns the
    message_id on success and `None` on failure (it never raises), and the mark-read POST used to
    run unconditionally right after it - so a 5s timeout or a second 429 meant the alert was never
    seen, the message was no longer unread, and the next `is:unread` query could never find it
    again. An undelivered alert now leaves the mail **UNREAD**, which *is* the retry: the next
    cycle re-lists it. A duplicate alert costs a glance; a dropped one costs the interview.
  - **Inbound tray (`inbound_threads`, `/inbox`, `/done <id>`):** one durable row per Gmail
    *thread* - the unit Kevin actually acts on. This is the ledger the notification path never had:
    - A second reply on an open thread updates the row instead of firing a duplicate alert.
      **Tier 1 is exempt** - an interview or offer landing on an existing thread still interrupts.
    - Strangers are recorded too (`sheet_uuid` blank). The CRM path is keyed on a sheet row, so a
      recruiter's first email - a stranger by definition - previously got one alert and then fell
      out of the system entirely.
    - `state` is `open` until `/done <id>`; a new reply reopens a closed thread.
    - Every helper is failure-tolerant: if SQLite is unreachable the alert still goes out, since
      a broken ledger must not become a broken notification.
  - **Poller failure alerts:** a dead `GMAIL_REFRESH_TOKEN` or a failing list query now sends a
    Telegram notice (`report_poller_failure`, debounced 6h per stage via the DB-backed
    `should_send_alert`). Previously these logged and continued, so notifications stopped silently
    and the failure surfaced as a missed interview.
  - Gmail `messages.get` is fetched at `format=full` (not `metadata`), because `payload.parts` is
    the only place an `.ics` is visible. Same 5 quota units per call; the cost is response size,
    roughly 1KB → tens of KB, at ≤10 messages per poll.
  - **Spam sweep:** every cycle also runs a second narrow query over `label:SPAM is:unread`
    (`sweep_spam_for_interview_signals()`, ≤10 messages) and applies **only** the Tier 1 test.
    Gmail's classifier is wrong in one costly direction - an invite from a company you have never
    corresponded with looks exactly like bulk mail - and a false positive in Spam is unrecoverable
    because nobody reads that folder. Nothing else from Spam is ever surfaced, and nothing from
    Spam ever writes to the CRM: the sweep is a separate function that calls no CRM lookup, write,
    outcome recorder or metric event, so the guarantee is structural rather than a matter of
    reading the branches correctly. A surfaced message is marked read (so it does not re-alert
    every cycle) and otherwise left in Spam - reclassifying it on the strength of a regex is
    Kevin's call, not the poller's. Non-matching spam is not touched at all.
- **Editing outreach copy without a redeploy:** use the Telegram `/edit` command, or edit
  `templates/*.json` / `resume_bullets_bank.json` directly - both are hot-reloaded on every use.
  A resume bullet may be a `{"text", "source_job", "replaces"}` object rather than a string, which
  is how a bullet describing ABC Technologies' or 40 Acres' work renders under **that** employer
  instead of the most recent one. `/edit TG0` rewords such an entry and keeps its tags; editing the
  JSON by hand, replace only the `"text"` value. A bare string means the most recent employer,
  which is what 110 of the 120 entries are.
- **Adding a job you found yourself:** `/job <linkedin-url>` pushes one hand-picked posting
  through the *same* Stage 2 path `/t` uses (`process_single_candidate` → `dispatch_tier1_matches`),
  so it lands in Tetiana Cold as a real card with a live `sheet_uuid` - swipe-reply (`/apply`,
  `/n`, `/f`), the follow-up sequencer and `/funnel` all work on it. Unlike `/t` there is **no
  score>=80 gate**: a posting you chose is already vetted, so any score writes the row.
  Re-pasting the same job dedups instead of writing a second row.
  - Works with **any** posting URL, not just LinkedIn: the scraper keys on the schema.org
    `JobPosting` JSON-LD block that nearly every ATS and corporate careers site emits. An
    employer careers page (the destination behind LinkedIn's Apply button) is usually the
    *better* link to paste - it answers a plain GET with the full description, where LinkedIn
    serves an auth wall to any server-side fetch. LinkedIn URLs are rewritten to the public
    `jobs-guest` endpoint, which usually works.
  - Campaign params (`utm_*`, `source`, `refId`, …) are stripped before the URL is used as the
    dedup key or stored as the apply link, so the same job reached from a LinkedIn ad and an
    Indeed ad is one row, not two.
  - When a page can't be read (auth wall, JS-only rendering), the command replies asking for
    `/job Title @ Company <url>` rather than filing a `Manual Ingest` row. The desktop
    bookmarklet (`POST /ingest`) scrapes inside your own session, so it always has the full
    description - prefer it when you are at a desk and the page is JS-only.

## Public aggregate endpoint

`GET /public/stats` - unauthenticated, read-only, **counts only**. It exists so the portfolio
site can cite pipeline numbers that a stranger can verify instead of taking them on faith.

```json
{"status":"ok","applications_logged":51,"replies":12,"interviews":6,"rejections":9,
 "still_sourcing":40,"days_running":50,"start_date":"2026-07-31","as_of":"2026-09-19"}
```

- **Source** is the Sheets CRM `funnel_stats` action, not local SQLite - the Sheet is where a
  status actually changes, and SQLite can trail it after a host restart.
- **No PII.** Every value but `status`/`start_date`/`as_of` is an integer. No row, company,
  role, person or address is reachable through it.
- **Bucket roll-up.** `funnel_stats` reports each row's *current* status, so a row in
  Interviewing was necessarily applied to and replied to. Each count includes every stage past
  it. `Rejected` counts as an application but **not** as a reply: the bucket cannot tell an
  auto-reject from a post-interview no, and folding it in would inflate the reply rate in the
  direction that flatters.
- **Failure mode is 503, never zero.** Zeros from a failed fetch are indistinguishable from
  invented numbers once they are printed on a page. A stale cache is served (flagged
  `"stale": true`) ahead of an outage; with no cache at all the endpoint refuses to answer.
- Cached `PUBLIC_STATS_TTL_SECONDS` (default 900) so a page refresh doesn't spend an Apps
  Script call.

## Tests

```
python -m pytest test_pipeline_utils.py -v
```

Covers dork builders, priority normalization, dedup hashing, salary/work-style extraction, age
badges, and smart CRM tab routing - all pure functions in `pipeline_utils.py`, no network/DB/Flask
dependency required.
