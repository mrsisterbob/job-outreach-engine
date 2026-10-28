---
id: TASK-206
scope: main.py, test_main_integration.py
verify: python -m pytest test_main_integration.py -q -k "visit_probe_ingest" 2>&1 | tail -n 1 | grep -E "^([4-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
A pageview for a path that is not a real site page, posted to `/analytics/ingest`, is stored with bot `"probe"`, raises no "Someone opened" Telegram alert, and shows up in the standup line as a filtered bot hit instead of a visitor. Depends on TASK-205 having merged.

## Proof it is needed
`python -m pytest test_main_integration.py -q -k visit_probe_ingest` collects nothing, so the verify exits 1 (observed 2026-10-28: `1318 deselected in 1.10s`). Today `record_site_visits` (main.py:19872) calls `visit_bot_flag(ua, ip, self, ignore)` with no path, so a `/.env` view is stored with bot NULL, passes `_VISIT_ALERT_GATE.admit` (main.py:19960, `bot is not None` is the only person/bot test) and counts as a visitor in `site_visitor_standup_line` (the 2026-10-28 standup read "111 visitors (24h) · 4 → Job Engine · 28 bot hits filtered"). Pass-regex proven able to pass: `-k hirebase_lookupstep` ends `10 passed, 1308 deselected`.

## Notes
- One change in `record_site_visits` (main.py:19885-19889): pass the already-stripped `path` to `visit_bot_flag(..., path=path)`. The stored bot column carries it, so the alert gate and the standup both see it with no further change. Do not add a second filter in `_start_visit_alerts`.
- `path` there is cut at 200 chars after splitting off `?`; classify on that same value. A path cut at 200 chars that was a real page cannot happen (all real paths are short).
- Existing visit tests are around test_main_integration.py:13437-13640 (they call `record_site_visits` and `visit_bot_flag` directly and reset `m._VISIT_ALERT_GATE`). Reuse their `_view()` helper and Flask client style for the ingest tests; read that range first.
- **Test the write path, not the return value.** Drive the real route: POST to `/analytics/ingest` with `X-Analytics-Token`, a batch of views, then read `site_visits` back and the text the fake Telegram sender received, then call `site_visitor_standup_line()`. Do not call `visit_bot_flag` and assert on it here (that is TASK-205's test).
- Tests (`-k visit_probe_ingest`, at least 4): (1) a batch of the probe paths from the 2026-10-28 list plus one real `/` view: the stored rows hold `bot='probe'` for the probes and NULL for `/`, and exactly ONE alert is sent (the `/` view); (2) a probe alone sends no alert and stores one row per probe; (3) the standup line after (1) counts the human `/` as 1 visitor and the probes in "bot hits filtered", not in the visitor count; (4) an unchanged real page, `/job-engine?r=abc`, is still a person and still alerts. Also assert the hourly ceiling is not consumed by probes: after 30 probes, a real view still alerts.

## Log

- Attempt 1 (2026-10-28): changed main.py record_site_visits to pass path=path to visit_bot_flag; appended 5 `visit_probe_ingest` tests to test_main_integration.py. Verify exit 0 ("5 passed, 1342 deselected").
- But existing test `test_visit_alert_ingest_switch_ceiling_batch_and_analytics_off` (test_main_integration.py:~13578) now FAILS: it ingests `_view(path="/a"), _view(path="/b"), _view(path="/c")` from one visitor and expects 1 alert; /a /b /c are not site pages, so they are now probes and send 0 (`assert 0 == 1`). The behavior is the intended new one; the test only needs real paths (e.g. "/", "/job-engine", "/lattice" - check SITE_PAGE_PATHS). I was told not to modify existing tests, so I stopped. Needs a one-line test edit by the worker/owner. Ran `-k "visit or analytics or standup"`: 1 failed, 18 passed, 1 skipped.
- BLOCKED by worker: built and its 5 visit_probe_ingest tests pass, but existing test test_visit_alert_ingest_switch_ceiling_batch_and_analytics_off fails (assert 0 == 1: it ingests paths /a /b /c, now probes by design). The only fix is editing that existing test to use real site-page paths; Kevin's standing order is not to edit tests, so held for his OK. Work is on branch task/TASK-206. Dependents TASK-209, TASK-210 (and 211 via 210) wait on this.
