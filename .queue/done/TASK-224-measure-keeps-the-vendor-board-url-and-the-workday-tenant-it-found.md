---
id: TASK-224
scope: scripts/measure_board_systems.py, test_board_system_classifier.py
verify: python -m pytest test_board_system_classifier.py -q -k "board_url" 2>&1 | tail -n 1 | grep -E "^([4-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
The careers-page measurement in `scripts/measure_board_systems.py` keeps WHERE it found a vendor board (the final vendor URL, host and path only) and, for Workday, the `tenant/wdN/site` triple, so a later report can say which Workday tenants were found instead of only the word "workday".

## Proof it is needed
`python -m pytest test_board_system_classifier.py -q -k board_url` selects no test, so the verify exits 1 (re-run before starting and paste the line). Pass-regex proven able to pass: `python -m pytest test_board_system_classifier.py -q -k classif` ends `N passed` with N >= 4 (re-check the count before relying on it). Today `measure` (scripts/measure_board_systems.py:128-185) sets `rec["bucket"] = "workday"` and returns; the URL that proved it is dropped, so TASK-225 cannot report a Workday tenant, and a tenant is exactly what `fetch_workday_jobs` needs to scan the company.

## Notes
- Read `scripts/measure_board_systems.py:128-185` (`measure`, the redirect branch and the 200 branch) and `workday_board.board_triple_from_url` (workday_board.py:21, its docstring says a shape check proves nothing about whether the board answers; keep that caveat in the field name: `workday_triple_shape`, not `workday_board`).
- Add to the record: `board_url` = the final URL that decided the bucket, as `host + path`, with no query string and no fragment, only when the bucket is a vendor (not `own_site`/`unknown_system`); both the redirect-onto-a-vendor-host return and the 200 return set it. For `workday` also `workday_triple_shape = board_triple_from_url(url)` (empty string when it cannot yield one). Records that came from slug hits (the six probes) keep working unchanged.
- The cache file name `CACHE_NAME` (`measure_board_systems_171_v2.json`) holds records WITHOUT these fields: bump it to `_v3` (its comment says to bump when logic changes) so an old cache cannot mask the new fields. Add a test that the name changed.
- Do not change what `bucket` means or the printed `report()` output beyond (optionally) one line counting how many workday records carry a triple shape.
- Tests (in `test_board_system_classifier.py`, `-k board_url`, at least 4, fake responses as the existing tests in that file build them, no network): a redirect onto `acme.wd5.myworkdayjobs.com/en-US/External_Careers` gives bucket `workday`, `board_url` host+path with no query, and triple shape `acme/wd5/External_Careers`; a 200 careers page whose final URL is a lever host sets `board_url` and no triple; an `own_site` result has no `board_url`; a query string and a fragment never reach `board_url`; the cache file name carries `v3`.

## Log


- Attempt 1: added _board_url/_record_vendor to scripts/measure_board_systems.py (board_url host+path, workday_triple_shape for workday, only vendor buckets), CACHE_NAME bumped to _v3, one report line counting workday records with a triple shape. Added 5 board_url tests. Verify exit 0 ("5 passed, 20 deselected"); test_board_system_classifier.py: "25 passed in 0.03s".
- Worker re-check: verify exit 0 (5 passed). review skipped: 36 non-test lines. Worktree gate (classifier + both suites) exit 0. Not run against real careers pages (no network); fake responses only.
