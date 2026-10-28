---
id: TASK-216
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -k "source_config_read" 2>&1 | tail -n 1 | grep -E "^([5-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
`source_config_read()` returns the validated per-source on/off and budgets from the Google Sheet `System_Config` tab, read fresh on each call, and when the sheet cannot be read it returns the LAST values it saw, never "everything on". Depends on TASK-215 having merged.

## Proof it is needed
`python -m pytest test_main_integration.py -q -k source_config_read` collects nothing, so the verify exits 1 (re-run before starting and paste the line). Why a fresh read: today System_Config values reach the engine only at boot (`hydrate_filters_from_sheets`, main.py:1801, which says "add or edit the System_Config row and restart the service"), so a budget Kevin lowers in the sheet would keep spending until the next restart. Pass-regex proven able to pass: `-k hirebase_lookupstep` ends `10 passed`.

## Notes
- Run `grep -i config FUNCTION_INDEX.md` and `grep -i filter FUNCTION_INDEX.md` first. Reuse: `crm_get({"action": "load_system_config"})` (main.py:188; Code.gs:611 returns `{"status": "success", "filters": {...}}` with every row parsed), `get_filter(key, default)` (main.py:1995), the `search_filters (key, value_json)` table, and `source_config.parse_source_config` (TASK-215). Do NOT add a new table.
- Flow: call `crm_get`; accept the reply only when the HTTP status is 200, the body status is `"success"` and `filters` is a dict (a rejected reply behind HTTP 200 is the repo's recurring failure; treat anything else as unreadable). Parse the six `source_*` keys with `parse_source_config`. On a good read, store the six keys' RAW values locally with `INSERT OR REPLACE INTO search_filters` (the same local-only write `hydrate_filters_from_sheets` does; NOT `set_filter`, which dual-writes back to the sheet) so a later unreadable read has something real to fall back to. A key absent from the sheet is DELETED from the local copy, so removing a row from the sheet restores the default instead of keeping a stale value.
- On an unreadable sheet: build the dict from `get_filter` for the six keys (values the last good read or the boot hydration stored) and parse that. A key that was never stored is its default. Return which path was taken so the caller can say it.
- Return `(SourceConfig, origin)` where `origin` is `"sheet"`, `"last seen"` or `"defaults"` (`"defaults"` only when the sheet was unreadable AND nothing was ever stored). Never raises. A DB error while storing does not change what is returned (log the class only).
- `crm_get` has a 10 second default timeout; `/w` already makes CRM calls, so one more per run is accepted. Do not add a retry loop.
- Tests (`-k source_config_read`, at least 5; fake `crm_get` at module level, real DB init): sheet says `source_apify_enabled` false -> config off, origin `"sheet"`; the next call with the sheet unreadable (raise, HTTP 500, `status: error` behind HTTP 200, `filters` a list) still returns Apify OFF with origin `"last seen"`; a never-stored unreadable first call returns defaults with origin `"defaults"`; a key removed from the sheet returns to its default on the next good read; a bad value in the sheet shows up in `problems` and is the default; the sheet's values do NOT get written back to the sheet (assert `crm_post` is never called).

## Log


- Attempt 1 (built): before change verify exit=1 (no tests collected). Added `source_config_read()` + `SOURCE_CONFIG_KEYS` in main.py (before restore_core_sourcing_filters; imports parse_source_config), 7 tests `test_source_config_read_*` appended to test_main_integration.py. Verify: exit=0, "7 passed, 1348 deselected in 3.81s". Touched-file run `-k source_config_read`: "7 passed, 1348 deselected in 0.47s" (full file not run, per instructions). No existing tests edited; not committed.
- Worker re-check: verify exit 0 (7 passed). review skipped: 51 non-test lines. Worktree full-suite gate exit 0.
