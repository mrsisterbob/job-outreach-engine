---
id: TASK-260
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -p no:cacheprovider -k warm_run_report 2>&1 | tail -n 1 | grep -E "^([8-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
Each `/w` run's report (the `run_report` snapshot, the full closing text and the running commit) can be written to and read back from a `warm_run_reports` table that keeps the newest 10 runs, and `/health` additionally returns the running build (`commit`, `short`, `source`); nothing calls the store from `/w` yet.

## HOLD: depends on TASK-257 and TASK-258 being in `done/`
Block, and write nothing, if either is missing: this task imports `build_info` and `run_report`.

## Proof it is needed
`python -m pytest test_main_integration.py -q -p no:cacheprovider -k warm_run_report` on 2026-10-31 (main at 4760805) selects nothing (exit 5), so the verify exits 1. Pass-regex proven able to pass: `-k hirebase_lookupstep` ends `10 passed, 1412 deselected` and matches `^([8-9]|[1-9][0-9]+) passed`. Today `grep -n "warm_run_reports\|_warm_run_report" main.py` finds nothing, and `/health` (main.py:19467) has no build field.

## Notes
- Search first: `grep -i -E "stage_samples|health|persistence" FUNCTION_INDEX.md`. Copy the shape of `warm_stage_samples`: table at main.py:1361, `_warm_stage_samples_write` / `_warm_stage_samples_read` at main.py:15692-15716 (BEGIN IMMEDIATE, insert, delete beyond the newest N, never raises on write).
- **Table**, created beside `warm_stage_samples` in the schema block: `warm_run_reports (run_id TEXT PRIMARY KEY, run_at TEXT NOT NULL, commit_sha TEXT NOT NULL DEFAULT '', commit_source TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')`. `WARM_RUN_REPORTS_KEEP = 10` (comment "DESIGN CHOICE, not a measurement").
- `_warm_run_report_write(snapshot, detail_text)` -> `True`/`False`, never raises. `run_id` and `run_at` come from the snapshot (`run_report` made them; this function adds no clock of its own except as a fallback when `started_at` is missing). `commit_sha`/`commit_source` come from `snapshot["build"]`. `detail_text` is stored cut at 60,000 characters with a final line `... (cut at 60,000 characters)` when cut; the cut is the only loss. A failed write logs `type(e).__name__` only (no body, no token) and returns `False`. The same `run_id` twice replaces the row (a rerun inside one second must not crash).
- `_warm_run_report_read(nth=1)` -> `{"run_id", "run_at", "commit_sha", "commit_source", "snapshot": dict, "detail": str}` of the nth newest, or `None` when there is none. A read error RAISES (the caller says "could not read"; an unreadable store must not look like "no run recorded"), as `_warm_stage_samples_read` does. `_warm_run_report_list(limit=10)` -> newest first `[(run_id, run_at, commit_sha)]` for the command in TASK-262.
- **Build, once.** Module-level `BUILD_INFO = build_info.running_commit(os.environ, read_file=<first line of the file or None>, run_git=<subprocess git rev-parse HEAD with timeout 3, or raise>)` computed ONCE at import (never inside a request: `/health` must stay under 5 ms and a `git` call per request would not). Wrap the whole thing so an import-time failure leaves `{"commit": "", "short": "", "source": "unknown"}`, never an import error.
- **`/health`**: add one key `"build": {"commit": ..., "short": ..., "source": ...}` to the success JSON and keep every existing key and value as is. The error branch of `health_check` is left as it is. Existing health tests must pass unchanged; if one compares the whole dict, do not edit it: block and name it.
- Tests (`test_warm_run_report_...`, at least 8, real DB): (1) write then read round-trips snapshot, detail, commit; (2) the 11th write deletes the oldest, the newest 10 remain; (3) the same `run_id` twice leaves one row with the newer content; (4) `nth=2` returns the previous run; (5) read on an empty table is `None`; (6) a 70,000-character detail is stored cut with the cut marker and nothing else lost; (7) a write that raises (monkeypatch `get_db_conn`) returns `False` and logs no snapshot text; (8) a read that raises propagates; (9) `/health` JSON has `build` with the three keys and still has `status, mode, elapsed_ms, persistence`; (10) `BUILD_INFO` falls back to `source == "unknown"` when env, file and git all fail (call the builder with failing stubs rather than reloading `main`).

## Log

