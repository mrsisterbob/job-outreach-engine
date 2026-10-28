"""Hirebase slug registry: pure routing for what /w does next with one company. No I/O, no clock."""
import json
from collections import namedtuple
from datetime import datetime, timezone

# DESIGN CHOICE, not a measurement
REGISTRY_SLUG_TTL_DAYS = 180
# DESIGN CHOICE, not a measurement
REGISTRY_NO_JOBS_TTL_DAYS = 45
# DESIGN CHOICE, not a measurement
REGISTRY_UNKNOWN_TTL_DAYS = 30

STATUSES = ("covered", "no_jobs", "unknown")

RegistryEntry = namedtuple("RegistryEntry", "key slug status checked_at")
Route = namedtuple("Route", "action slug needs_lookup")


def _age_days(checked_at, now):
    """Days since checked_at; a bad or future timestamp counts as now (age 0)."""
    try:
        dt = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (now - dt).total_seconds() / 86400.0
        return age if age > 0 else 0.0
    except Exception:
        return 0.0


def route_company(entry, now):
    """What /w does next: search_slug, skip, or search_guesses (with or without a lookup)."""
    guesses_lookup = Route("search_guesses", "", True)
    try:
        if entry is None:
            return guesses_lookup
        slug = str(entry.slug or "").strip()
        status = entry.status if entry.status in STATUSES else "unknown"
        age = _age_days(entry.checked_at, now)
        if slug:
            if age >= REGISTRY_SLUG_TTL_DAYS:
                return guesses_lookup
            if status == "no_jobs" and age < REGISTRY_NO_JOBS_TTL_DAYS:
                return Route("skip", slug, False)
            return Route("search_slug", slug, False)
        if age < REGISTRY_UNKNOWN_TTL_DAYS:
            return Route("search_guesses", "", False)
        return guesses_lookup
    except Exception:
        return guesses_lookup


def parse_seed(text, key_fn):
    """Seed JSON text -> (entries, rejected). Bad elements are counted and dropped. Never raises."""
    try:
        data = json.loads(text)
        if not isinstance(data, list):
            return [], 1
    except Exception:
        return [], 1
    entries, rejected = [], 0
    for el in data:
        try:
            name, slug, status, checked = (el[k] for k in ("name", "slug", "status", "checked"))
            if not isinstance(name, str) or not name.strip() or not isinstance(slug, str):
                raise ValueError
            if status not in STATUSES:
                raise ValueError
            if status in ("covered", "no_jobs") and not slug.strip():
                raise ValueError
            day = datetime.strptime(str(checked), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            entries.append(RegistryEntry(key_fn(name), slug.strip(), status, day.isoformat()))
        except Exception:
            rejected += 1
    return entries, rejected
