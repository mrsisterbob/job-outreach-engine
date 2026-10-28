---
id: TASK-233
scope: visit_alert.py, test_visit_alert.py
verify: python -m pytest test_visit_alert.py -q -p no:cacheprovider 2>&1 | tail -n 1 | grep -E "^(1[8-9]|[2-9][0-9]|[1-9][0-9]{2,}) passed" | grep -Ev "failed|error"
---

## Target
`format_visit_alert` can show where the visitor is (a place, plus the company when there is one) and which contact opened it, without changing a single byte of today's three-argument alert.

**Replaces TASK-208**, which the cloud worker built and then BLOCKED because the mandated 6-parameter signature necessarily breaks one existing test and the standing rule forbids editing tests. The built work lives only on a container-local branch (`task/TASK-208`) and is not recoverable, so this task carries the whole original target and gives permission for the ONE test edit below.

## Permission to edit exactly one existing assertion, and why
In `test_visit_alert.py`, in `test_visit_alert_format_carries_path_host_and_escapes`, change the last line (line 77 on da008fd):
`assert len(inspect.signature(va.format_visit_alert).parameters) == 3`
to `== 6`. It is an arity guard on the old three-argument signature; the mandated new signature `format_visit_alert(when_text, path, referrer, place=None, org="", who=None)` has 6 parameters (the cloud worker saw `assert 6 == 3`). Change nothing else in that test or any other existing test: the `/job-engine`, escaping, `direct` and length assertions above it in the same test are the byte-for-byte guarantee and must pass unchanged. If any other existing test fails, do not edit it: block the task and name it in the Log.

## Proof it is needed
`python -m pytest test_visit_alert.py -q -p no:cacheprovider` on 2026-10-28 (main at da008fd): `14 passed`, below the required 18, so the verify exits 1. Pass-regex proven able to pass: `python -m pytest test_apify_feed.py -q` ends `44 passed` and matches. Today `format_visit_alert(when_text, path, referrer)` (visit_alert.py:66) has no place, company or contact parameter. Cloud worker's result for the original: with the new signature and 4 `visit_alert_geo` tests, the file ran `1 failed, 17 passed`, the failure being the arity assert above.

## Notes
- Run `grep -i alert FUNCTION_INDEX.md` first. Only this function changes. The module docstring says no IP, user agent or visitor id can reach the message: keep that true and update the sentence to name what may now appear (a coarse place, a company name, a contact label).
- Signature: `format_visit_alert(when_text, path, referrer, place=None, org="", who=None)`.
  - `place=None`: no lookup was attempted (feature off): no location line, output identical to today.
  - `place=""`: a lookup was attempted and gave nothing usable (errored, rate limited, private address): a line `📍 location unknown`. An errored lookup must read as unknown, never as a place, so there is no path that turns an error string into text here.
  - `place="Grand Rapids, Michigan"`: `📍 Grand Rapids, Michigan`, and when `org` is non-empty `📍 Grand Rapids, Michigan · Steelcase Inc.`.
  - `who="Jane Doe"` (a contact label): a line `👤 Opened by Jane Doe` placed right under the title line. `who=None` or blank: no line.
  - Everything goes through the existing `esc` (html.escape); cap `who`, `place` and `org` at 80 characters each so an odd provider string cannot flood the message.
- The existing tests in `test_visit_alert.py` must pass unchanged apart from the one assertion named above (one of them is the no-extras byte-for-byte guarantee; keep it).
- New tests (named `test_visit_alert_geo_...`, at least 4): the three-argument output equals the old output exactly; `place=""` prints `location unknown` and no empty place; place plus org prints both, org omitted when `""`; `who` prints under the title and HTML in `who`/`place`/`org` is escaped (`<b>` becomes `&lt;b&gt;`). Total file count must reach 18.
- Wiring is TASK-209 and TASK-210. Do not touch `main.py`.
- Dependents now point here: TASK-209 and 210 wait on THIS task (233), not on the blocked TASK-208.

## Log
