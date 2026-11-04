---
id: TASK-262
scope: main.py, command_help.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -p no:cacheprovider -k wrun_command 2>&1 | tail -n 1 | grep -E "^([8-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
Kevin sends `/wrun` and gets the newest `/w` run's per-company overview; `/wrun <company>` gets that company's detail (source, name sent, names that came back, kept and dropped); `/wrun full` gets the full closing text the old long message carried; `/wrun 2` picks the run before; and `/wrun/` shows a help entry. It spends nothing and writes nothing.

## HOLD: depends on TASK-258 and TASK-260 being in `done/`
Block, and write nothing, if either is missing: this task uses `run_report.render_overview / render_company / chunk` and `_warm_run_report_read / _warm_run_report_list`.

## Proof it is needed
`python -m pytest test_main_integration.py -q -p no:cacheprovider -k wrun_command` on 2026-10-31 (main at 4760805) selects nothing (exit 5), so the verify exits 1. Pass-regex proven able to pass: `-k hirebase_lookupstep` ends `10 passed, 1412 deselected` and matches `^([8-9]|[1-9][0-9]+) passed`. Today `grep -n "/wrun" main.py command_help.py` finds nothing; the nearest command is `/wstages` (main.py:17229 region, command_help.py:194).

## Notes
- Search first: `grep -i -E "wstages|handle_.*command|help" FUNCTION_INDEX.md`. Copy `/wstages` dispatch and error replies exactly in shape (main.py, search `text == "/wstages"`): usage reply, "No /w run recorded yet.", a read error says `Could not read the /w run reports.` and logs the class name only. Find the existing test that requires every dispatched command to have a help entry or a place in the command list (`grep -n "wstages" test_main_integration.py command_help.py`) and satisfy it; memory `telegram-command-list` says the dispatch uses four forms and the BotFather list is regenerated from `main.py`, so add `/wrun` wherever `/wstages` is listed by hand.
- Argument forms (one parser, a small pure helper `_wrun_parse(arg)` -> `("overview", n) | ("full", n) | ("company", name, n) | ("usage",)`): empty -> newest overview; a number `1`-`10` -> that run's overview (1 = newest); `full` or `full 2` -> the full text of that run; anything else -> a company query against the newest run (`/wrun haworth`), with an optional trailing ` #2` for the run before (document it in the usage reply). A number outside 1-10 or `/wrun full x` gives the usage reply (`/wrun`, `/wrun 2`, `/wrun <company>`, `/wrun full`).
- Replies are sent as several messages when long: `for piece in run_report.render_overview(snapshot): send_telegram_message(chat_id, piece)`; `/wrun full` sends `run_report.chunk(detail)` pieces, the first prefixed with `run <run_id>, build <short or unknown>`. Company names and the query are HTML-escaped by `run_report`; the command does no extra escaping of its output. Cap at 6 messages per command and end with `... (N more characters, use /wrun <company>)` rather than flooding the chat.
- **Read-only.** The command writes nothing: no report row, no ledger, no sheet, no Telegram card state. A test asserts every table's row count is unchanged and zero HTTP calls were made.
- `command_help.py`: HELP entry `"/wrun"` (1-2 plain sentences; examples `/wrun`, `/wrun Haworth`, `/wrun full` each with a short `->` note), matching the style of the `/wstages` entry (command_help.py:194). `/wrun/` returns it through the existing help path.
- Tests (`test_wrun_command_...`, at least 8; write a run with `_warm_run_report_write` using a `run_report.RunRecorder` snapshot, dispatch like the `_stage_dispatch` helper at test_main_integration.py:21648): (1) `/wrun` lists each company with its sources and the `sent≠returned` clause for the Haworth case; (2) `/wrun haworth` shows source, sent `Haworth Inc.`, returned `Haworth International` and the drop reason; (3) `/wrun full` returns the stored long text and the header with the build; (4) `/wrun 2` reads the previous run and `/wrun 11` and `/wrun full x` give the usage reply; (5) no runs yet -> `No /w run recorded yet.`; (6) a store read that raises -> `Could not read the /w run reports.`, no traceback text in the reply; (7) an ambiguous query lists candidates, an unknown one says no company matches and names the run; (8) a 12,000-character stored text comes back in pieces each within 3,500 characters and at most 6 messages; (9) no table row count changes and no HTTP call is made; (10) `/wrun/` returns the help entry and the existing command-list test passes with `/wrun` included.

## Log


- 2026-10-31 cloud worker: built; verify 10 passed; suites 1953 passed, 1 skipped. No existing test edited. /wrun added to the /help list beside /wstages and to command_help.HELP.
