---
id: TASK-204
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -k "apify_learn" 2>&1 | tail -n 1 | grep -E "^([3-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
After an Apify call, `/w` records each company's `source_domain` and board in `company_feed_source`, and a company whose rows came from a Workday tenant gets that `tenant/wdN/site` triple stored, so the NEXT `/w` run scans its board free and the company is no longer a no-board candidate. Depends on TASK-200, 201 and 202.

## Proof it is needed
`python -m pytest test_main_integration.py -q -k apify_learn` collects nothing, so the verify exits 1 (observed 2026-10-28: `1318 deselected in 1.04s`). Today nothing is learned from an Apify row. Pass-regex proven able to pass: `-k hirebase_lookupstep` ends `10 passed, 1308 deselected`.

## Notes
- Run `grep -i workday FUNCTION_INDEX.md` first. **Reuse `record_workday_board_from_posting(job, warm_contact)` (main.py:3976); do not write a second Workday writer.** It already refuses aggregator links, accepts only `*.myworkdayjobs.com` via `workday_board.board_triple_from_url`, never overwrites a stored triple, makes no HTTP call, and stores through `set_company_workday_board` into `company_identities`, which is exactly where `/w` reads triples (`wd_to_contact`, main.py:15458-15469). It is called today only from /t (main.py:6504).
- Where: in the Apify block TASK-202 added, after `split_rows` and before the postings go through `_collect_posting`, for each company in `apify_kept_by_company`:
  1. `apify_feed.summarize_feed_sources(apify_kept_by_company)` -> `feed_source_upsert(company_key, source_domain, job_board, rows)` (TASK-201). Only companies with kept rows are written. Companies with zero rows are NOT written (the feed not covering a company is not worth a row, and it must never read as a place's board).
  2. For the first kept job per company whose apply link is a Workday address, call `record_workday_board_from_posting(job, warm_contacts.get(normalize_company_for_match(job["employer_name"])))`. `warm_contacts` is the dict `/w` already builds (keyed by normalized name; see main.py:15460).
  3. Both writes are best effort: wrap in try/except, log the class name only, a failure never fails the scan.
- The stored triple is UNVERIFIED (a posting link proves a posting, not a board `/w` can list). `/w`'s own scan is the check next run; do not claim otherwise in a comment or message.
- **Test the write path, not the return value.** The recurring failure here is a spec that passes on what a function returns while the next run reads back something different. So the main test must run `run_warm_radar_scan` TWICE through `_WarmRadarHarness` (test_main_integration.py:15750): run 1 with Hirebase answering zero rows and Apify returning a Workday row (apply link such as `https://fifththird.wd5.myworkdayjobs.com/en-US/FifthThird_Careers/job/...`, `source_domain` `fifththird.wd5.myworkdayjobs.com`, organization matching the warm company); between runs assert the `company_identities` row holds the triple and `feed_source_read()` holds domain and board "workday"; in run 2 the company is scanned through the Workday path (it must not be in the Apify request again and must not be a no-board candidate), asserted from what the harness's fake Workday fetch received, not from a hand-built dict.
- Other tests (`-k apify_learn`, at least 3 in all): a non-Workday `source_domain` stores board "" and writes no `workday_board`; a stored triple (hand-added) is not overwritten by an Apify row; a company with zero Apify rows writes nothing to `company_feed_source`.

## Log
