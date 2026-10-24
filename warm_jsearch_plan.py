"""Pure planner: which no-board warm companies get a JSearch lookup this /w.

No I/O, no network, no clock reads: `now` is passed in. The caller supplies
candidates, the spend ledger and the current time; this returns the picks and
the requests left.

The JSearch plan quota is UNKNOWN (nothing in the repo records it). The
weekly cap of 10 below is a conservative guess for Kevin to raise, not a
measured limit.

Boundaries: a ledger row exactly seven days old still COUNTS against the cap
(window is inclusive); a company looked up exactly WARM_JSEARCH_RETRY_DAYS ago
is ELIGIBLE again (the gap is exclusive of its last day).
A malformed or future timestamp is treated as spent right now.
"""
from datetime import datetime, timedelta, timezone

# Requests (pages) per rolling trailing seven days, not calendar weeks, so
# there is no Monday reset edge. A guess: the plan quota is unknown.
WARM_JSEARCH_WEEKLY_REQUESTS = 10
# One page per company keeps the cap meaning "ten companies a week".
WARM_JSEARCH_PAGES_PER_COMPANY = 1
# A company with no board rarely gains a posting fast; don't re-spend on it
# for four weeks.
WARM_JSEARCH_RETRY_DAYS = 28

_WINDOW = timedelta(days=7)


def _parse(ts, now):
    """Parse an ISO timestamp to aware UTC; None on any failure."""
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def plan_warm_jsearch(candidates, ledger, now,
                      weekly_requests=WARM_JSEARCH_WEEKLY_REQUESTS,
                      pages_per_company=WARM_JSEARCH_PAGES_PER_COMPANY,
                      retry_days=WARM_JSEARCH_RETRY_DAYS):
    """Return (picks, requests_left).

    candidates: iterable of (company_key, display_name, contact_count)
    ledger: iterable of (company_key, iso_timestamp, requests_spent)
    picks: list of (company_key, display_name), biggest company first.
    requests_left: budget remaining after the picks are spent.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    retry_gap = timedelta(days=retry_days)
    spent = 0
    last_lookup = {}
    for key, ts, req in ledger:
        dt = _parse(ts, now)
        if dt is None or dt > now:
            dt = now
        age = now - dt
        try:
            req = int(req)
        except (ValueError, TypeError):
            req = weekly_requests  # unparseable: assume the worst, never grant budget
        req = max(0, req)
        if age <= _WINDOW:
            spent += req
        prev = last_lookup.get(key)
        if prev is None or dt > prev:
            last_lookup[key] = dt
    left = max(0, weekly_requests - spent)
    if pages_per_company <= 0:
        return [], left

    best = {}
    for c in candidates:
        if c[0] not in best or c[2] > best[c[0]][2]:
            best[c[0]] = c
    ordered = sorted(best.values(), key=lambda c: (-c[2], c[0]))
    picks = []
    for key, name, count in ordered:
        if left < pages_per_company:
            break
        if count <= 0:
            continue
        last = last_lookup.get(key)
        if last is not None and now - last < retry_gap:
            continue
        picks.append((key, name))
        left -= pages_per_company
    return picks, left
