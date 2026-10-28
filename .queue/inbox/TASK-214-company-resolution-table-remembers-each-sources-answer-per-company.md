---
id: TASK-214
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -k "resolution_store" 2>&1 | tail -n 1 | grep -E "^([5-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
The SQLite database has a `company_resolution` table with `resolution_record` and `resolution_read` helpers, so /w can remember, per company and per source, whether the last paid resolution attempt resolved, came back empty, or errored, and when that company may be tried again. Depends on TASK-213 and TASK-201 having merged.

## Proof it is needed
`python -m pytest test_main_integration.py -q -k resolution_store` collects nothing, so the verify exits 1 (re-run before starting and paste the line). Pass-regex proven able to pass: `-k feedsource` on the same file ends `4 passed` after TASK-201, and `-k hirebase_lookupstep` ends `10 passed`.

## Notes
- Run `grep -i registry FUNCTION_INDEX.md` and `grep -i feed_source FUNCTION_INDEX.md` first. Copy TASK-201's `feed_source_upsert` / `feed_source_read` style exactly (same module area, same never-raises rule, same None-on-read-error rule).
- **Why a table of its own.** No store holds "this source tried this company and what happened" with a next-eligible date: `hirebase_slug_registry` carries routing meaning (`unknown` suppresses lookups 30 days, `no_jobs` skips), `company_feed_source` records only what a feed returned, the ledgers are per request with a 60-day window (`_ledger_marks` docstring in hirebase_feed.py: a zero row ages out and the company plans as never searched). Do NOT write into `hirebase_slug_registry` or `company_feed_source`.
- Table next to `company_feed_source`, same init function: `company_resolution (company_key TEXT NOT NULL, source TEXT NOT NULL, outcome TEXT NOT NULL, attempted_at TEXT NOT NULL, next_eligible_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (company_key, source))`.
- `resolution_record(company_key, source, outcome, now=None)`: `outcome` must be one of `resolve_plan`'s outcomes; `next_eligible_at` comes from `resolve_plan.next_eligible(outcome, now)` (TASK-213; import it, do not copy the day counts). Blank key, blank source or an unknown outcome writes nothing and returns False. Replaces the row for that `(key, source)`. Returns True when the write went through; never raises (log the exception CLASS only).
- A `"resolved"` row must never be overwritten by a later `"empty"` or `"error"` from the SAME source on the same call path unless the caller passes `force=True` (a resolved company that has a quiet week is not "unresolved" again; the existing scan planners own that).
- `resolution_read()` -> `{(company_key, source): (outcome, attempted_at, next_eligible_at)}`, or None on a read error (None, never `{}`: an unreadable table must not read as "nothing tried yet", because every cooldown would vanish and the heavy companies would be re-billed).
- Tests (`-k resolution_store`, at least 5; go through the real DB init the way TASK-201's tests do): round trip for each of the three outcomes with `next_eligible_at` equal to `resolve_plan.next_eligible`; the same `(key, source)` replaced; the same key under two sources keeps two rows; a `"resolved"` row survives a later `"empty"` and yields to `force=True`; blank key, blank source and an unknown outcome are refused; a read error returns None (monkeypatch `get_db_conn` to raise); a second init does not drop rows.
- FUNCTION_INDEX.md is NOT in scope; the planner regenerates it after merge.

## Log

