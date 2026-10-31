---
id: TASK-253
scope: main.py, command_help.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -p no:cacheprovider -k spend_command 2>&1 | tail -n 1 | grep -E "^([6-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
Kevin sends `/spend` and gets, without running `/w` and without spending anything, the same per-source spend lines `/w` ends with (month spent, the sheet's cap, what is left, and whether the Apify figure came from the console or only the ledger), plus a `/spend/` help entry.

## HOLD: depends on TASK-252 being in `done/`
Block, and write nothing, if it is not. This task reuses the helper TASK-252 builds for the closing block; if that helper is inline in `run_warm_radar_scan`, first lift it into one function (`_spend_view_rows(config, now)`), keeping `/w`'s output unchanged, and say so in the Log.

## Proof it is needed
`python -m pytest test_main_integration.py -q -p no:cacheprovider -k spend_command` on 2026-10-31 (main at 099d737) selects nothing (`1422 deselected`, exit 5), so the verify exits 1. Pass-regex proven able to pass: `-k hirebase_lookupstep` ends `10 passed, 1412 deselected` and matches `^([6-9]|[1-9][0-9]+) passed`. Today the only way to see a month figure is to run `/w`, which can spend; no command shows spend.

## Notes
- Search first: `grep -i -E "handle_.*command|spend|help" FUNCTION_INDEX.md`. Follow the closest existing read-only command. `handle_refer_command` (main.py:15594) is dispatched near main.py:17925; copy how it is routed, how it replies, and how it escapes HTML. Add the `/spend` route in the same place and the same style. Find the existing test that checks every dispatched command has a help entry or appears in the command list (`grep -n "HELP\b\|lookup_command_help\|command_list" test_main_integration.py test_pipeline_utils.py | head`) and satisfy it; memory `telegram-command-list` says the dispatch uses four forms and the BotFather list is regenerated from main.py, so check whether a hand-kept list of command names exists in `main.py` (`/help` menu text) and add `/spend` there too.
- `handle_spend_command(chat_id, text)`: read config with `source_config_read()` once, ledgers read-only, the console figure via the same call TASK-252 uses (read-only GET, spends nothing), then reply with `Spend this month:` followed by `spend_view.render_lines(rows)`. Rows for a source that is off say `off in System_Config`. Unreadable ledgers print `unknown`. **The command writes nothing**: no ledger row, no resolution row, no sheet write, no Telegram card state. A test asserts every table's row count is unchanged and the Feed, Hirebase and resolver fakes got zero calls.
- `command_help.py` HELP entry `"/spend"` (summary 1-2 plain sentences, example with a short `->` note), matching the style of the `/refer` entry (command_help.py:112).- Extra argument: `/spend` takes none; anything after it is ignored without error.
- Tests (`test_spend_command_...`, at least 6): (1) reply has a line per configured source with month, cap and left; (2) the Feed line says console when the console fake reports more than the ledger, and ledger when the console fake fails, with the failure reason; (3) a source switched off in the sheet says so; (4) an unreadable ledger prints `unknown`, never `$0.00`; (5) the command makes zero Feed, Hirebase, resolver and Apollo calls and changes no table row count; (6) `/spend/` returns the help entry; (7) the command is listed wherever the existing command-list test requires; (8) `APIFY_TOKEN` never appears in the reply or `caplog`.

## Log

