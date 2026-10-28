---
id: TASK-213
scope: resolve_plan.py, test_resolve_plan.py
verify: python -m pytest test_resolve_plan.py -q -k "resolve_plan" 2>&1 | tail -n 1 | grep -E "^([9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
A new pure module `resolve_plan.py` decides, per source, which UNRESOLVED companies get the next paid resolution attempt, in contact-weight order, skipping companies in cooldown, so the tail of the Carmen Warm list gets a turn instead of the same heavy companies winning every run. No I/O, no clock argument hidden inside.

## Proof it is needed
`python -m pytest test_resolve_plan.py -q -k resolve_plan` fails because the file does not exist (observed 2026-10-28: exit 1). Why the module is needed, measured from the code: `plan_hirebase_run` sorts `eligible.sort(key=lambda c: (-(c[2] or 0), c[0]))` (hirebase_feed.py, in `plan_hirebase_run`) and `plan_apify_run` sorts `sorted(best.values(), key=lambda c: (-c[2], c[0]))` (apify_feed.py:288), and their only memory of an empty answer is a 3 to 14 day retry (`HIREBASE_RETRY_DAYS`, `HIREBASE_EMPTY_RETRY_DAYS`) and 7 days (`APIFY_COMPANY_RETRY_DAYS`). Pass-regex proven able to pass: `test_visit_alert.py -k visit` ends `14 passed`.

## Notes
- Run `grep -i plan FUNCTION_INDEX.md` first. Do NOT touch `hirebase_feed.py`, `apify_feed.py` or `hirebase_registry.py`; they keep their own budget and watermark logic. This module only decides WHICH companies are offered to them.
- Constants, each commented `DESIGN CHOICE, not a measurement`: `EMPTY_COOLDOWN_DAYS = 45` (same figure as `REGISTRY_NO_JOBS_TTL_DAYS`), `ERROR_COOLDOWN_DAYS = 1`, `RESERVED_SHARE = 0.5` (the share of a source's per-run slots held for unresolved companies).
- Outcomes (strings, the vocabulary TASK-214 stores): `"resolved"` (the source returned rows that belong to the company, or a board token was learned), `"empty"` (the source answered cleanly with zero rows: it does not cover the company, which is NOT "no jobs"), `"error"` (the call failed: unknown, never "empty").
- `next_eligible(outcome, now)` -> ISO date-time string: `"empty"` -> now + 45 days, `"error"` -> now + 1 day, `"resolved"` -> `""` (a resolved company is scanned by the existing planners, not re-resolved). An unknown outcome is treated as `"error"`.
- `plan_resolution(candidates, resolution, source, covered_keys, per_run, now)` -> list of `(key, display, people)`:
  - `candidates`: iterable of `(key, display, people)` in any order; `resolution`: `(key, source) -> (outcome, attempted_at, next_eligible_at)`; `covered_keys`: keys that already have a working source, never offered.
  - Excluded: keys in `covered_keys`; keys whose resolution row for THIS source is `"resolved"`; keys whose `next_eligible_at` for this source is later than `now`. A malformed timestamp counts as NOT in cooldown only for `"error"` and as in cooldown for `"empty"` (a lost date must not turn a 45-day miss into a re-bill).
  - Order: never-attempted companies and expired cooldowns, by `people` descending then key ascending. A company whose cooldown just expired and a never-attempted one of the same weight: never-attempted first.
  - Takes at most `per_run`; `per_run <= 0` returns `[]`.
- `reserve_order(scan, resolve, per_run)` -> list of at most `per_run` `(key, display, people)`: guarantees `ceil(per_run * RESERVED_SHARE)` slots to `resolve` (when it has that many), fills the rest from `scan` heaviest first, then back-fills any unused slot from whichever list still has companies. WHY it exists: both existing planners sort heaviest-first, so a list of 8 resolved heavy companies would again push every unresolved one out. Passing them a list already trimmed to `per_run` means their own sort cannot change who gets in (a budget cut still drops the lightest, say so in the docstring).
- Both existing planners' per-run caps are `HIREBASE_COMPANIES_PER_RUN = 8` and `APIFY_COMPANIES_PER_RUN = 8`; TASK-217/218 pass the configured value.
- Tests (`-k resolve_plan`, at least 9): THE property test: 40 unresolved companies of descending weight, `per_run=8`, simulate 6 runs where each pick answers `"empty"` and is written into `resolution` through `next_eligible` -> across 5 runs all 40 get exactly one turn and none repeats; an `"error"` pick returns next day (`now` + 1 day) and not before; an `"empty"` pick does not return in 44 days and does in 46; `"resolved"` is never re-offered; a covered key is never offered; a malformed `empty` date stays in cooldown; `per_run=0` returns `[]`; `reserve_order` with 8 heavy scans and 3 resolves and `per_run=8` contains all 3 resolves; `reserve_order` back-fills when `resolve` is empty; tie order is deterministic.

## Log

- Attempt 1 (built): added resolve_plan.py (next_eligible, plan_resolution, reserve_order, 3 DESIGN CHOICE constants) and test_resolve_plan.py. Grep of FUNCTION_INDEX.md found no duplicate. verify exit=0 ("11 passed in 0.03s"); test_resolve_plan.py run "11 passed in 0.01s". Uncommitted. FUNCTION_INDEX.md not regenerated (outside scope).
- Attempt 2 (review fixes): reserve_order now sorts scan and resolve by (-people, key) itself (docstring updated); people coerced via _weight() (int or 0) in plan_resolution and reserve_order. Added 2 tests (unsorted scan; string/None people). verify exit=0 ("13 passed in 0.03s"); test_resolve_plan.py "13 passed in 0.02s".
- Worker re-check: verify exit 0 (13 passed). Review (109 non-test lines): fixed reserve_order sorting (spec: heaviest first) and people coercion; skipped dedupe (handled by seen set) and bad-'now' loud failure (caller bug). Worktree full-suite gate exit 0.
