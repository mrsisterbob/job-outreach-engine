# Follow-up Sequencer — Design Notes

Branch: `feature/followup-sequencer` (5 commits, one per numbered item, + this docs commit).

**No Sheet schema change.** `SCHEMAS`, `normalizeRowData`, `transposeRowValues`,
`SCHEMA_FIELD_KEYS`, `UUID_COL`, the 10-column assumption, and Column K are all untouched.
Sequencing state is derived entirely from **Status (Col F)**, **Date Added (Col A)** and
**Next Followup Date (Col G)**; `[reason: ghosted]` is written into the existing **Notes
(Col I)** via the existing `append_note` action.

One additive `Code.gs` change ships with this branch and **requires a redeploy** (see §6).

---

## 1. Policy table (`pipeline_utils.followup_action`)

Pure function `followup_action(status, date_added, next_followup, today)` → exactly one of
`none` / `send_followup_1` / `send_followup_2` / `bury_ghosted` / `stale_nudge`.

`days` below = `(today − anchor).days`, where **anchor = Date Added** (see §5 for why not
Next Followup Date). A row whose **Next Followup Date is in the future is always `none`**,
whatever its status — this is checked before anything else and covers both a manual `/f`
snooze and the job's own re-fire guard.

| Status (canonical) | Next Followup Date in future | `days` (from Date Added) | Action |
|--------------------|------------------------------|--------------------------|--------|
| `Applied` | yes | — | `none` |
| `Applied` | no | `days < 4` | `none` |
| `Applied` | no | `4 ≤ days < 9` | `send_followup_1` |
| `Applied` | no | `9 ≤ days < 16` | `send_followup_2` |
| `Applied` | no | `days ≥ 16` | `bury_ghosted` |
| `Replied` / `Screening` / `Interviewing` | yes | — | `none` |
| `Replied` / `Screening` / `Interviewing` | no | `days ≤ 5` | `none` |
| `Replied` / `Screening` / `Interviewing` | no | `days > 5` | `stale_nudge` (never auto-buries) |
| `Matched` / `Offer` / `Rejected` | any | — | `none` |
| unknown / blank / legacy free-text | any | — | `none` (row is never touched) |
| anchor unresolvable (Date Added *and* Next Followup Date blank/malformed) | — | — | `none` |

Boundary behaviour (unit-tested): day 3→`none`, 4→`#1`, 5→`#1`; 8→`#1`, 9→`#2`, 10→`#2`;
15→`#2`, 16→`bury`, 17→`bury`. "Untouched > 5 days" is strict (`days == 5` → `none`).

Status is matched case-insensitively / whitespace-trimmed against
`pipeline_utils.STATUS_VOCAB` (`["Matched","Applied","Replied","Screening","Interviewing","Offer","Rejected"]`).
Anything else — including pre-migration free text — ranks −1 and returns `none`.

"No reply" needs no extra check: a reply advances Status off `Applied`, so `Status == Applied`
*is* "applied, no reply".

---

## 2. The four tunable knobs

All in **`pipeline_utils.py`**, in one commented block immediately above `followup_action`
(they are the only place the cadence is expressed; `main.run_followup_sequencer` imports the
names, and nothing on the `Code.gs` side references them):

| Constant | Value | Meaning |
|----------|-------|---------|
| `FOLLOWUP_1_DAYS` | `4` | `Applied` + no reply → follow-up #1 due at `anchor + 4d` |
| `FOLLOWUP_2_DAYS` | `9` | `Applied` + no reply → follow-up #2 due at `anchor + 9d` |
| `FOLLOWUP_BURY_DAYS` | `16` | `Applied` + no reply → auto-bury as ghosted at `anchor + 16d` |
| `STALE_HOT_DAYS` | `5` | `Replied`/`Screening`/`Interviewing` untouched longer than this → `stale_nudge` |

