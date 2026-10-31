---
id: TASK-240
scope: main.py, test_main_integration.py, command_help.py, test_pipeline_utils.py
verify: python -m pytest test_main_integration.py -q -p no:cacheprovider -k "vlink" 2>&1 | tail -n 1 | grep -E "^([5-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error" && python -m pytest test_pipeline_utils.py -q -p no:cacheprovider -k "vlink_help" 2>&1 | tail -n 1 | grep -E "^([2-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
`/vlink Jane Doe` in Telegram replies with that contact's link `https://montelattice.com/?r=<tag>` (creating the tag the first time, reusing it after), `/vlink` alone lists the links already made with how many times each was opened, and `/vlink` has a per-command help entry. Depends on TASK-239 having merged.

**Replaces TASK-211**, which the cloud worker BLOCKED only because TASK-210 was not done; no build was attempted. This copy depends on its replacement, **TASK-239** (it must be in `.queue/done/` first. Block if not). Nothing about the target changed.

## Proof it is needed
Both halves fail now (observed 2026-10-28 for the pattern; re-run before starting): no `/vlink` exists, so `-k vlink` collects nothing in either file. Pass-regex proven able to pass: `test_pipeline_utils.py -k help` ends `6 passed, 401 deselected`; `test_main_integration.py -k hirebase_lookupstep` ends `10 passed, 1308 deselected`.

## Notes
- Run `grep -n '"/vlink\|/vlink' main.py command_help.py` first (none on 2026-10-28) and `grep -i command FUNCTION_INDEX.md`. The nearest model is `/wstages` from TASK-196/TASK-199: copy how its handler is dispatched (the dispatch uses several forms, so match the nearest neighbour exactly) and how its HELP entry is written in `command_help.py`. The worker previously had to ask for the help entry as a follow-up; it is in scope here from the start.
- Handler: name, create-or-reuse via `visit_tag_create(label)` (TASK-239; the same label always returns the same tag). Reply: the link and one line saying what it does ("when they open it you get 'Opened by <name>'"). Base URL: a module constant `SITE_PUBLIC_URL = "https://montelattice.com"`; no trailing-slash trick, the link is `<base>/?r=<tag>`.
- No argument: list up to the 20 most recent tags as `label — link — opened N times`, where N is the count of `site_visits` rows with that tag (a plain count query; label HTML-escaped). An empty table says so plainly, with the usage line.
- A blank or over-80-character name gets a usage reply and creates nothing. A DB failure replies with a short "could not save" and never a link (a link whose tag was not saved would show as unknown forever).
- Warm-outreach rule from Kevin's memory applies to anything this command says to Kevin or generates for a contact: no negations.
- Tests (`-k vlink`, at least 5 in `test_main_integration.py`; drive the real command entry the way the `/wstages` tests do): first call creates a tag and returns a link whose tag is in `visit_tags`; second call with the same name returns the SAME link and adds no row; two names get different links; `/vlink` with no argument lists them with the opened count (seed two tagged `site_visits` rows by POSTing to `/analytics/ingest`, then assert "opened 2 times"); a blank name and a DB write failure create no link. Help (`-k vlink_help`, at least 2 in `test_pipeline_utils.py`): `/vlink` returns a real help entry, not the "No per-command help" fallback, and it mentions the `?r=` link.

## Log
