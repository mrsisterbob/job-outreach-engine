---
id: TASK-248
scope: apify_feed.py, test_apify_feed.py
verify: python -m pytest test_apify_feed.py -q -p no:cacheprovider 2>&1 | tail -n 1 | grep -E "^(5[0-9]|[6-9][0-9]|[1-9][0-9]{2,}) passed" | grep -Ev "failed|error"
---

## Target
`apify_feed.actual_cost` and the worst case inside `plan_apify_run` use the MEASURED Free-plan rates by default ($0.10 per run plus $0.0025 per job returned, so 200 jobs cost $0.60 and zero rows cost $0.10), the Starter rates stay available only when a plan is named explicitly, and nothing else about the planner changes.

## Proof it is needed
`python -m pytest test_apify_feed.py -q -p no:cacheprovider` on 2026-10-31 (main at 099d737) ends `44 passed`, below the required 50 (those 44 plus at least 6 new), so the verify exits 1. The wrong number itself: `actual_cost(200)` returns 0.401 (test at test_apify_feed.py:280) while Kevin's console billed $0.60 for a 200-job run on 2026-10-26; `START_FEE_USD = 0.001` and `PER_JOB_USD = 0.002` (apify_feed.py:20-21) are the Starter prices, "Kevin-reported, NOT verified by a billed run" in the file's own comment, and `plan_apify_run` (apify_feed.py:258) builds its worst case and its monthly room from them, so the ceiling is enforced on a number a third too low. Pass-regex proven able to pass: the same command ends `44 passed`; the regex needs 50+. Memory `apify-feed-measured-facts`: four runs matched the Free formula to the cent (81 jobs $0.303, 148 $0.470, 46 $0.215, 200 $0.600).

## Permission to edit exactly these existing tests, and why
They assert Starter dollar figures and must now say which plan they mean. Edit only these, in `test_apify_feed.py`, by name: `test_apify_plan_limit_scales_but_not_below_min` (the `0.001 + 0.002 * 400` line), `test_apify_plan_ceiling_too_low_for_min_run`, `test_apify_plan_shrinks_picks_to_fit_ceiling`, `test_apify_plan_actual_cost`, `test_apify_fallthrough_prices_and_ceiling`. The edit is: pass `plan="starter"` to the call under test (or assert the renamed Starter constants), changing no other value, name or assertion. For the two ceiling tests whose comments show Starter room arithmetic, passing `plan="starter"` keeps their numbers valid. If another existing test fails, do not edit it: block the task and name it in the Log. `test_main_integration.py` is NOT in scope: it calls `actual_cost(rows)` positionally and compares against `actual_cost(2)` (lines 21947, 22039) or monkeypatches it with a one-argument lambda (line 22095), so **keep `actual_cost`'s first parameter positional and its other parameters optional**, and `main.py` must not need an edit. Run the whole `test_main_integration.py` after the change; a failure there is a block, not an edit.

## Notes
- Search first: `grep -i -E "apify|cost" FUNCTION_INDEX.md`. Read apify_feed.py:12-22 and 238-305.
- Add `Pricing = namedtuple("Pricing", "start_fee per_job")`, `PRICING = {"free": Pricing(0.10, 0.0025), "starter": Pricing(0.001, 0.002)}`, `DEFAULT_PLAN = "free"`. Comment the Free entry `MEASURED, 4 runs matched to the cent, 2026-10-26 (memory apify-feed-measured-facts)` and the Starter entry `Kevin-reported from the Starter pricing page, NOT verified by a billed run`. Keep `START_FEE_USD` and `PER_JOB_USD` names only if something still imports them (`grep -rn "START_FEE_USD\|PER_JOB_USD" *.py`); if kept, they are the DEFAULT plan's values, not Starter's.
- `actual_cost(rows_returned, plan=DEFAULT_PLAN)`: an unknown plan name falls back to Free (the HIGHER price, so an unknown plan can only over-reserve) and never raises. Negative or non-integer rows count as 0.
- `plan_apify_run(..., plan=DEFAULT_PLAN)` passes the plan into the worst-case calculation; every other line of the planner is untouched. Existing callers pass no plan and now get Free.
- Do NOT add the plan to `source_config.py` or the sheet here (TASK-251 does the wiring). Do not touch `LIMIT_PER_COMPANY`, `LIMIT_MIN`, or `APIFY_MONTHLY_CEILING_USD`.
- Tests (`test_apify_pricing_...`, at least 6): (1) `actual_cost(200)` is 0.60 and `actual_cost(0)` is 0.10, and a parametrized table of the measured runs agrees with the console figures to within 0.001: 81 rows -> 0.303, 148 -> 0.470, 46 -> 0.215, 200 -> 0.600 (the formula gives 0.3025 for 81, which the console showed as $0.303); (2) `plan="starter"` still gives 0.401 for 200; (3) an unknown plan name gives the Free number; (4) `plan_apify_run` with 8 companies has worst case 1.10 on Free and 0.801 on Starter; (5) a 1.00 room that fitted 8 companies on Starter now drops to fewer picks on Free; (6) a ledger row of $0.60 for a 200-job run leaves $9.40 of the $10 ceiling.

## Log