Retune constraint (locked by `test_followup_cadence_knobs_are_strictly_increasing`):
`0 < FOLLOWUP_1_DAYS < FOLLOWUP_2_DAYS < FOLLOWUP_BURY_DAYS`. The nightly job pushes Next
Followup Date to the *next* boundary, so out-of-order values would skip or repeat a step.
`STALE_HOT_DAYS` is independent.

---

## 3. Automatic vs. approval-gated writes

Per nightly run of `run_followup_sequencer()` (07:30 local, `EMAIL_POLL_SCHEDULER` cron job,
ahead of the 08:30 standup digest):

| Sequencer decision | What the job does | Automatic? |
|--------------------|-------------------|------------|
| `bury_ghosted` | `append_note` `[reason: ghosted]` **then** `update_status` → `Died` (via the durable CRM outbox) | **YES — the only automatic Sheet write.** Surfaced in the card's "Buried overnight" section so it is never silent. |
| due follow-up — **PEOPLE row** (Carmen Cold, 4/11/21 ladder) | Bump text is built from the template bank and listed under the card's "Nudge these people". **No Gmail draft is created at 7:30** — the text is saved in the day's snapshot, and the draft is made only when Kevin clicks ✉️ Open in Gmail on `/followups`. No draft cap. Next Followup Date advances via `update_snooze`. **No email is ever sent.** | Snooze is automatic (never under `/queue`); **the draft and the send are both on demand.** |
| `send_followup_1` / `send_followup_2` — **JOBS row** (Tetiana Cold/Warm, Clavicular) | Listed under the card's "Applications going quiet" as status only: applied date, days silent, severity dot, buries-on date, 📋 link to `/stage`. **No bump text, no Gmail draft.** Next Followup Date still advances via `update_snooze`. | Snooze is automatic — it must be, or the +16 bury is never reached. |
| Carmen ladder **revival** (stale or blank anchor, see §3a) | `append_note` `[date] Ladder restarted …` **then** `update_snooze` → today + 4. Listed under "Back on the ladder", labelled "revived — ladder restarted today". | Automatic. Date Added is never rewritten. |
| Carmen ladder **exhausted**, notes carry a reply | Listed under "Ready to promote" with a `/promote <id>` hint. | **No write** — reappears every morning until Kevin runs `/promote` or `/demote`. |
| Carmen ladder **exhausted**, no reply | `append_note` `[reason: no reply after 3 nudges]` **then** `update_status` → `Killed` (`CARMEN_GHOST_TAB`). Listed under "Killed overnight". | **YES**, capped at `MAX_AUTO_KILLS_PER_RUN` (10); overflow is reported, not written, not logged, and drains on a later run. |
| `stale_nudge` | Listed in the card's "Going cold" section. | No write at all. |
| top-3 `Matched` by Fit Score | Listed in the card's "Top 3 untouched matches" section. | No write at all. |

**Why applications don't draft.** Carmen Cold holds real people with real addresses. A JOBS
row's Contact Email is often Kevin's own address (no human was resolved) or a contact already
tracked in Carmen Cold, so a bump drafted from it is undeliverable or a duplicate. Applications
are for watching; people are for messaging.

**The card and `/followups`.** The 07:30 card is a scannable list: one line per person
(role or name — company · #attempt · due → next · 🆔 short_id) and one per application. It
carries no draft text and no Gmail links. A single 📋 link opens `GET /followups`, which shows
each person's full draft in a readonly textarea with a Copy button and, when the row has a
usable address, an ✉️ Open in Gmail link, followed by the applications table. A row with a blank
or bracketed (unverified) address shows the text and Copy button with a "no verified address"
note instead.

`/followups` renders **the 07:30 run's saved result** (`followup_queue_snapshot`, one row per
`run_date`, pruned after 14 days; it lives in `jobs_cache.db` on the mounted disk, so it
survives deploys). It never recomputes: within seconds of the run, its own snoozes push every
listed row's Next Followup Date into the future, so a later scan finds nothing due. With no
snapshot for today the page says "no queue for today yet" rather than showing a misleading
recompute.

