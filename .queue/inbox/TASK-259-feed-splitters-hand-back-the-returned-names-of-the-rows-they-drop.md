---
id: TASK-259
scope: apify_feed.py, hirebase_feed.py, test_apify_feed.py, test_hirebase_feed.py
verify: python -m pytest test_apify_feed.py test_hirebase_feed.py -q -p no:cacheprovider -k dropped_names 2>&1 | tail -n 1 | grep -E "^([6-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error" && python -m pytest test_apify_feed.py test_hirebase_feed.py -q -p no:cacheprovider 2>&1 | tail -n 1 | grep -E "^[0-9]+ passed" | grep -Ev "failed|error"
---

## Target
`apify_feed.split_rows` and `hirebase_feed.split_jobs` can hand back the employer name each DROPPED row came back with (and why it was dropped), through a new optional `dropped=None` list argument, so a later report can show "sent Haworth Inc., Haworth International came back"; their return values and every existing caller are unchanged.

## Proof it is needed
`python -m pytest test_apify_feed.py test_hirebase_feed.py -q -p no:cacheprovider -k dropped_names` on 2026-10-31 (main at 4760805) selects nothing (`116 deselected`, exit 5), so the verify exits 1. Pass-regex proven able to pass: `test_hirebase_feed.py` alone ends `72 passed in 0.09s` and the second half of the verify (whole files, any count) matches `^[0-9]+ passed`. Why: `split_rows` (apify_feed.py:160-183) counts `stats["unmatched"] += 1` and `split_jobs` (hirebase_feed.py:183-202) does the same, and neither keeps the row's own employer name; main.py then rewrites kept rows to the sent name (`dict(job, employer_name=apify_name)`), so no name that came back survives the call.

## Notes
- Search first: `grep -i -E "split|unmatched|parse_row|parse_job" FUNCTION_INDEX.md`. Read the two functions above and `parse_row` / `parse_job` (the parsed dict's `employer_name` is what the source returned).
- **Signature, decided:** `split_rows(rows, company_names, normalize, dropped=None)` and `split_jobs(jobs, company_name, slugs, normalize, trusted_slugs=(), dropped=None)`. `dropped` is added LAST and keyword-friendly, so no positional caller changes (the two functions are called in main.py and in these two test files; do not edit any of those calls). When `dropped` is a list, append `(returned_name, reason)` for each dropped row: Apify `reason` is `"no_match"` (no company owns it) or `"claimed_by_several"` (more than one does; the second form is what `owners` of length 2 or more means at apify_feed.py:177-182); Hirebase `reason` is `"no_match"`. A malformed row appends `("", "malformed")`. `returned_name` is the parsed `employer_name` (string, stripped, may be empty); for a malformed row nothing can be read, so it is `""`. When `dropped` is `None` the functions behave byte-for-byte as today. A non-list `dropped` (a tuple, a string) is ignored, not appended to, and never raises.
- The list is the CALLER's, so appending is the only mutation. Do not change `stats` keys (`returned, malformed, kept, unmatched`) or add any.
- Do not touch the matching rules (`row_belongs_to`, `job_belongs_to`): which rows are kept must stay identical. A test pins it: the same inputs with and without `dropped` give equal `(kept, stats)`.
- Scope overlaps TASK-248 (`apify_feed.py`, `test_apify_feed.py`); the queue serializes it. Do not edit a test 248 owns; add new tests only, named `..._dropped_names_...`.
- Tests (at least 6 across the two files, all `dropped_names` in the name): (1) Apify, the Haworth case: companies `["Haworth Inc."]`, rows for `Haworth Inc.` and `Haworth International`: the second is in `dropped` as `("Haworth International", "no_match")` if no rule claims it (use whichever result the CURRENT rules give: assert on the real `kept` and put the non-kept row's name in `dropped`, whatever it is; the test pins that every non-kept parsed row appears exactly once in `dropped`); (2) a row claimed by two companies is `claimed_by_several`; (3) a malformed row gives `("", "malformed")`; (4) `dropped=None` and `dropped=[]` give the same `(kept, stats)`; (5) Hirebase: an unmatched job lands in `dropped` with its returned name; (6) `len(dropped) == stats["unmatched"] + stats["malformed"]` for a mixed batch in both modules; (7) a tuple passed as `dropped` is ignored without raising.

## Log

