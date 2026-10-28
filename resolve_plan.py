"""Which UNRESOLVED companies get the next paid resolution attempt (pure, no I/O).

hirebase_feed.plan_hirebase_run and apify_feed.plan_apify_run both sort heaviest-first and
only remember an empty answer for days, so the same heavy companies win every run and the
tail of the Carmen Warm list never gets a turn. This module decides WHICH companies are
offered to them; their own budget and watermark logic is untouched.

`now` is always passed in (a datetime); nothing here reads the clock.
"""
import math
from datetime import datetime, timedelta, timezone

# DESIGN CHOICE, not a measurement: same figure as REGISTRY_NO_JOBS_TTL_DAYS.
EMPTY_COOLDOWN_DAYS = 45
# DESIGN CHOICE, not a measurement: an error is unknown, so retry soon.
ERROR_COOLDOWN_DAYS = 1
# DESIGN CHOICE, not a measurement: share of a source's per-run slots held for unresolved companies.
RESERVED_SHARE = 0.5

# Outcome vocabulary (stored by TASK-214).
RESOLVED = "resolved"  # rows that belong to the company came back, or a board token was learned
EMPTY = "empty"        # clean answer with zero rows: source does not cover the company (NOT "no jobs")
ERROR = "error"        # the call failed: unknown, never "empty"


def _parse(ts):
    """Aware-UTC datetime from an ISO string or datetime; None when missing or malformed."""
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).strip())
        except (ValueError, TypeError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _weight(people):
    """people as an int; None, '' or junk count as 0 so a bad count cannot raise."""
    try:
        return int(people)
    except (ValueError, TypeError):
        return 0


def _heaviest_first(items):
    return sorted(items, key=lambda c: (-_weight(c[2]), c[0]))


def next_eligible(outcome, now):
    """ISO date-time when a company may be re-resolved for a source.

    empty -> now + 45 days, error -> now + 1 day, resolved -> "" (the existing planners
    scan it; it is never re-resolved). An unknown outcome is treated as "error".
    """
    now = _parse(now)
    if outcome == RESOLVED:
        return ""
    days = EMPTY_COOLDOWN_DAYS if outcome == EMPTY else ERROR_COOLDOWN_DAYS
    return (now + timedelta(days=days)).isoformat()


def _in_cooldown(row, now):
    """True when `row` (outcome, attempted_at, next_eligible_at) blocks a new attempt."""
    outcome, _attempted, nxt = row
    due = _parse(nxt) if nxt else None
    if due is None:
        # A lost date must not turn a 45-day miss into a re-bill; an error may retry.
        return outcome == EMPTY
    return due > now


def plan_resolution(candidates, resolution, source, covered_keys, per_run, now):
    """Up to `per_run` (key, display, people) to resolve for `source`, heaviest first.

    Excluded: covered keys, rows already "resolved" for this source, rows still in cooldown.
    Order: people descending, then never-attempted before an expired cooldown, then key.
    """
    if per_run <= 0:
        return []
    now = _parse(now)
    covered = set(covered_keys)
    ranked = []
    for key, display, people in candidates:
        if key in covered:
            continue
        row = resolution.get((key, source))
        if row is not None:
            if row[0] == RESOLVED or _in_cooldown(row, now):
                continue
        ranked.append((-_weight(people), 0 if row is None else 1, key, display, people))
    ranked.sort(key=lambda r: r[:3])
    return [(k, d, p) for _w, _a, k, d, p in ranked[:per_run]]


def reserve_order(scan, resolve, per_run):
    """At most `per_run` (key, display, people), holding slots for unresolved companies.

    Both lists are sorted here by (-people, key); caller order is not trusted. Guarantees ceil(per_run * RESERVED_SHARE) slots to `resolve` (when it has that many),
    fills the rest from `scan` heaviest first, then back-fills any
    unused slot from whichever list still has companies. Pass the result to the existing
    planners: the list is already trimmed to `per_run`, so their heaviest-first sort cannot
    change who gets in (a later budget cut inside them still drops the lightest).
    """
    if per_run <= 0:
        return []
    scan, resolve = _heaviest_first(scan), _heaviest_first(resolve)
    reserved = min(len(resolve), math.ceil(per_run * RESERVED_SHARE))
    picked, seen = [], set()

    def take(items, limit):
        for item in items:
            if len(picked) >= limit:
                return
            if item[0] not in seen:
                seen.add(item[0])
                picked.append(item)

    take(resolve, reserved)
    take(scan, per_run)
    take(resolve, per_run)
    return picked
