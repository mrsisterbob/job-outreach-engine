# Suite time report (TASK-105)

Measured 2026-10-20 on Kevin's Windows machine (12 logical CPUs). Run from the TASK-105 worktree (same code as main, nothing
changed) under the main tree's venv (`C:/Users/kjmil/job-outreach-engine/.venv/Scripts/python`, pytest 9.1.1).
Command: `python -m pytest test_main_integration.py test_pipeline_utils.py -q -p no:cacheprovider --durations=15`.
Result: 1235 passed, 1 skipped (the skip is the montelattice-site end-to-end test, `test_main_integration.py:11913-11914`,
which cannot run in a worktree).

## Where the time goes

Quiet machine: yes

Check: `Get-CimInstance Win32_Process | ? CommandLine -match 'pytest'` found 0 processes immediately before the run (1:25:23 PM)
and 0 immediately after (1:27:36 PM). An earlier look at 1:23 PM found two other sessions' Stop-hook suites still running, so I
did not time anything then; I waited until both had exited.

Suite wall time: 130s (pytest printed `129.77s (0:02:09)`; an outer clock around the whole command read 130.40s)

A second run of the same command (without `--durations`), also bracketed by 0 pytest processes before (1:30:35 PM) and after (1:32:07 PM),
finished in `88.41s (0:01:28)`, 1235 passed, 1 skipped. So even with the machine clean of other pytest processes, the same code measured
129.77s and 88.41s; the 41s gap was not explained (I did not measure other load that is not pytest, such as a browser or antivirus), and the
per-test seconds in the table below are from the 129.77s run and are not a fixed property of the tests.

| Seconds | Phase | Node id |
|---|---|---|
| 24.97s | call | test_pipeline_utils.py::test_quality_floor_bank_wording |
| 24.48s | call | test_main_integration.py::test_d_prints_power_bi_dashboard_above_market_breakout |
| 8.51s | call | test_pipeline_utils.py::test_d_summary_leads_with_proven_thing |
| 7.96s | call | test_pipeline_utils.py::test_d_skills_two_labeled_lines |
| 6.94s | call | test_pipeline_utils.py::test_contact_line_legible_on_resume_and_letter |
| 5.89s | call | test_pipeline_utils.py::test_signal_title_carries_intern |
| 3.36s | call | test_main_integration.py::test_every_track_still_compiles_to_one_page |
| 2.64s | call | test_main_integration.py::test_all_eight_tracks_compile_to_a_pdf |
| 2.31s | call | test_pipeline_utils.py::test_d_never_prints_the_401k_claim_twice |
| 1.79s | call | test_main_integration.py::test_second_click_redirects_to_the_same_draft_without_a_telegram_ping |
| 1.70s | call | test_main_integration.py::test_repeat_click_on_a_threaded_draft_does_not_create_a_second |
| 1.69s | call | test_main_integration.py::test_worst_case_tag_distribution_still_fits_one_page |
| 1.61s | call | test_pipeline_utils.py::test_skills_row_under_summary_for_d_h_b |
| 1.54s | call | test_pipeline_utils.py::test_resume_render_blocks_banned_words |
| 1.25s | call | test_main_integration.py::test_footer_surfaces_banked_skills_compiles_to_one_page |

What the slow ones do. Twelve of the fifteen compile real PDFs: they call `resume_engine.compile_resume_pdf`, which is an
in-process `typst.compile(markup.encode())` (`resume_engine.py:728`, cover letters at `:801`), then count pages with pypdf. No
subprocess, no sleep and no network is involved in any of them (I found no `sleep` in the Gmail-draft tests either). The top two
are loops of compiles: `test_quality_floor_bank_wording` (`test_pipeline_utils.py:3824`) compiles 2 tones x (8 tracks x 10 routings
+ 2 extra for track d) = 164 PDFs in 24.97s (0.15s each, derived from the loop bounds); `test_d_prints_power_bi_dashboard_above_market_breakout`
(`test_main_integration.py:13179`) compiles 2 tones x (60 ranked + 5 named routings) = 130 PDFs in 24.48s (0.19s each). The next
two are 8 tracks x 2 tones x 2 job texts = 32 compiles each (`test_pipeline_utils.py:3694`, `:3727`). The twelve compile tests sum
to 91.61s. `test_resume_render_blocks_banned_words` (`test_pipeline_utils.py:3528`) only renders Typst markup (many
`render_typst_markup` calls, no compile). The two draft-click tests (`test_main_integration.py:1316`, `:1428`) stub Gmail HTTP and write
SQLite; why they cost 1.7s each I did not isolate. All fifteen entries are `call` phases, so no setup or teardown step is over 1.25s.

