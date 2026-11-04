---
id: TASK-239
scope: main.py, test_main_integration.py, visit_alert.py, test_visit_alert.py
verify: python -m pytest test_visit_alert.py -q -p no:cacheprovider -k "visit_alert_tag" 2>&1 | tail -n 1 | grep -E "^([3-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error" && python -m pytest test_main_integration.py -q -p no:cacheprovider -k "visit_tag_ingest" 2>&1 | tail -n 1 | grep -E "^([5-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
The engine owns a `visit_tags` table (tag -> contact label). `/analytics/ingest` accepts an optional `tag` on each view, stores it on the `site_visits` row only when it maps to a known contact, and the "Someone opened" alert says "Opened by <contact>". Depends on TASK-232 and TASK-233 having merged; the site half (forwarding the tag) is not in this repo.

**Replaces TASK-210**, which the cloud worker BLOCKED only because its dependencies TASK-206 and TASK-208 were not in `done/` (its Log: "may be runnable if Kevin re-points"); no build was attempted. Those two were themselves blocked and are superseded by TASK-232 and TASK-233, which are now in `.queue/done/` and merged into `main`. This copy points at them. Nothing about the target changed. **TASK-209 is NOT a dependency**: it needs TASK-207 (a live network measurement Kevin runs locally) and stays held; the Notes below already say what to do if it has merged and what to do if it has not.

## Proof it is needed
Both halves fail now (observed 2026-10-28): `test_visit_alert.py -k visit_alert_tag` ends `14 deselected in 0.03s`, and `test_main_integration.py -k visit_tag_ingest` ends `1318 deselected in 1.09s`, so the chained verify exits 1. Today `record_site_visits` (main.py:19872) reads only `ip`, `ua`, `path`, `referrer`, `ts`, `self` from a view, and `site_visits` has no tag column. Pass-regex proven able to pass: `test_visit_alert.py -k visit` ends `14 passed`; `test_main_integration.py -k hirebase_lookupstep` ends `10 passed, 1308 deselected`.

## Notes
- **Correction to the request's premise, from reading montelattice-site/visits.py:** the `?` cut at main.py:19885 is NOT what loses the tag. The site forwards `request.path`, which excludes the query string, so `/?r=abc` already arrives here as `/`. The tag can only arrive if the site adds a field. **The contract for the site half: each forwarded view dict gets an optional string key `"tag"`, taken from the query parameter `r`** (`request.args.get("r", "")` in `visits.capture`). The engine ignores a missing, blank or malformed `tag`, so deploying this task before the site changes breaks nothing.
- Run `grep -i tag FUNCTION_INDEX.md` and `grep -i visit FUNCTION_INDEX.md` first. No tag table exists.
- Pure helpers in `visit_alert.py` (add, do not fork a module): `valid_tag(text)` -> the lowercased tag when it matches `^[a-z0-9]{6,12}$`, else `""`; `new_tag(rng=None)` -> an 8-character tag from `secrets` over lowercase letters and digits without `0 o 1 l i`. Random on purpose: a guessable tag like `jane` lets anyone fake being her in the alert.
- In `main.py`: table `visit_tags (tag TEXT PRIMARY KEY, label TEXT NOT NULL, created_at TEXT NOT NULL)` next to `site_visits` (main.py:1724), column `ALTER TABLE site_visits ADD COLUMN tag TEXT` (same try/except pattern as main.py:1430); `visit_tag_create(label, now=None)` -> the tag (label trimmed, whitespace collapsed, capped at 80 chars, blank refused with None; creating the same label again returns the EXISTING tag, so a repeat chore never makes duplicates; retry on a PK collision); `visit_tag_lookup(tag)` -> the label or None (None on a read error too: an unreadable table must not read as a person).
- Ingest: `tag = visit_alert.valid_tag(v.get("tag"))`; look it up once per view; store `tag` on the row ONLY when the lookup found a label (an unknown tag is stored NULL and never shown, so a guessed `?r=` cannot name anyone). The alert gets `who=label` via `format_visit_alert(..., who=label)` (TASK-233). `_send_visit_alert` and the candidate tuple are also changed by TASK-209; if 209 has merged, extend its tuple, do not rebuild it.
- Known limit, state it in a comment: the gate allows one alert per visitor per 10 minutes, so a visitor who opens an untagged page first and the tagged link second within that window is not re-alerted. A tagged link is nearly always the first page, so this is accepted.
- Never log or store the label with the ip. The alert already goes only to Kevin's Telegram chat.
- Tests: pure (`-k visit_alert_tag`, at least 3): `valid_tag` accepts a good tag in any case and rejects `../x`, `<script>`, a 200-character string, `abc`, `None`; `new_tag` is 8 characters, only the allowed alphabet, and two calls differ. Integration (`-k visit_tag_ingest`, at least 5; drive `/analytics/ingest` through the Flask client and read back `site_visits` and the fake Telegram text): a known tag -> "Opened by Jane Doe" in the alert and the row's tag stored; an unknown well-formed tag -> no "Opened by" line, row tag NULL; a malformed tag -> same as none; a tagged probe path -> no alert; a `visit_tags` read error -> the alert still goes out with no "Opened by" line; a label containing `<b>` is escaped; a view with no `tag` key produces today's alert exactly.

## Log

- 2026-10-31 cloud worker: built; verify 3 + 8 passed; full suites 1879 passed, 1 skipped (incl. test_visit_alert.py). No existing test edited.