**Drafts are on demand.** ✉️ Open in Gmail links to `GET /followups/draft/<sheet_uuid>`, which
finds that entry in **today's** snapshot (404 page otherwise — it never recomputes), creates the
draft with the saved `draft_text` as its body and the same `Re:` subject `/sendall` uses, and
302-redirects to the draft in Gmail. It is a GET that writes, deliberately, so it works as a plain
link, and it is bounded: a blank or bracketed address never reaches Gmail (copy-by-hand page
instead); a repeat click finds the draft through the 24h `(to_email, subject)` dedup and redirects
to the same one, checked before `create_gmail_draft()` so its "Draft Already Exists" Telegram
ping does not fire from a browser click; and a Gmail failure renders the reason plus the text to
copy. There is no draft cap: the old `MAX_AUTO_DRAFTS_PER_RUN` only bounded speculative drafting
and silently deferred real follow-ups. `/sendall` still drafts eagerly — it is a separate,
explicit command.

`/queue` runs the identical scan with `dry_run=True`: **zero** enqueues, **zero** bury,
**zero** kill, **zero** revival note, **zero** snooze advancement, **zero**
`followup_sequencer_log` writes — it only renders the "what would happen" card.

### 3a. Carmen contact lifecycle

```
Carmen Warm  --(Kevin drags a row when ready)-->  Carmen Cold
Carmen Cold  --(4/11/21 ladder + 7-day grace)-->  replied?  --> Carmen Hot   (/promote, by hand)
                                                  silent?   --> Killed       (automatic, capped)
Carmen Cold / Carmen Hot  --(/demote)-->  Carmen Warm   (the only route back to the bench)
```

Carmen Warm is the reserve bench (~291 older contacts), not an archive: nothing is cleaned or
migrated there, and only `/demote` routes into it. It stays in `get_overdue_followups()`'s scan
for its own reasons (see that docstring).

**Ladder.** Nudges at anchor + 4 / 11 / 21 days (`CARMEN_LADDER_DAYS`). The third nudge writes
one more date, anchor + 28 (`CARMEN_TERMINAL_GAP_DAYS` = 21 + `CARMEN_KILL_GRACE_DAYS`); when it
arrives the row is **exhausted** and triaged. Before this, nudge #3 wrote no date, the row
re-read as rung 3, and it nudged every morning forever — "exhausted" was unreachable.

**Anchor** (`plan_carmen_ladder`) = the latest of Date Added (Col A), the last
`[date] Inbound reply received` note (`INBOUND_REPLY_NOTE_MARKER`, written by
`route_inbound_reply_to_crm`) and the last `[date] Ladder restarted` note
(`LADDER_RESTART_NOTE_MARKER`, written by the sequencer). Both markers are module constants in
`pipeline_utils.py`, shared by writer and parser. A reply therefore restarts 4/11/21 from the day
they wrote back, so a live conversation does not die on a ghost's clock.

**Revival** (`CARMEN_STALE_ANCHOR_DAYS` = 30). A whole ladder takes 28 days, so an anchor older
than 30 cannot be mid-ladder: it is a promoted bench contact (Last Contact Dates months old) or a
stalled row. Such a row restarts from today as unscheduled — unless its follow-up date is one the
ladder plausibly wrote (gap 1–28 **and** that date is itself within 30 days, so a late run still
kills a finished ghost), or is a hand-set future date (respected; the row waits for it). A blank
Date Added revives too. The restart is persisted as a dated note, never by rewriting Date Added —
without a persisted anchor the next pass would revive again and the row would never reach rung 1.
Revival is guarded by `followup_sequencer_log`, so a same-day re-run writes one note, not two.

**Ghost destination** is `CARMEN_GHOST_TAB = "Killed"` in `main.py`: the existing PEOPLE archive,
reversible (the row still exists) and skipped by `quick_add`'s duplicate check so the person can
be re-added. Change it to `"Carmen Warm"` to return ghosts to the bench instead.