Share of wall time: the 15 items sum to 96.64s, which is 74.5% of the 129.77s (the twelve PDF-compiling ones alone are 91.61s, 70.6%).
That leaves 33.13s for everything else: the other 1221 tests plus collection. Collection and import time, measured with two
`--collect-only` runs on the same machine: pytest reported 1.16s and 1.55s for collecting 1236 tests (outer wall including interpreter
start: 2.16s and 2.78s), i.e. about 1% of the suite wall time. The remaining 31.6-32.0s is the other 1221 tests
(about 26ms each on average); I did not run `--durations=0` to break that down further.

## Median of logged runs

Source: every `passed ... in N.NNs` suite time in the `## Log` sections of `.queue/done/*.md` (84 files) and `.queue/archive/*.md`
(5 files), extracted by regex `(\d{3,}) passed(, N word)* in (\d+(\.\d+)?)s` (suite runs only, so the 1-12 test verify
runs are excluded). The script was run from the scratchpad and is not committed.

- Count: 34 logged suite wall times (35 if TASK-78's Log line `suites 1215 passed, 1 skipped (58s)`, which has no `in`, is included; the median is
  116.0s either way).
- Median: 116.455s (the mean of the 17th and 18th of 34 sorted values, 116.0s and 116.91s). Shown below as 116s, truncated.
- Minimum: 21.90s (TASK-5, 1072 tests). Maximum: 323.07s (TASK-93).
- Mean 123.87s. Without the three early, short runs (21.90s, 27.57s, 36.81s) the median is 121.0s over n=31.
- Under contention: 0 Logs say so in words. Two runs are more than twice the median and match the 285-323s contended range quoted in the task
  Notes, TASK-85 (285.72s) and TASK-93 (323.07s), but their Logs give no cause. TASK-99's Log says a full-suite run was killed at the 600s
  tool timeout "while other python processes were running"; it printed no wall time, so it is not in the count.
- Why the early runs are short: TASK-56's Log (`.queue/done/TASK-56-render-side-projects-one-page.md:46`) records the suite runtime rising "from ~30s to
  78-207s" from the new compile tests.

Median: 116s over n=34

Kevin's figures are n=29 and a 111s median. Neither reproduces from the Logs: the count is 34, not 29, and the median is 116.455s, not
111s. I did not find a subset that reproduces 111s and did not try to fit one. This run (129.77s) is a quiet-machine measurement and is
not counted in the 34.

## Parallel safety

Question: can the two suites run split across processes without interfering? Method: reading `test_main_integration.py`,
`test_pipeline_utils.py` and the main.py code they exercise for anything that lives outside one process's memory. Python globals,
caches and monkeypatches are per interpreter, so they are never shared across worker processes; only files, the OS and the SQLite file
can collide.

Plugin check: `pytest-xdist` is NOT installed in `.venv` (`pip list` shows only `pytest 9.1.1` among pytest/xdist/forked/parallel/randomly)
and NOT installed in the system Python the hook uses (`C:/Users/kjmil/AppData/Local/Microsoft/WindowsApps/python3.12.exe -m pip list`: the same single
`pytest 9.1.1`). No other parallel plugin is present. Installing one is out of scope for this task, so no two-worker trial was possible
and none was run. Every conclusion below is from reading, not from a parallel run.

Shared state found, with whether two processes would collide:

