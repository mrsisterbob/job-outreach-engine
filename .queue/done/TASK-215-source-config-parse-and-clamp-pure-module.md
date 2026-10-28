---
id: TASK-215
scope: source_config.py, test_source_config.py
verify: python -m pytest test_source_config.py -q -k "source_config" 2>&1 | tail -n 1 | grep -E "^([8-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
A new pure module `source_config.py` turns the raw System_Config values for the two paid job sources into a validated config (on/off plus a budget each), clamped to hard maxima so a typo in the sheet cannot spend money. No I/O.

## Proof it is needed
`python -m pytest test_source_config.py -q -k source_config` fails because the file does not exist (observed 2026-10-28: exit 1). Pass-regex proven able to pass: `test_visit_alert.py -k visit` ends `14 passed`.

## Notes
- Run `grep -i config FUNCTION_INDEX.md` and `grep -i filter FUNCTION_INDEX.md` first. Config lives in the Google Sheet `System_Config` tab (key in column A, JSON in column B, per the Config lives in Sheets note); Code.gs `load_system_config` (Code.gs:611) returns every row as `filters[key] = JSON.parse(cell)`, or the raw cell text when it is not JSON. So a cell holding `false` arrives as a boolean, a cell holding `off` arrives as the string `"off"`, a blank cell as `""`. This module must read all of those.
- Keys (exact, lowercase): `source_hirebase_enabled`, `source_hirebase_companies_per_run`, `source_hirebase_monthly_jobs`, `source_apify_enabled`, `source_apify_companies_per_run`, `source_apify_monthly_usd`.
- Defaults are TODAY'S behavior, so an absent key changes nothing: enabled True; `hirebase_feed.HIREBASE_COMPANIES_PER_RUN` (8); `hirebase_feed.HIREBASE_MONTHLY_JOBS` (500); `apify_feed.APIFY_COMPANIES_PER_RUN` (8); `apify_feed.APIFY_MONTHLY_CEILING_USD` (10.00). Import them, do not copy the numbers. (Whether a source actually runs still also needs its env key or token; this config can only turn a source OFF or lower its budget, never supply a credential.)
- Hard maxima, each commented `DESIGN CHOICE, not a measurement` except the Hirebase one: companies per run <= 20 for both; `monthly_jobs` <= `HIREBASE_MONTHLY_JOBS` (the Free plan's measured 500 cap: a sheet value above it is clamped DOWN to it); `monthly_usd` <= 25.00. Minimum 0 (0 disables spend without flipping the switch). A budget of 0 and `enabled` False both mean the source is off.
- `parse_source_config(filters)` -> `SourceConfig(hirebase_enabled, hirebase_per_run, hirebase_monthly_jobs, apify_enabled, apify_per_run, apify_monthly_usd, problems)`; `filters` is the dict `load_system_config` returned, or None.
  - Booleans accept JSON true/false and the strings `true/false/on/off/yes/no/1/0` in any case with surrounding spaces; anything else (including `""`) is the default AND adds a `problems` entry naming the key. A blank cell is "unset", not a problem.
  - Numbers accept int/float and numeric strings; bool, negative, NaN, infinity, and text are the default AND a `problems` entry. Per-run values are rounded DOWN to int. Over-max values are clamped and ALSO listed in `problems` so Kevin sees that his number was cut.
  - `problems` is a tuple of short strings like `source_apify_monthly_usd=999 clamped to 25.0`. Never raises, never returns None.
- Tests (`-k source_config`, at least 8): absent keys give today's defaults and no problems; `false`, `"off"`, `" No "` turn a source off; `"maybe"` keeps the default and reports the key; a number above each hard max is clamped and reported; `monthly_jobs` of 5000 clamps to 500 specifically; negative, NaN and `True` for a number are defaults with a problem; `0` budget reads as off; `filters=None` equals an empty dict; a float per-run `3.9` becomes 3.

## Log

- Attempt 1 (built): added source_config.py (parse_source_config, SourceConfig with hirebase_on/apify_on properties, maxima 20/500/25.0) and test_source_config.py. FUNCTION_INDEX grep for config/filter found no duplicate. Verify exit 0 ("19 passed in 0.05s"); test_source_config.py run: 19 passed in 0.02s. Uncommitted in worktree.
- Worker re-check: verify exit 0 (19 passed). Review (99 non-test lines): no findings (garbage enable-switch defaults ON per the task's spec; budgets still capped). Worktree full-suite gate exit 0.
