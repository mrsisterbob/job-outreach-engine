---
id: TASK-251
scope: source_config.py, test_source_config.py
verify: python -m pytest test_source_config.py -q -p no:cacheprovider -k apify_plan 2>&1 | tail -n 1 | grep -E "^([4-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
`parse_source_config` also returns `apify_plan` from the sheet key `source_apify_plan` (`free` or `starter`), defaulting to `free` (the only plan whose prices are measured), so the Feed's cost can be computed at the plan Kevin is actually on.

## Proof it is needed
`python -m pytest test_source_config.py -q -p no:cacheprovider -k apify_plan` on 2026-10-31 (main at 099d737) selects nothing (`19 deselected`, exit 5), so the verify exits 1. Pass-regex proven able to pass: `python -m pytest test_source_config.py -q -p no:cacheprovider` ends `19 passed` and matches `^([4-9]|[1-9][0-9]+) passed` (re-check the count). Today there is no plan setting: `apify_feed.actual_cost` has one set of prices, and Kevin moving from Free to the $19 Starter plan would change every price with no way to say so.

## Notes
- Read `source_config.py` whole (110 lines; if TASK-243 has merged it also holds the resolver fields, keep them). Add `apify_plan: str` to `SourceConfig` BEFORE `problems`; construction is by keyword. Do not import `apify_feed` for the plan names beyond what the file already imports: define `APIFY_PLANS = ("free", "starter")` and `APIFY_DEFAULT_PLAN = "free"` here with a comment `must match apify_feed.PRICING keys (TASK-248)`, and a test that fails if the two ever differ (it may import `apify_feed` in the TEST only; if TASK-248 is not merged yet, make that one test skip with a clear reason rather than fail).
- Parse: absent or blank is `free` with no problem; the value is case-insensitive and trimmed; an unknown value falls back to `free` and names the key in `problems` (`free` is the higher price, so a typo can only over-reserve).
- Do NOT edit `main.py`: TASK-252 adds `source_apify_plan` to `SOURCE_CONFIG_KEYS` and passes the plan on.
- Tests (`test_source_config_apify_plan_...`, at least 4): empty config is `free` and no problem; `"Starter "` is `starter`; `"enterprise"` falls back to `free` and lists the key in `problems`; a number or `True` in the cell falls back to `free` with a problem; the two older sources' parsed values are unchanged; the plan names equal `apify_feed.PRICING` keys.

## Log

