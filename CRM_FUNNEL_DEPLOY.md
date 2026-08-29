# CRM Funnel + Dedup + Canonical Status — Deploy Notes

Branch: `feature/crm-funnel` (7 commits, one per numbered item).

This change set is **additive or bug-fix only**. No schema change: `SCHEMAS`,
`normalizeRowData`, `transposeRowValues`, `SCHEMA_FIELD_KEYS`, `UUID_COL`, and the
10-column assumption are all untouched. No Column K was added.

Two moving parts ship together:

1. **`Code.gs`** — must be pasted into the Apps Script editor and re-deployed (steps below).
2. **`main.py` / `pipeline_utils.py`** — deploy the Python app as usual. It talks to the
   **new deployment** of `Code.gs`, so deploy Code.gs first (or at the same time).

---

## 1. Every change to `Code.gs`, function by function

### New constants (top of file)

| Name | What |
|------|------|
| `STATUS_COL = 6` | Column F, the Status column (shared JOBS/PEOPLE position). |
| `STATUS_VOCAB` | `["Matched","Applied","Replied","Screening","Interviewing","Offer","Rejected"]` — canonical Status vocabulary, ordered earliest→latest. Mirrors `pipeline_utils.STATUS_VOCAB`. |
| `DEDUP_STOP_TOKENS` | `{inc,llc,corp,co,ltd,the}` — filler tokens dropped from dedup keys. Mirrors `pipeline_utils._DEDUP_STOP_TOKENS`. |

### New helper functions

| Function | What it does |
|----------|--------------|
| `statusRank(value)` | 0-based ordinal of `value` in `STATUS_VOCAB` (case-insensitive, trimmed); `-1` if unrecognized. Mirrors `pipeline_utils.status_rank`. |
| `canonicalizeStatus(raw)` | Maps any free-text / blank Status to a canonical value, or `null` when nothing matches. Rules applied in canonical order, first match wins: blank / `match|sourc|lead|prospect|candidate|pending|queue|identif|backlog|to review` → `Matched`; `appl|sent` → `Applied`; `repl|respond` → `Replied`; `screen` → `Screening`; `interview` → `Interviewing`; `offer` → `Offer`; `reject|declin|pass|no thanks` → `Rejected`. Shared by items 5 and 6. |
| `normalizeDedupKey(company, role)` | `"<company>|<role>"` — lowercased, punctuation→spaces, whitespace collapsed, `DEDUP_STOP_TOKENS` removed. Empty inputs → `"|"`. Mirrors `pipeline_utils.normalize_dedup_key`. |
| `findLiveJobsDuplicate(sheet, company, role)` | Returns the Sheet UUID (may be `""`) of an existing **non-terminal** JOBS row in `sheet` matching `(company, role)` by normalized key, else `null`. Non-terminal = Status ≠ `Rejected` and the sheet is not `Died`. Scans bottom-to-top. |
| `dedupeJobsTabs(dryRun)` | **Editor-callable one-time cleanup.** See §3. |
| `migrateStatusVocabulary(dryRun)` | **Editor-callable one-time migration.** See §3. |

### Modified functions

| Function | Change |
|----------|--------|
| `doPost` → `add_row` action | Before appending a JOBS row, calls `findLiveJobsDuplicate`. On a hit, returns **`{status:"success", message:"duplicate suppressed", sheet_uuid:<existing uuid>}`** and appends nothing. |
| `doPost` → `batch_add_rows` action | Seeds the set of live `Company+Role` keys already in the tab (terminal rows excluded), then skips any batch row that collides with it or with an earlier row in the same batch. `message` reports `(N duplicate(s) suppressed)` when any were skipped; **`count` now reflects rows actually inserted**, not `rows.length`. |
| `doPost` → **new** `set_status` action (`3b`) | `{action:"set_status", sheet_uuid, status}` → writes Column F on the row found by `sheet_uuid` and **nothing else — no tab move**. Missing args → `set_status requires sheet_uuid and status`; not found → `No record found for sheet_uuid <uuid>`; success → `Status set to <status> for <uuid>`. Used by `/apply`, `/replied`, `/interview`. |
| `doGet` → **new** `funnel_stats` action | See §1 payload below. Authorized by the same `isRequestAuthorized(e.parameter.secret)` check as the other GET actions. **Reads only, writes nothing.** |
| `get_followups` action (inside `doPost`) | JOBS rows now return `title: row[2] || ""` (the real Role from Column C, `""` when blank) instead of the hardcoded `"Operations Specialist"`. PEOPLE rows are unchanged (they still get `"Operations Specialist"` — they have no role column; out of scope). |
| `formatSheet(sheet)` | For **JOBS tabs only**, inside the existing per-tab pass (after banding, before conditional formatting): applies a data-validation dropdown to `Status` (Column F, rows 2..maxRows) listing the 7 canonical values, `setAllowInvalid(true)`, dropdown shown; clears prior validation on that range first. PEOPLE tabs untouched. Banding and conditional formatting still apply. |

### `funnel_stats` response shape