**Triage is by note, not by `application_outcomes`.** That table records interview / rejection /
offer only, so it misses plain replies — the reply note is the only complete responder signal.

Follow-up drafts: `build_followup_bump_draft()` → `load_outreach_templates()["followup_bumps"]`
→ `resolve_template_text(pool, idx)` → `interpolate_template(...)`. `idx = 0` for attempt #1,
`idx = 1` for attempt #2 (rotates the two bank entries by attempt number). No LLM call, no
prose authored in Python.

---

## 4. Idempotency guarantee

**Running the sequencer twice on the same day, on the same row data, queues nothing extra
and buries nothing twice.** Two independent mechanisms:

1. **Same-day guard (local).** New SQLite table `followup_sequencer_log(sheet_uuid, run_date,
   action)`, PK `(sheet_uuid, run_date)`. Before any write for a row, the job checks
   `_sequencer_already_actioned(sheet_uuid, today)`; after a successful enqueue it calls
   `_record_sequencer_action(...)`. A second run the same day short-circuits every already-
   actioned row. This is what makes idempotency hold even though the CRM writes are async
   (outbox) and a mock/again-run sees unchanged row data. `dry_run` never reads or writes
   this table.

2. **Across-day guard (state-based).** A queued follow-up pushes Next Followup Date to
   `anchor + FOLLOWUP_2_DAYS` (after #1) or `anchor + FOLLOWUP_BURY_DAYS` (after #2), so
   `followup_action()`'s "future Next Followup Date ⇒ none" gate suppresses the row until the
   next window opens. A `bury_ghosted` row is moved to `Died`, which `get_followups` for
   `TC`/`TW`/`CL` never returns — so it cannot be re-buried.

`followup_sequencer_log` is added to `test_main_integration.py`'s `clean_tables` truncation
list (one-line change, called out here).

---

## 5. Judgment calls

- **Anchor is Date Added, not Next Followup Date — contrary to a literal reading of the spec
  ("use Next Followup Date when set, else Date Added").** The literal rule drifts: after the
  job pushes Next Followup Date forward and that date later arrives, re-anchoring on it gives
  `days = 0`, which maps back into the `send_followup_1` window — so #1 fires forever and #2
  never does. Making Date Added the stable clock, and using Next Followup Date purely as a
  "not yet" gate (+ as the fallback anchor only when Date Added is blank/malformed), makes the
  windows monotonic and the sequence terminate. `followup_anchor()` encodes this and is shared
  by the policy and the job.
- **`calculate_followup_interval` was not reused.** Its model is a priority-decayed interval
  (`max(3, round(35 − priority·3.2))`, ~3–32 days) — a different concept from a fixed
  `+4/+9/+16` cadence keyed off row age. Forcing it in would have meant diverging from its
  semantics silently; per the spec I'm flagging it instead.
- **"The date the row entered Applied" is not knowable without a schema change**, so it is
  approximated by the anchor above. `/apply` only writes `set_status`; it does not stamp a
  date. Accepted as a known imprecision rather than adding an audit column.
- **`get_followups` had to learn two response fields** (`status`, `date_added`) — see §6.
  This is additive to the JSON the action already returns (same category as the prior batch's
  `title` change), not a schema change. Without it the sequencer has no Status or age input
  and Item 2 could not be done.
- **The morning card is one message, not per-row swipe cards.** The spec asks for the "swipe-
  reply card format … approve/send with the commands that already exist" *and* "a single
  Telegram message". Per-row reply-mappable cards would be several messages. Resolution: one
  message, one line per row (drafts live on `/followups`), and each row carries its `short_id`
  so `/replied <id>` / `/interview <id>` (which need no reply context) work directly. The card
  deliberately carries **no full `sheet_uuid`**: swipe-reply recovery takes the first UUID in a
  message, so a swipe on a multi-row card would silently act on row #1. Swipes fail cleanly
  instead. `bury_ghosted` and
  the Died move are still done automatically; the card only *reports* them.
- **`short_id`** comes from a new `get_short_id_by_sheet_uuid()` (reverse of the existing
  `get_sheet_uuid_by_short_id`). Rows with no local `jobs`-cache entry fall back to an
  8-char `sheet_uuid` stub in the card and cannot be driven by `/replied`/`/interview`.
- **Fit Score** for the "top 3" comes from `get_followups`' existing `raw_priority` field,
  which for JOBS rows is Column E (Fit Score) verbatim — no third Code.gs field added.
- **Only canonical `Matched`** rows feed the "top 3" (`status_rank == 0`); pre-migration
  free-text equivalents ("Sourced", "Lead", …) are excluded until the Status migration runs.
- **Scan set = Tetiana Cold + Tetiana Warm + Clavicular** (the JOBS tabs `get_followups`
  exposes, codes `TC`/`TW`/`CL`). `Died` is terminal and unreachable by `get_followups`, so
  buried rows drop out of scope for free.

---

## 6. Deploy

1. **`Code.gs` — redeploy required.** The `get_followups` action now also returns
   `status: row[5] || ""` (Col F) and `date_added: formatDate(row[0])` (Col A) on each row.
   Additive to the response object only — no `SCHEMAS` / column / `UUID_COL` change. Paste
   the new `Code.gs`, **Deploy → Manage deployments → Edit → New version → Deploy**. The Web
   App URL is unchanged. Until this is live the sequencer sees blank Status/Date Added on
   every row and takes no action (fails safe).
   **Carmen Hot (lifecycle change): deploy the Apps Script FIRST, Render second.** The
   `Code.gs` commit adds `"Carmen Hot": "PEOPLE"` to `TAB_MAP` and a `CH` code to every
   resolver. With it live, the first `/promote` creates the tab via `getOrCreateSheet` with the
   PEOPLE header row — no hand-made tab needed. Against the OLD script, the same move creates
   the tab with JOBS headers (`TAB_MAP[name] || "JOBS"`), transposes the contact into JOBS
   columns, and leaves the row invisible to every `ALL_TABS` lookup — silent corruption, not a
   rejected write.
2. **Python app** — deploy `main.py` / `pipeline_utils.py` as usual. `init_db()` creates
   `followup_sequencer_log` on boot (`CREATE TABLE IF NOT EXISTS`). The 07:30 job registers
   on the existing `EMAIL_POLL_SCHEDULER` via `start_followup_sequencer()`.
3. **Env / config** — nothing new.

## 7. New / changed commands

| Command | What |
|---------|------|
| `/promote <id>` | Moves a Carmen Cold contact to Carmen Hot and appends a dated note. `<id>` is the card's 🆔: a short_id, or the 8+ character sheet_uuid prefix most contacts show. Only Carmen Cold/Hot rows match, so a JOBS short_id can never be moved into a PEOPLE tab. |
| `/demote <id>` | Parks a Carmen Cold or Carmen Hot contact on the Carmen Warm bench, with a dated note. |
| `/queue` | **New.** Read-only preview of what the nightly sequencer would do — no writes, no bury, no snooze advancement. Same card as the 07:30 message, labelled "Queue Preview · read-only". Added to the `/`-help TELEMETRY list. |

## 8. Tests

`python -m pytest -q` → all green. New coverage:

- `test_pipeline_utils.py`: every `followup_action` branch, boundary days 3/4/5 · 8/9/10 ·
  15/16/17, blank/malformed dates, unknown/legacy Status, future-date gate, `datetime`
  coercion, `followup_anchor` precedence, cadence-knob ordering invariant.
- `test_main_integration.py` (CRM webhook layer mocked as in the existing suite): correct
  action/payload per branch, `bury_ghosted` writes `append_note` *then* `update_status`→`Died`,
  future-dated rows untouched, `stale_nudge`/top-3 make no writes, **idempotency across two
  consecutive runs on the same data**, `dry_run` writes nothing, the card renders every
  populated section / collapses to one line when empty, and **`/queue` performs zero writes**.
