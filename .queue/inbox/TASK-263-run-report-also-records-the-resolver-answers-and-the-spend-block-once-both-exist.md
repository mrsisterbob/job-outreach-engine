---
id: TASK-263
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -p no:cacheprovider -k warm_run_resolver 2>&1 | tail -n 1 | grep -E "^([4-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
The run report from TASK-261 also carries the ATS resolver's per-company answer (board found, no board, errored; never "no jobs") and the spend block's money line, so the short Telegram message shows what the run cost and `/wrun <company>` shows which source finally answered.

## HOLD: depends on TASK-245, TASK-246, TASK-252 and TASK-261 all being in `done/`
Block, and write nothing, if any is missing. 245 and 246 add the resolver's loop and its closing lines; 252 adds the `Spend this run and month:` block; 261 adds `run_recorder` and the short message. This task only connects them.

## Proof it is needed
`python -m pytest test_main_integration.py -q -p no:cacheprovider -k warm_run_resolver` on 2026-10-31 (main at 4760805) selects nothing (exit 5), so the verify exits 1. Pass-regex proven able to pass: `-k hirebase_lookupstep` ends `10 passed, 1412 deselected` and matches `^([4-9]|[1-9][0-9]+) passed`. Without this, a company answered only by the resolver would show no source in `/wrun`, and the short message would carry no money figure even though `/w` spent some.

## Notes
- Search first: `grep -i -E "run_recorder|note_source_call|resolver_status|spend" FUNCTION_INDEX.md main.py | head`. Read where 245 calls the resolver and records `resolver_status`, and where 252 builds `spend_view.render_lines(rows)`.
- Resolver: for each company the resolver was asked, `run_recorder.note_source_call("resolver", sent_name, status=<ok/empty/errored/skipped>, returned=<rows>, kept=<1 when a board was found else 0>, dropped=<(returned name, reason) pairs for rows the resolver's own name rule refused, the Haworth-ambiguous case>, error=<short reason>)`. An unresolved or errored company is `empty` / `errored`, never anything that reads as "no jobs"; the report text for it says the board is unknown.
- Money: pass the spend block's lines (the `Spend this run and month:` heading plus its lines, as 252 renders them) to `compose_short(..., money_line=...)` instead of the Hirebase/Apify lines 261 passed, collapsed to at most 4 lines (one per paid source). When 252's block is empty, keep 261's behaviour. Do not change what the spend block says or how it is computed.
- Counts: put the resolver's asked/answered counts in `counts` so the sources line in the short message can show `Resolver 3 asked, 1 board found`; if that needs a new key in `compose_short`'s sources line, that is a change to `run_report.py`, which is NOT in scope: put it in the Log as a follow-up instead and keep the sources line unchanged.
- Existing tests: change none. If one fails, block and name it.
- Tests (`test_warm_run_resolver_...`, at least 4, harness as TASK-245's tests): (1) a company the resolver answered shows `resolver` in the stored snapshot with `kept == 1`; (2) an errored resolver call is `errored` in the snapshot and in `/wrun`'s company text, and the text never says "no jobs"; (3) the short message contains the spend lines and stays within 12 lines and 1,400 characters when all four paid sources have a line; (4) with the resolver off, no `resolver` entry exists in the snapshot and the short message is unchanged from TASK-261's.

## Log