| Item | Where | Two processes collide? |
|---|---|---|
| Temp SQLite file for the whole suite: `tempfile.mkstemp`, then `os.environ["JOBS_DB_PATH"] = ...` before `import main` | `test_main_integration.py:28-30`; `main.py:168` (`DB_PATH = os.environ.get("JOBS_DB_PATH", "jobs_cache.db")`) | No. `mkstemp` gives every process its own unique file, and each worker would import both test modules at collection. The real `jobs_cache.db` is not touched because the env var is set first. |
| Second temp DB made by the other module: `mkstemp` + `os.environ.setdefault` | `test_pipeline_utils.py:19-21` | No collision. `setdefault` means whichever module imports first owns the DB (integration, in file order); the pipeline_utils file is then orphaned and never deleted, so each run leaves one empty `.db` in the temp dir (cosmetic leak). |
| `clean_tables` autouse fixture deletes from 15 tables before every test | `test_main_integration.py:38-58` | No across processes (it deletes from that process's own file). |
| Module teardown deletes the DB and its `-wal`/`-shm` files | `test_main_integration.py:68-73` | No collision, but an order hazard: serially, the integration module finishes first and removes the DB, then pipeline_utils tests run against a re-created file. `test_pipeline_utils.py:2479` already has to call `m.init_db()` for this reason ("the table may not exist when another suite owns the DB file"). Under xdist a worker that leaves and re-enters a module would hit the same missing-table failure. |
| SQLite is opened in WAL mode with a busy timeout | `main.py:845-867` (`get_db_conn`) | No. Each process has its own file; WAL would also tolerate concurrent writers to one file. |
| Background daemons (Gmail poller, outbox worker, digest, backup scheduler) | `main.py:15007` skips them when `PYTEST_CURRENT_TEST` is set, which pytest sets in every worker | No. |
| Module-level caches: `_SENT_NAME_CACHE` `main.py:9521`, `_OPEN_FOLLOWUPS_CACHE` `main.py:15344`, `_OPEN_FOLLOWUPS_BUILD` `main.py:15376`, `_visitor_salt` `main.py:15970`, `_analytics_last_prune` `main.py:15972` | reset in `test_main_integration.py:55-57` and `:11760-11761` | No. Each worker interpreter has its own copy. |
| Template bank writes: `update_template_entry` writes `<path>.tmp` then `os.replace` | `main.py:603-606` | No. Every test that calls it uses a `tmp_path` copy: `test_main_integration.py:3930-3936` (fixture `temp_bank`), `:10877-10884`, `:10918`. The real `resume_bullets_bank.json` and `templates/` are only read (`resume_engine.py:100`). |
| Backup directory: `BACKUP_DIR` = `<repo>/backups` | `main.py:8116-8119` | No. No test calls `backup_sqlite_db`; the backup tests point `BACKUP_DIR` at `tmp_path` (`test_main_integration.py:5222`) or a non-existent temp path (`:5236`). |
| PDFs | `resume_engine.py:728`, `:801` | No. `typst.compile` returns bytes in memory; no output file or fixed temp path. |
| Subprocess with its own DB path | `test_main_integration.py:13347-13351` (`test_init_db_failure_survives`) | No. It points `JOBS_DB_PATH` at `tmp_path` (a directory, to make it unopenable). |
| Sibling-repo end-to-end test | `test_main_integration.py:11910-11919` imports `../montelattice-site`; the site writes `console_heartbeats.db` only in its heartbeat code (`montelattice-site/main.py:74`, `:723`) | Runs only in the main tree. The test does not call the heartbeat endpoint, so I found no write to the site DB; I did not trace it further. |
| Fixed paths under `reports/` or `.queue/` | none | The two suites contain no write to `reports/` or `.queue/` (a grep for file writes outside `tmp_path` found only the items above). |

Other findings that matter for splitting:
- `test_verify_suites_hook.py` is not part of the hook's command and not in these timings.
- The Stop hook has a hard 300s limit (`.claude/hooks/verify_suites.py:31`). The two high logged runs (285.72s, 323.07s) are at or over it, and
  contention from other processes is the likely cause, so more parallelism from within one session can make CPU contention worse before it helps (unmeasured).
- Splitting by file (`--dist loadfile`) would give two workers at best, and `test_main_integration.py` is 13,492 of the 17,403 lines in the two files
  and holds 7 of the 15 slowest items including the 24.48s one, so the speedup is bounded by that one file. A useful split needs per-test distribution.
- Unverified: nothing here was tried under xdist, so a collision I could not see by reading (for example an import-time side effect in
  `main.py`) would only show up in a trial.

Verdict: safe with changes

Changes it would need (findings, not edits):
1. A parallel runner has to be installed in the venv and in the system Python the hook uses (`pytest-xdist`; neither has it), then trialed on a quiet machine.
2. `teardown_module` in `test_main_integration.py:68-73` deletes the shared DB; under per-test distribution it should not delete a file other tests in the same worker still need
   (or the missing-table case at `test_pipeline_utils.py:2479` has to be handled by every DB-touching test).
3. The orphaned `mkstemp` file at `test_pipeline_utils.py:19-21` should be created only if the integration module has not already set `JOBS_DB_PATH` (cosmetic).
4. The Stop hook's subprocess call (`.claude/hooks/verify_suites.py:56`) would need the worker flag added.
