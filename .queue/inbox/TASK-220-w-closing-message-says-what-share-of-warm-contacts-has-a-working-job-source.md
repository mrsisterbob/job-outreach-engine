---
id: TASK-220
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -k "source_share_line" 2>&1 | tail -n 1 | grep -E "^([5-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
The `/w` closing message carries ONE number: of all warm CONTACTS in the sheet, how many work at a company with a working job source (a board /w scans, a Hirebase slug with jobs, or an Apify feed that returned rows), shown as a count and a percent. It counts contacts, not companies. Depends on TASK-212 and TASK-214 having merged (TASK-201 for the feed source).

## Proof it is needed
`python -m pytest test_main_integration.py -q -k source_share_line` collects nothing, so the verify exits 1 (re-run before starting and paste the line). Today `_warm_radar_coverage_line` (main.py:15317) counts only board-scanned contacts (`scanned`), so a contact at a company covered by Hirebase or Apify reads as "checked, no public job board found". Pass-regex proven able to pass: `-k hirebase_lookupstep` ends `10 passed`.

## Notes
- Run `grep -i coverage FUNCTION_INDEX.md` first. Do NOT replace `_warm_radar_coverage_line`; it answers a different question (why a contact has no board) and has tests. Add a sibling `_warm_radar_source_share_line(covered, total)` placed right after it in the closing message.
- Build the numbers from `company_source.build_source_records(...)` and `company_source.covered_contact_share(records)` (TASK-212) over the employer-like companies `/w` already classified. **Denominator decision, state it in a comment and the Log:** the total is ALL warm contacts the run read, so the contacts at `not_company` / non-employer entries (`contacts_by_bucket["not_company"]`) and the `unplaced` contacts (no company) are added to the total and never to the covered side. That keeps the figure conservative and tied to the Carmen Warm row count; Kevin can ask for the other denominator.
- Line text: `Warm contacts with a working job source: C of T (P%)` with P rounded to a whole number; `T == 0` prints nothing. When the records cannot be built (a store read returned None, see TASK-219) print `Job-source coverage unavailable this run` instead of a stale or guessed number. Counts-only wording, like its neighbour.
- One record build per `/w`, shared with TASK-219: whichever of the two lands first adds a helper `warm_radar_source_records(...)` that gathers the stores and returns the records (None when any store read failed); the second reuses it. Do not read the stores twice in one run.
- Tests (`-k source_share_line`, at least 5; drive `run_warm_radar_scan` through `_WarmRadarHarness`, test_main_integration.py:15750, read the real closing message): a company of 40 contacts covered by a stored board plus 2 uncovered companies of 1 contact each reads `40 of 42` (NOT 1 of 3); a Hirebase-covered company counts, a Hirebase `no_jobs` one does not; an Apify feed company with rows counts, one with zero rows does not; `not_company` and `unplaced` contacts raise the total and not the covered side; a store read returning None prints the "unavailable" line, not a number.

## Log

