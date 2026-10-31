---
id: TASK-264
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -p no:cacheprovider -k apify_cutoff 2>&1 | tail -n 1 | grep -E "^([6-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
When the Feed (Apify) returns exactly as many rows as the request limit, `/w`'s closing text says on its own line that the answer is possibly cut off (rows and limit named, kept count named), and a company with no rows in such a run is described as possibly cut off rather than uncovered; a run below the limit reads exactly as it does today.

## HOLD: depends on TASK-238 being in `done/`
Block, and write nothing, if it is not: 238 rewrites the Apify block and the closing lines this task adds to.

## Proof it is needed
`python -m pytest test_main_integration.py -q -p no:cacheprovider -k apify_cutoff` on 2026-10-31 (main at 4760805) selects nothing (exit 5), so the verify exits 1. Pass-regex proven able to pass: `-k hirebase_lookupstep` ends `10 passed, 1412 deselected` and matches `^([6-9]|[1-9][0-9]+) passed`. Why: `apify_feed.fetch_feed` already computes it (apify_feed.py:226, `FetchResult.capped` is True when `len(body) == limit`, pinned by `test_apify_fetch_exactly_limit_rows_sets_capped`, test_apify_feed.py:184), but `grep -n "apify_result.capped" main.py` finds nothing: the Hirebase block reads its own `.capped` (main.py:16364) and Apify's is dropped. On 2026-10-31 a 200-row answer to a limit-200 request printed `Apify: N companies, 200 rows returned, 186 kept ...` with nothing to say the batch may have stopped early.

## Notes
- Search first: `grep -i -E "capped|cut off|apify" FUNCTION_INDEX.md`. Read the Apify call and settle (main.py:16470-16490 on 4760805; 238 will have moved it) and the closing Apify lines (main.py:16813-16830). Never Read `main.py` whole.
- Capture once, where the call is settled: `apify_capped = bool(apify_result.capped)` in the `status == "ok"` branch (initialise `apify_capped = False` beside `apify_rows_returned = 0`, main.py:~16431), and keep the limit that was sent (`apify_plan.limit`). A run that errored, was skipped or was never made keeps `False`. Do not recompute it as `rows == limit` in main.py: `fetch_feed` already owns that test.
- **New closing line, its own line, placed immediately BEFORE the `Apify: ...` line** (the way Hirebase's `... cut off at N jobs` line precedes its summary): `Apify possibly cut off: {rows} rows returned and the request limit was {limit}; {kept} kept are what came back, which may not be everything the feed holds for these companies`. Plain text, numbers from the run. Do not change one character of the existing `Apify: ...` line or the `Searched Apify, no rows for:` line when the run is not capped: about ten existing tests (test_main_integration.py:22036-22100) assert substrings of them.
- **When capped, the `Searched Apify, no rows for: ...` line** keeps its text but ends `(the answer was cut off, so these may have rows that were not returned; that is not the same as no jobs)` instead of the current parenthesis. Never "no jobs" wording.
- **Not changed, on purpose:** the ledger row stays `ok` at `actual_cost(rows)` (`apify_ledger_settle` accepts only `ok`/`errored`, main.py:3142, and `plan_apify_run` stamps every picked company as fed on an `ok` row, apify_feed.py:262-280). Nothing in this task alters which companies are asked next. Say in the Log, as a flagged decision for Kevin and not an action: today a cut-off batch marks every picked company as fed for `APIFY_COMPANY_RETRY_DAYS` (7), so a company whose rows were cut off is not re-asked for a week; a follow-up would stamp only the companies that had kept rows. Do not build it here.
- A `logging.warning("Warm Radar Apify answer hit the request limit (rows == limit): possibly cut off")` with the two numbers; no token, no row text.
- Tests (`test_apify_cutoff_...`, at least 6; drive `run_warm_radar_scan` through `_apify_closing_run` / `_apify_wire_harness` and `_apify_row` (test_main_integration.py:22028 and neighbours), real DB, fake HTTP by host): (1) two companies picked (limit 200) and exactly 200 rows returned: the text has a line starting `Apify possibly cut off: 200 rows returned and the request limit was 200`, it comes before the `Apify: ` line, and the `Apify: 2 companies, 200 rows returned, ` prefix is still present unchanged; (2) 199 rows: no `cut off` text anywhere; (3) a larger batch whose limit is above 200 (five companies, limit 250 via `LIMIT_PER_COMPANY`): 250 rows names limit 250, 249 rows does not trigger it; (4) a capped run where one picked company has no rows: its `Searched Apify, no rows for:` line contains `cut off` and not `no jobs` in a claiming way, and the same run uncapped keeps the old parenthesis byte for byte; (5) an errored call (`mode="500"`) and an unconfigured token print no cut-off line; (6) the kept count in the cut-off line equals the real kept (200 rows of which 14 belong to no picked company: `186 kept`); (7) the ledger row for the capped run is status `ok` and usd `actual_cost(200)`, unchanged; (8) `caplog` has the warning once and no `APIFY_TOKEN` value.
- Existing tests: change none. If one fails, block and name it.

## Log

