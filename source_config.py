"""Validated config for the two paid job sources, parsed from System_Config values.

Pure logic, no I/O. Input is the dict Code.gs load_system_config returns (cells are
JSON-parsed, or raw text when not JSON). Never raises. This config can only turn a source
off or lower its budget; credentials still come from env.
"""
import math
from dataclasses import dataclass

from apify_feed import APIFY_COMPANIES_PER_RUN, APIFY_MONTHLY_CEILING_USD
from hirebase_feed import HIREBASE_COMPANIES_PER_RUN, HIREBASE_MONTHLY_JOBS

MAX_COMPANIES_PER_RUN = 20  # DESIGN CHOICE, not a measurement
MAX_HIREBASE_MONTHLY_JOBS = HIREBASE_MONTHLY_JOBS  # MEASURED: the Free plan's cap
MAX_APIFY_MONTHLY_USD = 25.00  # DESIGN CHOICE, not a measurement
# The ATS resolver's defaults live here, not in ats_resolver (which this module must not import). OFF by default:
# the actor's price is unmeasured, so a paid source never spends before Kevin turns it on.
ATS_RESOLVER_DEFAULT_ENABLED = False  # DESIGN CHOICE, not a measurement
ATS_RESOLVER_DEFAULT_PER_RUN = 3  # DESIGN CHOICE, not a measurement
ATS_RESOLVER_DEFAULT_MONTHLY_USD = 2.00  # DESIGN CHOICE, not a measurement
MAX_ATS_RESOLVER_MONTHLY_USD = 10.00  # DESIGN CHOICE, not a measurement

APIFY_PLANS = ("free", "starter")  # must match apify_feed.PRICING keys (TASK-248)
APIFY_DEFAULT_PLAN = "free"  # the only plan whose prices are measured; also the higher price, so a typo can only over-reserve

_TRUE = {"true", "on", "yes", "1"}
_FALSE = {"false", "off", "no", "0"}


@dataclass(frozen=True)
class SourceConfig:
    hirebase_enabled: bool
    hirebase_per_run: int
    hirebase_monthly_jobs: int
    apify_enabled: bool
    apify_per_run: int
    apify_monthly_usd: float
    apify_plan: str
    resolver_enabled: bool
    resolver_per_run: int
    resolver_monthly_usd: float
    problems: tuple = ()

    @property
    def hirebase_on(self):
        return self.hirebase_enabled and self.hirebase_monthly_jobs > 0 and self.hirebase_per_run > 0

    @property
    def apify_on(self):
        return self.apify_enabled and self.apify_monthly_usd > 0 and self.apify_per_run > 0

    @property
    def resolver_on(self):
        return self.resolver_enabled and self.resolver_monthly_usd > 0 and self.resolver_per_run > 0


def _unset(raw):
    return raw is None or (isinstance(raw, str) and not raw.strip())


def _parse_bool(filters, key, default, problems):
    raw = filters.get(key)
    if _unset(raw):
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower() if isinstance(raw, (str, int)) else ""
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    problems.append(f"{key}={raw!r} not a boolean, using {default}")
    return default


def _parse_number(filters, key, default, maximum, problems, as_int):
    raw = filters.get(key)
    if _unset(raw):
        return default
    value = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            value = None
    if value is None or not math.isfinite(value) or value < 0:
        problems.append(f"{key}={raw!r} not a non-negative number, using {default}")
        return default
    if as_int:
        value = math.floor(value)
    if value > maximum:
        problems.append(f"{key}={raw} clamped to {maximum}")
        return maximum
    return int(value) if as_int else float(value)


def _parse_plan(filters, key, problems):
    raw = filters.get(key)
    if _unset(raw):
        return APIFY_DEFAULT_PLAN
    text = raw.strip().lower() if isinstance(raw, str) else ""
    if text in APIFY_PLANS:
        return text
    problems.append(f"{key}={raw!r} not one of {', '.join(APIFY_PLANS)}, using {APIFY_DEFAULT_PLAN}")
    return APIFY_DEFAULT_PLAN


def parse_source_config(filters):
    """Raw System_Config dict (or None) -> SourceConfig; bad values fall back and are listed."""
    if not isinstance(filters, dict):
        filters = {}
    p = []
    return SourceConfig(
        hirebase_enabled=_parse_bool(filters, "source_hirebase_enabled", True, p),
        hirebase_per_run=_parse_number(filters, "source_hirebase_companies_per_run",
                                       HIREBASE_COMPANIES_PER_RUN, MAX_COMPANIES_PER_RUN, p, True),
        hirebase_monthly_jobs=_parse_number(filters, "source_hirebase_monthly_jobs",
                                            HIREBASE_MONTHLY_JOBS, MAX_HIREBASE_MONTHLY_JOBS, p, True),
        apify_enabled=_parse_bool(filters, "source_apify_enabled", True, p),
        apify_per_run=_parse_number(filters, "source_apify_companies_per_run",
                                    APIFY_COMPANIES_PER_RUN, MAX_COMPANIES_PER_RUN, p, True),
        apify_monthly_usd=_parse_number(filters, "source_apify_monthly_usd",
                                        APIFY_MONTHLY_CEILING_USD, MAX_APIFY_MONTHLY_USD, p, False),
        apify_plan=_parse_plan(filters, "source_apify_plan", p),
        resolver_enabled=_parse_bool(filters, "source_ats_resolver_enabled", ATS_RESOLVER_DEFAULT_ENABLED, p),
        resolver_per_run=_parse_number(filters, "source_ats_resolver_companies_per_run",
                                       ATS_RESOLVER_DEFAULT_PER_RUN, MAX_COMPANIES_PER_RUN, p, True),
        resolver_monthly_usd=_parse_number(filters, "source_ats_resolver_monthly_usd",
                                           ATS_RESOLVER_DEFAULT_MONTHLY_USD, MAX_ATS_RESOLVER_MONTHLY_USD, p, False),
        problems=tuple(p),
    )
