---
id: TASK-243
scope: source_config.py, test_source_config.py
verify: python -m pytest test_source_config.py -q -p no:cacheprovider -k ats_resolver 2>&1 | tail -n 1 | grep -E "^([5-9]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
`parse_source_config` also returns the ATS resolver's three sheet settings (`source_ats_resolver_enabled`, `source_ats_resolver_companies_per_run`, `source_ats_resolver_monthly_usd`), validated exactly like the other two sources, with the switch OFF when the sheet says nothing, so a paid source of unmeasured price never spends before Kevin turns it on.

## Proof it is needed
`python -m pytest test_source_config.py -q -p no:cacheprovider -k ats_resolver` on 2026-10-31 (main at 099d737) selects nothing (`19 deselected`, exit 5), so the verify exits 1. Pass-regex proven able to pass: `python -m pytest test_source_config.py -q -p no:cacheprovider` ends `19 passed` and matches `^([5-9]|[1-9][0-9]+) passed` (re-check the count).

## Notes
- Read `source_config.py` whole (110 lines). Add to the dataclass `resolver_enabled: bool`, `resolver_per_run: int`, `resolver_monthly_usd: float` BEFORE `problems` (construction in `parse_source_config` is by keyword; run `grep -n "SourceConfig(" *.py` to confirm nothing builds it positionally, and fix a caller in `test_source_config.py` only if one does), plus a property `resolver_on` = enabled and monthly > 0 and per_run > 0.
- Defaults are plain constants in `source_config.py` (it must NOT import `ats_resolver`, which may not exist yet): `ATS_RESOLVER_DEFAULT_ENABLED = False`, `ATS_RESOLVER_DEFAULT_PER_RUN = 3`, `ATS_RESOLVER_DEFAULT_MONTHLY_USD = 2.00`, `MAX_ATS_RESOLVER_MONTHLY_USD = 10.00`, all commented `DESIGN CHOICE, not a measurement`. The enabled default is False on purpose: the actor's price is unmeasured when this lands. `MAX_COMPANIES_PER_RUN` (20) clamps the per-run count as for the others.
- `_parse_bool` takes its default as an argument: pass `ATS_RESOLVER_DEFAULT_ENABLED`. Do not touch the Hirebase or Apify parse lines.
- Do NOT edit `main.py` (TASK-244 adds the three keys to `SOURCE_CONFIG_KEYS`).
- Tests (`test_source_config_ats_resolver_...`, at least 5): empty config gives enabled False, per_run 3, monthly 2.00 and `resolver_on` False; `"on"`, `true` and `1` enable it; a bad boolean falls back to False and names the key in `problems`; a monthly value above the max clamps and says so; zero monthly or zero per_run makes `resolver_on` False even when enabled; the two older sources' parsed values are unchanged by the new keys.

## Log


- 2026-10-31 cloud worker: built; verify 13 passed; full suites 1919 passed, 1 skipped. main.py untouched.
