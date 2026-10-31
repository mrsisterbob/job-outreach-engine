---
id: TASK-246
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -p no:cacheprovider -k source_line 2>&1 | tail -n 1 | grep -E "^([6-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
`/w`'s closing message ends with lines that say, for the companies it worked on this run, which source answered each one (`board scan`, `Hirebase`, `Feed` or `ATS resolver`), and for each company nobody answered, the reason in plain words (`not covered`, `doubtful match`, `error`, `skipped: off`, `skipped: over budget`); a company that errored or was never asked is never shown as having no jobs.

## HOLD: depends on TASK-237, TASK-238 and TASK-245 being in `done/`
Block, and write nothing, if any is missing: each of them adds a stage whose outcome this task reports.

## Proof it is needed
`python -m pytest test_main_integration.py -q -p no:cacheprovider -k source_line` on 2026-10-31 (main at 099d737) selects nothing (`1422 deselected`, exit 5), so the verify exits 1. Pass-regex proven able to pass: `-k "hirebase_e2e or apify_wire or hirebase_empty_e2e or hirebase_slugreg"` ends `27 passed, 1395 deselected` and matches `^([6-9]|[1-9][0-9]+) passed`. Today the closing message gives per-source counts (`Hirebase: N companies, ...` main.py:16789, `Apify: ...` main.py:16825) and a coverage line (`_warm_radar_coverage_line`, main.py:15629), but nothing names which source answered a given company.

## Notes
- Search first: `grep -i -E "coverage|closing|lines.append" FUNCTION_INDEX.md`, then read the closing-message block of `run_warm_radar_scan` (find `lines.append(f"Hirebase:` by symbol) and `_warm_radar_coverage_line` (main.py:15629). Keep every existing line as it is; add after them.
- **Build a pure helper first**, in `main.py` next to `_warm_radar_coverage_line`: `_warm_radar_source_lines(answered, unanswered, limit=12)` where `answered` is `{display_name: source_label}` and `unanswered` is `{display_name: reason_label}`; it returns a list of strings and does no I/O. Output shape: one line per source that answered at least one company, `Answered by <source>: A, B, C (+N more)` (alphabetical, cut at `limit` names, the rest as a count), then one line `Not answered: A (not covered), B (error), ...` cut the same way. If both are empty it returns `[]` (no empty header). Reason labels are exactly the five in the Target; any other reason string is shown as `error`, never invented.
- **Fill the two dicts as each stage finishes**, in the one function, not by re-deriving them at the end: a company scanned as a board this run (the `_stamp_boards_scanned` path) is `board scan`; a company with kept Hirebase rows is `Hirebase`; kept Feed rows is `Feed`; a resolver `resolved` answer is `ATS resolver`. Precedence when more than one applies is the order above (free source first). A company asked of a source that answered `empty`/`error`/ambiguous and answered by no one is `unanswered` with that source's reason, and the LAST source that tried it names the reason. A company a stage skipped for budget, switch or cooldown is `skipped: off` or `skipped: over budget` only if it was in that stage's pool. A company that no stage touched this run does not appear at all (do not list the whole warm list).
- **Never "no jobs".** A company on the `Not answered` line must not be described with the words `no jobs`, `no openings` or `no postings`. A test greps the whole closing message for them. An errored stage's companies are `error`, not `not covered`.
- Do not change any CRM write, Telegram card, `died_roles` or seen-jobs behaviour. This task only reads outcomes that the stages already hold in locals.
- **Existing tests.** Tests that assert the closing message by equality may break if they hit companies in these lines; do not edit them, block and name each. Tests that match substrings are expected to keep passing.
- Tests (`test_source_line_...`, at least 6). (1)-(2) pure helper: grouping by source, alphabetical order, the `+N more` cut at the limit, empty inputs give `[]`, an unknown reason label becomes `error`; (3) driving `run_warm_radar_scan` through `_WarmRadarHarness` (test_main_integration.py:15792): one company answered by a board scan, one by Hirebase, one by the Feed, one by the ATS resolver (fakes as in TASK-245's tests): the closing message names each company under its source; (4) the free-source-first precedence: a company with both kept Hirebase rows and kept Feed rows is listed under Hirebase only; (5) the Haworth case: Feed rows rejected as `Haworth International`, resolver answers doubtful: Haworth is under `Not answered` with `doubtful match`; (6) every stage erroring: the companies are under `Not answered` as `error` and the message contains neither `no jobs` nor `no openings`; (7) a run with nothing asked prints no `Answered by` or `Not answered` line at all.

## Log

