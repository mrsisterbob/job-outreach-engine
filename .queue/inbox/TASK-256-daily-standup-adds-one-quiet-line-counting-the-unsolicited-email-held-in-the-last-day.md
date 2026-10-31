---
id: TASK-256
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -p no:cacheprovider -k standup_held 2>&1 | tail -n 1 | grep -E "^([5-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
The 08:30 daily standup carries one quiet line saying how many unsolicited emails were held (not pinged) in the last 24 hours and naming up to three of them, so a real first-contact recruiter held by TASK-255 is still seen the next morning; a day with none adds no line, and a failed read says it is unavailable instead of showing zero.

## HOLD: depends on TASK-254 and TASK-255 being in `done/`
Block, and write nothing, if either is missing: this task uses `inbound_hold.format_held_line` and `held_inbound_since`.

## Proof it is needed
`python -m pytest test_main_integration.py -q -p no:cacheprovider -k standup_held` on 2026-10-31 (main at 099d737) selects nothing (`1422 deselected`, exit 5), so the verify exits 1. Pass-regex proven able to pass: `-k visit_probe_ingest` ends `5 passed, 1417 deselected` and matches `^([5-9]|[1-9][0-9]+) passed`. Today `send_daily_standup` (main.py:9971) shows the visitor line, whose bots clause (`N bot hits filtered`, `format_visitor_line`, pipeline_utils.py:2807) already counts scanner hits quietly (tests `test_visit_probe_ingest_standup_counts_human_as_visitor_and_probes_as_filtered` and the three other `visit_probe_ingest` tests prove the four paths in the request, `/.env`, `/wp-admin/`, `/phpinfo.php`, `/wp-includes/js/json2.js`, are stored as `probe` and send no alert), but held email has no count anywhere.

## Notes
- Search first: `grep -i -E "standup|held" FUNCTION_INDEX.md`. Read `send_daily_standup` and `_standup_visitor_line` (main.py:9965-10000). Add `_standup_held_line()` beside `_standup_visitor_line`: `held_inbound_since(24)` -> `inbound_hold.format_held_line(rows)` -> the line plus a newline, or `""`. It never raises (the standup must send regardless): a raised exception is logged by class name and returns `format_held_line(None)`.
- Insert it in the digest string directly after `{_standup_visitor_line()}\n` (the visitor line already ends with its own newline, so keep the spacing the digest has today when there is no held line: an empty held line adds nothing, not a blank line). Do not change any other line of the standup, and do not touch the Tuesday hub (`morning_digest_loop` dispatches it instead of this standup on Tuesdays, and it carries no visitor line either; mention that in the Log so Kevin knows Tuesdays show neither).
- The held line is information, not an alert: the standup is already sent once a day, so no new Telegram message is created.
- Do not add a command to list held mail here. The line carries sender and subject for up to three, and the tray row keeps the rest.
- **Existing tests.** The existing standup tests (`test_standup_leads_with_an_overdue_conversation` main test_main_integration.py:11013, `test_standup_names_each_auto_retired_row_and_why`:13188, the visitor ones near :13606 and :13696) must pass unchanged: with no held rows the standup text is identical to today's. If one fails, do not edit it: block and name it.
- Tests (`test_standup_held_...`, at least 5, through `m.send_daily_standup(1)` with `send_telegram_message` patched as the neighbouring standup tests do, real DB, held rows created through `m.record_held_thread` from TASK-255): (1) three held rows in the last day: the standup contains `3 unsolicited emails held, not pinged`, each sender, and no snippet text; (2) none held: the text contains neither `held` nor `📪`, and equals the text of a run with the same data and no held code path (compare to a standup built after deleting all `state='held'` rows); (3) five held rows: the first three are named and `(+2 more)` appears, newest first; (4) a held row older than 24 hours is not counted; (5) `held_inbound_since` raising: the standup still sends and contains `held-mail count unavailable`; (6) an HTML-special sender name is escaped and the digest still parses as Telegram HTML (no raw `<b>` from the sender); (7) the visitor line is unchanged when held rows exist.

## Log

