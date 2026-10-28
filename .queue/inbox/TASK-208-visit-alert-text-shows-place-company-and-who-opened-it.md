---
id: TASK-208
scope: visit_alert.py, test_visit_alert.py
verify: python -m pytest test_visit_alert.py -q -k "visit_alert_geo" 2>&1 | tail -n 1 | grep -E "^([4-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
`format_visit_alert` can show where the visitor is (a place, plus the company when there is one) and which contact opened it, without changing a single byte of today's three-argument alert.

## Proof it is needed
`python -m pytest test_visit_alert.py -q -k visit_alert_geo` collects nothing, so the verify exits 1 (observed 2026-10-28: `14 deselected in 0.03s`). Today `format_visit_alert(when_text, path, referrer)` (visit_alert.py:66) has no place, company or contact parameter. Pass-regex proven able to pass: `-k visit` on the same file ends `14 passed`.

## Notes
- Run `grep -i alert FUNCTION_INDEX.md` first. Only this function changes. The module docstring says no IP, user agent or visitor id can reach the message: keep that true and update the sentence to name what may now appear (a coarse place, a company name, a contact label).
- New signature: `format_visit_alert(when_text, path, referrer, place=None, org="", who=None)`.
  - `place=None`: no lookup was attempted (feature off): no location line, output identical to today.
  - `place=""`: a lookup was attempted and gave nothing usable (errored, rate limited, private address): a line `📍 location unknown`. An errored lookup must read as unknown, never as a place, so there is no path that turns an error string into text here.
  - `place="Grand Rapids, Michigan"`: `📍 Grand Rapids, Michigan`, and when `org` is non-empty `📍 Grand Rapids, Michigan · Steelcase Inc.`.
  - `who="Jane Doe"` (a contact label): a line `👤 Opened by Jane Doe` placed right under the title line. `who=None` or blank: no line.
  - Everything goes through the existing `esc` (html.escape); cap `who`, `place` and `org` at 80 characters each so an odd provider string cannot flood the message.
- Existing 11+ tests in `test_visit_alert.py` must pass unchanged (one of them is the no-extras byte-for-byte guarantee; keep it).
- Tests (`-k visit_alert_geo`, at least 4): the three-argument output equals the old output exactly; `place=""` prints `location unknown` and no empty place; place plus org prints both, org omitted when `""`; `who` prints under the title and HTML in `who`/`place`/`org` is escaped (`<b>` becomes `&lt;b&gt;`).
- Wiring is TASK-209 and TASK-210. Do not touch `main.py`.

## Log
