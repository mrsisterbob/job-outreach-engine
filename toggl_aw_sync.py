"""Pure planning for the Toggl Focus -> ActivityWatch stopwatch sync (TASK-307).

No I/O, no clock: `now` always comes in. This module only builds events and plans
inserts and deletes; a caller does the HTTP.

Fetching: the caller must ask Toggl Focus for time entries with include_taskless=true.
The default query returns {"data": []} for an account whose entries have no task, and
`time_entries_taskless.data` is the list that holds EVERY entry, the running one too
(no `duration` key). So the sync never needs a tracking/current call.

Whether ActivityWatch replaces an event when one is POSTed with an existing `id` is
UNVERIFIED, so this module never relies on it and never sends an `id`: a changed entry
is an insert of the new event plus a delete of the old ids.

Every event we write carries data.source == SOURCE; the plan only ever touches events
that carry it and a string toggl_id.

Idempotence holds for finished entries; a running entry re-plans each run because its
duration grows.
"""
import math
import re
from datetime import timezone

# Private by name in toggl_snapshot.py (not in this task's scope); used as they are.
from toggl_snapshot import TogglReplyError, _parse_ts, _unwrap  # noqa: F401

SOURCE = "toggl-focus"
NO_PROJECT = "No project"
LABEL_MAX = 60
DESC_MAX = 200


def _clean(s, limit):
    return re.sub(r"\s+", " ", str(s)).strip()[:limit].strip()


def _iso_z(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _ms(dt):
    return int(dt.timestamp() * 1000) if dt else None


def _finished_ok(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0)


def build_events(entries_reply, projects_reply, window_start, window_end, now):
    """Return (events, report). Both replies are the wrapped {"data": [...]} objects."""
    for nm, v in (("window_start", window_start), ("window_end", window_end), ("now", now)):
        if getattr(v, "tzinfo", None) is None:
            raise ValueError(f"{nm} must be a timezone-aware datetime")
    entries = _unwrap(entries_reply, "time entries")
    projects = _unwrap(projects_reply, "projects")
    names = {}
    for p in projects:
        if isinstance(p, dict) and isinstance(p.get("id"), (int, str)) \
                and not isinstance(p.get("id"), bool):
            names[str(p["id"])] = _clean(p.get("name") or "", LABEL_MAX)
    rep = {"entries": 0, "events": 0, "running": 0, "no_project": 0,
           "unknown_project": 0, "unreadable": 0, "outside_window": 0,
           "unreadable_ids": []}
    events = []
    for e in entries:
        rep["entries"] += 1
        if not isinstance(e, dict):
            rep["unreadable"] += 1
            continue
        start = _parse_ts(e.get("start"))
        eid = e.get("id")
        id_ok = not isinstance(eid, bool) and isinstance(eid, (int, str))
        if start is None or not id_ok:
            rep["unreadable"] += 1
            if id_ok:
                rep["unreadable_ids"].append(str(eid))
            continue
        if not (window_start <= start < window_end):
            rep["outside_window"] += 1
            continue
        d = e.get("duration")
        running = d is None or (isinstance(d, (int, float)) and not isinstance(d, bool)
                                and math.isfinite(d) and d < 0)
        if running:
            dur = float(round(max(0.0, (now - start).total_seconds())))
        elif _finished_ok(d):
            dur = float(d)
        else:
            rep["unreadable"] += 1
            rep["unreadable_ids"].append(str(eid))
            continue
        pid = e.get("project_id")
        label = NO_PROJECT
        if pid is None:
            rep["no_project"] += 1
        elif str(pid) in names and names[str(pid)]:
            label = names[str(pid)]
        else:
            rep["unknown_project"] += 1
        desc = e.get("description")
        typ = e.get("type")
        events.append({
            "timestamp": _iso_z(start),
            "duration": dur,
            "data": {
                "label": label,
                "running": running,
                "source": SOURCE,
                "toggl_id": str(eid),
                "description": _clean(desc, DESC_MAX) if isinstance(desc, str) else "",
                "type": typ if isinstance(typ, str) else "",
            },
        })
        if running:
            rep["running"] += 1
    events.sort(key=lambda ev: (ev["timestamp"], ev["data"]["toggl_id"]))
    rep["events"] = len(events)
    return events, rep


def _ours(ev):
    if not isinstance(ev, dict):
        return None
    d = ev.get("data")
    if isinstance(d, dict) and d.get("source") == SOURCE and isinstance(d.get("toggl_id"), str):
        return d["toggl_id"]
    return None


def _equal(w, x):
    wd, xd = w["data"], x["data"]
    xdur = x.get("duration")
    if not isinstance(xdur, (int, float)) or isinstance(xdur, bool) or not math.isfinite(xdur):
        return False
    xt = _parse_ts(x.get("timestamp"))
    if xt is None or _ms(xt) != _ms(_parse_ts(w["timestamp"])):
        return False
    if abs(xdur - w["duration"]) > 0.5:
        return False
    return all(wd[k] == xd.get(k) for k in ("label", "running", "description", "type"))


def plan_sync(wanted, existing, window_start, protected=()):
    """Compare wanted events with the bucket's events; return the inserts and deletes.

    `protected` is report["unreadable_ids"]: toggl_ids whose row came back unreadable are
    never deleted as orphans. A repeated toggl_id in `wanted` keeps the first."""
    seen, uniq = set(), []
    for w in wanted:
        if w["data"]["toggl_id"] not in seen:
            seen.add(w["data"]["toggl_id"])
            uniq.append(w)
    wanted = uniq
    protected = {str(x) for x in protected}
    groups = {}
    for ev in existing:
        tid = _ours(ev)
        if tid is not None:
            groups.setdefault(tid, []).append(ev)
    wanted_ids = {w["data"]["toggl_id"] for w in wanted}
    insert, delete, keep, replace = [], set(), 0, 0

    def mark(evs):
        for ev in evs:
            if ev.get("id") is not None:
                delete.add(ev["id"])

    for w in wanted:
        grp = sorted(groups.get(w["data"]["toggl_id"], []), key=lambda ev: str(ev.get("id")))
        equal = [ev for ev in grp if _equal(w, ev)]
        if equal:
            keep += 1
            mark([ev for ev in grp if ev is not equal[0]])
        else:
            insert.append(w)
            if grp:
                replace += 1
                mark(grp)
    for tid, grp in groups.items():
        if tid in wanted_ids or tid in protected:
            continue
        for ev in grp:
            t = _parse_ts(ev.get("timestamp"))
            if t is not None and t >= window_start:
                mark([ev])
    insert.sort(key=lambda ev: (ev["timestamp"], ev["data"]["toggl_id"]))
    return {"insert": insert, "delete": sorted(delete, key=str), "keep": keep, "replace": replace}
