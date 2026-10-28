---
id: TASK-212
scope: company_source.py, test_company_source.py
verify: python -m pytest test_company_source.py -q -k "company_source" 2>&1 | tail -n 1 | grep -E "^([8-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
A new pure module `company_source.py` turns the four places that today each hold part of "what covers this company" into ONE record per company, and counts the share of warm CONTACTS (not companies) whose employer has a working job source. No I/O, no clock, no `main.py`.

## Proof it is needed
`python -m pytest test_company_source.py -q -k company_source` fails because the file does not exist (observed 2026-10-28: exit 1 / `no tests ran` or `file or directory not found`). Pass-regex proven able to pass: `test_visit_alert.py -k visit` ends `14 passed`.

## Notes
- Run `grep -i registry FUNCTION_INDEX.md`, `grep -i identity FUNCTION_INDEX.md` and `grep -i coverage FUNCTION_INDEX.md` first. Nothing builds a per-company record. `_warm_radar_coverage_line` (main.py:15317) already splits contacts by board status; this module does NOT replace it, TASK-220 adds one number beside it.
- **The record is DERIVED, not a fifth store of truth.** The answer is split across `company_identities` (`ats_slug`, `workday_board`, `board_checked_at`, `board_attempted_at`; main.py:1516), `hirebase_slug_registry` (`RegistryEntry(key, slug, status, checked_at)`, `hirebase_registry.py:18`, statuses `covered|no_jobs|unknown`), `company_feed_source` (TASK-201: `key -> (source_domain, job_board, rows, checked_at)`) and the new `company_resolution` rows (TASK-214). The module reads plain dicts of those and invents no new data.
- `build_source_records(companies, identities, registry, feed_source, resolution, now)`:
  - `companies`: iterable of `(key, display, contacts)`.
  - `identities`: `key -> {"ats_slug", "workday_board"}` (strings, blank = none). The CALLER passes only a workday triple that already passed `valid_workday_triple` (main.py:3936); this module treats any non-blank value as a board and never validates.
  - `registry`: `key -> RegistryEntry`-like with `.slug`, `.status`, `.checked_at`.
  - `feed_source`: TASK-201 shape. `resolution`: `(key, source) -> (outcome, attempted_at, next_eligible_at)` (TASK-214 shape).
  - Returns a list of dicts, sorted contacts descending then key ascending (deterministic, so a sheet rewrite does not reshuffle). Keys: `key, display, contacts, board_system, board_token, covered_by, feed, last_result, last_checked_at, next_eligible_at, covered`.
- Field rules:
  - `board_system`: `"workday"` when `workday_board` is set, `"ats"` when only `ats_slug` is set, else `""`. The stored slug carries NO system name (Greenhouse/Lever/Ashby/SmartRecruiters is not recorded anywhere), so do not invent one; say so in the docstring.
  - `board_token`: the workday triple, else the slug, else `""`.
  - `covered_by`, first match wins: `"board"` (a board token) -> `"hirebase"` (registry `status == "covered"` with a non-blank slug) -> `"feed"` (`feed_source` rows > 0) -> `""`. `covered` is `covered_by != ""`. The `feed` field names the feed behind a feed cover: the `job_board` text, else `"apify"`.
  - `last_result` is a short text such as `board`, `hirebase:covered`, `hirebase:no_jobs`, `apify:empty`, `apify:error`, `none`. `last_checked_at` is the newest of the registry `checked_at`, the feed `checked_at`, the resolution `attempted_at` and the identity-less blank. `next_eligible_at` is the EARLIEST non-blank `next_eligible_at` among the company's resolution rows, `""` when covered or when there is none.
  - A malformed entry (None, wrong type, missing field) is read as "nothing recorded" for that source; the function never raises.
- `covered_contact_share(records)` -> `(covered_contacts, total_contacts)`: the SUM of `contacts`, never a count of records. A company with 40 contacts that is covered counts 40.
- Sheet contract, so the engine and the Apps Script agree (there is no shared schema between them, see the Apps Script contract drift note): a module constant `COMPANIES_HEADER` (tuple of column titles, one per record field above, `key` first) and `record_row(record)` -> list of strings in that order (`covered` as `yes`/`no`, ints as text, never None). TASK-219 writes exactly these.
- Tests (`-k company_source`, at least 8): board wins over a registry cover; registry cover when no board; feed cover only when rows > 0; uncovered company has `covered_by == ""`; `next_eligible_at` is the earliest of two resolution rows; a bad registry entry and a bad feed tuple do not raise; the SHARE counts contacts (companies of 40 covered + 2 uncovered with 1 contact each = `(40, 42)`, not 1 of 3); `record_row` has the same length as `COMPANIES_HEADER` and contains no None; ordering is contacts desc then key.

## Log