```json
{"status":"success",
 "overall":{"Matched":n,"Applied":n,"Replied":n,"Screening":n,"Interviewing":n,"Offer":n,"Rejected":n},
 "by_persona":{"Tetiana":{…same keys…},"Clavicular":{…same keys…}},
 "rates":{"matched_to_applied":pct,"applied_to_reply":pct,"reply_to_interview":pct,"interview_to_offer":pct}}
```

- Persona = tab-name prefix: `Tetiana Cold` + `Tetiana Warm` → `Tetiana`; `Clavicular` → `Clavicular`.
- **`Died` is excluded** (archive tab, no persona). If you want archived rows folded into
  `overall`, that's a one-line change in `personaFor` — flagged in the final summary.
- Legacy/blank Status text is bucketed with `canonicalizeStatus(raw) || "Matched"`.
- Rates are **adjacent-bucket ratios** of the point-in-time counts (`Applied/Matched`,
  `Replied/Applied`, `Interviewing/Replied`, `Offer/Interviewing`), rounded to 1 decimal,
  `0.0` when the denominator is 0. It's a snapshot funnel, not a cumulative cohort funnel.

---

## 2. Paste-and-Deploy steps (Code.gs)

1. Open the CRM spreadsheet → **Extensions → Apps Script**.
2. Select the whole `Code.gs` contents in the editor and replace with the new
   `Code.gs` from this branch. Save (Ctrl/Cmd-S).
3. **Deploy → Manage deployments** → pick the existing Web App deployment →
   pencil (**Edit**) → **Version: New version** → add a note
   (`crm-funnel: dedup + canonical Status + funnel_stats`) → **Deploy**.
4. The Web App URL does not change, so `CRM_WEBHOOK_URL` in the Python app stays the same.
5. Deploy the Python app (`main.py`, `pipeline_utils.py`) normally.

---

## 3. One-time ops — exact run order

Run each from the Apps Script editor (**Run** ▸ pick the function), then open
**View → Logs** (or **Executions**) to read the output. `dryRun` defaults to `true`,
so calling with no argument is always the safe preview.

1. **`dedupeJobsTabs(true)`** — dry run.
   Review the log: one line per duplicate group — tab, kept UUID + row, and the
   dropped UUIDs + row numbers.
2. **`dedupeJobsTabs(false)`** — deletes the losers (bottom-up, under the script lock)
   and re-formats each affected tab. Re-run `dedupeJobsTabs(true)` afterward to confirm
   `0 duplicate row(s)`.
3. **`migrateStatusVocabulary(true)`** — dry run.
   Review the log: every intended `"<old>" -> "<new>"` and every
   `UNMAPPED: "<value>" (tab, row)`.
4. **Resolve each `UNMAPPED` by hand** in the sheet (set the Status cell to the right
   canonical value from the dropdown), or leave it — unmapped values are left untouched
   by the migration and bucket as `Matched` in the funnel.
5. **`migrateStatusVocabulary(false)`** — writes the canonical Status text per tab and
   logs a per-tab count.
6. **Sanity-check the funnel:** in a browser, hit
   `<CRM_WEBHOOK_URL>?action=funnel_stats&secret=<CRM_SHARED_SECRET>`
   (omit `&secret=…` if `CRM_SHARED_SECRET` is unset). Confirm the buckets sum to your
   row counts and the rates look sane. Or just send `/funnel` in Telegram.

Order matters: dedupe **before** migrate (fewer rows to migrate, and the dedupe
tie-break uses `statusRank`, which reads cleaner on canonical text but works on legacy
text too), and both **before** relying on `/funnel`.

`formatSheet` will add the Status dropdown to JOBS tabs the next time it runs for each
tab (any `add_row`/`batch_add_rows`/`update_status`/`quick_add` write, or a manual
`formatAllSheets()` run from the editor). Running `formatAllSheets()` once after the
migration is the quickest way to get the dropdown onto every JOBS tab immediately.

---

## 4. New / changed Telegram commands

| Command | Syntax | What it does |
|---------|--------|--------------|
| `/apply` | reply to a job card | **Changed.** Now writes `Status = Applied` on the row **in place** (via `set_status`) instead of moving the row to the `Tetiana Warm` tab. Metric/outcome logging and the local applied-company cache are unchanged. |
| `/replied` | `/replied <short_id>` | Resolves `<short_id>` → `sheet_uuid` (`get_sheet_uuid_by_short_id`), writes `Status = Replied` (no tab move). Unknown/expired id → the standard "Record Not Found" message; success → `✅ Replied - <date>`. |
| `/interview` | `/interview <short_id>` | Same as `/replied` but writes `Status = Interviewing`. |
| `/funnel` | `/funnel` | **Changed.** Now calls the `funnel_stats` GET action and prints an OVERALL block (7 canonical Status counts + the 4 conversion rates) followed by one block per persona. A webhook error or `status:"error"` response prints a single `⚠️ Funnel unavailable` line. (Previously it printed a local-metric-counter ASCII funnel.) |

Both new commands appear in the `/`-help command list (the "SWIPE-REPLY ACTIONS" section).

---

## 5. Env / config

**Nothing new.** No new environment variables, script properties, or config keys.
`CRM_WEBHOOK_URL` and `CRM_SHARED_SECRET` are the same as today; `funnel_stats` and
`set_status` ride the existing webhook and the existing shared-secret check.
