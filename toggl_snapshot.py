"""Turn one week of Toggl FOCUS time entries into a `toggl` snapshot section.

Pure logic: no network, no clock, no environment. The caller passes the two replies as Focus
sends them (wrapped, `{"data": [...]}`), the Detroit week window, and `now`.

IMPORTANT: THE FETCHER MUST ALWAYS ASK FOR ENTRIES WITH `include_taskless=true`. WITHOUT IT
FOCUS RETURNS `{"data": []}` FOR AN ACCOUNT WHOSE ENTRIES HAVE NO TASK, WHICH LOOKS EXACTLY LIKE
AN EMPTY WEEK. This module cannot tell which query produced the list; a well-formed empty
`data` list builds `actual == []`, and only a non-conforming shape raises.

Why the section is called `toggl`, not `time`: finance_model.build_finance_view takes the `time`
section from the single newest snapshot that carries one. The ActivityWatch push already writes
`time`; a Toggl push under that name would replace the PC hours and the next PC push would replace
Toggl. Toggl also covers what ActivityWatch cannot see (dog, driving, time away from the PC), so
both must sit side by side. This module builds only the section; personal_snapshots and
finance_model do not know `toggl` yet.

Contract (JSON-safe):
    {"as_of": "YYYY-MM-DD",      # Detroit local date of `now`
     "week_of": "YYYY-MM-DD",    # Monday of the window
     "actual": [{"category": "Dog", "hours": 3.5}, ...]}

Category names are values in a list, never dict keys.
"""
import math
from collections.abc import Mapping
import re
from datetime import datetime, timedelta, timezone

from activity_snapshot import _zone, week_bounds

SECTION = "toggl"
NO_PROJECT_CATEGORY = "Other"
MAX_CATEGORY_LEN = 40

TOKEN_REJECTED_MSG = (
    "Toggl rejected the API key (HTTP {status}). Check that TOGGL_API_TOKEN still holds a "
    "current key from your Toggl Focus profile (it may have been refreshed, revoked, expired "
    "or pasted wrong). No snapshot was built."
)


class TogglReplyError(Exception):
    """Toggl answered with something that is not a usable reply."""


class TogglTokenRejected(TogglReplyError):
    """Toggl rejected the API key."""


def _header(headers, name):
    if not isinstance(headers, Mapping):
        return None
    for k in list(headers.keys()):
        if isinstance(k, str) and k.lower() == name.lower():
            return headers[k]
    return None


def check_reply(status, body_text, headers=None) -> None:
    """Return None only for HTTP 200; raise for everything else (204 included).

    The 401/403 rejection status is the documented guess: how Focus refuses a bad key is
    unverified (the fixture holds only 200 replies).
    """
    if status == 200:
        return None
    if status in (401, 403):
        raise TogglTokenRejected(TOKEN_REJECTED_MSG.format(status=status))
    if status == 402:
        resets = _header(headers, "X-Toggl-Quota-Resets-In")
        extra = f" Quota resets in {resets} seconds." if resets is not None else ""
        raise TogglReplyError(
            "Toggl says the hourly request quota is used up (HTTP 402)." + extra
            + " No snapshot was built.")
    if status == 429:
        retry = _header(headers, "Retry-After")
        extra = f" Retry after {retry}." if retry is not None else ""
        raise TogglReplyError("Toggl is rate limiting this key (HTTP 429)." + extra
                              + " No snapshot was built.")
    raise TogglReplyError(f"Toggl answered HTTP {status}, not 200. No snapshot was built.")


def _unwrap(reply, what):
    if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
        raise TogglReplyError(
            f"The Toggl {what} reply is not a {{\"data\": [...]}} object, so it was not read "
            "as an empty list. No snapshot was built.")
    return reply["data"]


_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def _parse_ts(s):
    if not isinstance(s, str):
        return None
    m = _TS.match(s.strip())
    if not m:
        return None
    day, hms, frac, tz = m.groups()
    frac = ((frac or "0") + "000000")[:6]
    try:
        dt = datetime.fromisoformat(f"{day}T{hms}.{frac}")
    except ValueError:
        return None
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        dt = dt - sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return dt.replace(tzinfo=timezone.utc)


def _good_duration(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0)


def _hashable(v):
    try:
        hash(v)
    except TypeError:
        return False
    return True


def _clean_name(name):
    return re.sub(r"\s+", " ", str(name)).strip()


def _tally(entries_reply, projects_reply, window_start, window_end, now, category_map):
    entries = _unwrap(entries_reply, "time entries")
    projects = _unwrap(projects_reply, "projects")
    names = {}
    for p in projects:
        if isinstance(p, dict) and isinstance(p.get("name"), str) and _hashable(p.get("id")):
            names[p["id"]] = p["name"]
    cmap = {_clean_name(k).lower(): v for k, v in (category_map or {}).items()
            if isinstance(k, str)}
    secs = {}
    stats = {"no_project_entries": 0, "unknown_project_entries": 0, "running_included": False,
             "unreadable": 0, "categories_cut": 0}
    spans = []
    seen_ids = set()
    for e in entries:
        if not isinstance(e, dict):
            stats["unreadable"] += 1
            continue
        eid = e.get("id")
        if eid is not None and _hashable(eid):
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
        start = _parse_ts(e.get("start"))
        if start is None:
            stats["unreadable"] += 1
            continue
        d = e.get("duration")
        running = (d is None or (isinstance(d, (int, float)) and not isinstance(d, bool)
                                 and math.isfinite(d) and d < 0))
        if running:
            end = max(now, start)
        else:
            if not _good_duration(d):
                stats["unreadable"] += 1
                continue
            try:
                end = start + timedelta(seconds=d)
            except (OverflowError, ValueError):
                stats["unreadable"] += 1
                continue
        lo, hi = max(start, window_start), min(end, window_end)
        s = (hi - lo).total_seconds()
        if s <= 0:
            continue
        pid = e.get("project_id")
        if not _hashable(pid):
            stats["unreadable"] += 1
            continue
        if pid is None:
            cat = NO_PROJECT_CATEGORY
            stats["no_project_entries"] += 1
        elif pid in names and _clean_name(names[pid]):
            cat = _clean_name(names[pid])
            if len(cat) > MAX_CATEGORY_LEN:
                cat = cat[:MAX_CATEGORY_LEN].rstrip()
                stats["categories_cut"] += 1
        else:
            cat = NO_PROJECT_CATEGORY
            stats["unknown_project_entries"] += 1
        mapped = cmap.get(cat.lower())
        if isinstance(mapped, str) and _clean_name(mapped):
            cat = _clean_name(mapped)
            if len(cat) > MAX_CATEGORY_LEN:
                cat = cat[:MAX_CATEGORY_LEN].rstrip()
                stats["categories_cut"] += 1
        secs[cat] = secs.get(cat, 0.0) + s
        if running:
            stats["running_included"] = True
        spans.append((lo, hi))
    spans.sort()
    overlap = 0.0
    cur_end = None
    for lo, hi in spans:
        if cur_end is not None and lo < cur_end:
            overlap += (min(hi, cur_end) - lo).total_seconds()
        cur_end = hi if cur_end is None else max(cur_end, hi)
    return secs, stats, overlap


def _local_date(now):
    tz, _name = _zone()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(tz).date().isoformat()


def build_toggl_section(entries_reply, projects_reply, window_start, window_end, now,
                        category_map=None) -> dict:
    """Build the `toggl` section. Raises TogglReplyError on a reply that is not wrapped data."""
    secs, _stats, _ov = _tally(entries_reply, projects_reply, window_start, window_end, now,
                               category_map)
    rows = [{"category": c, "hours": round(s / 3600, 2)} for c, s in secs.items()]
    rows = [r for r in rows if r["hours"] > 0]
    rows.sort(key=lambda r: (-r["hours"], r["category"]))
    return {"as_of": _local_date(now), "week_of": week_bounds(window_start)["week_of"],
            "actual": rows}


def describe_week(entries_reply, projects_reply, window_start, window_end, now,
                  category_map=None) -> dict:
    secs, stats, overlap = _tally(entries_reply, projects_reply, window_start, window_end,
                                  now, category_map)
    return {
        "tracked_hours": round(sum(secs.values()) / 3600, 2),
        "other_hours": round(secs.get(NO_PROJECT_CATEGORY, 0.0) / 3600, 2),
        "no_project_entries": stats["no_project_entries"],
        "unknown_project_entries": stats["unknown_project_entries"],
        "running_included": stats["running_included"],
        "overlap_hours": round(overlap / 3600, 2),
        "unreadable": stats["unreadable"],
        "categories_cut": stats["categories_cut"],
    }
