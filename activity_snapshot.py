"""One week of ActivityWatch "app, active seconds" rows -> the `time` snapshot section.

Pure logic: no I/O, no env, no network, no clock (`now` is passed in as an aware datetime).

The section is {as_of, week_of, planned, actual}. `actual` is hours per CATEGORY chosen from the
app name, counted only while Kevin was at the keyboard (the server-side query intersects the
window watcher with the not-afk status; the raw window bucket keeps recording while he is away
and over-counted a measured week by 5 hours). Nothing in the section carries an app name or a
window title: the query result is read for `data.app` and `duration` only, every other key
(including `title`) is dropped at the boundary, and apps are turned into bucket names before
anything leaves this module.

Why `planned` lists EVERY bucket with hours 0.0: finance_model.editable_categories is the union
of the saved plan's categories and the snapshot's planned/actual categories, and the plan editor
can only edit categories it is shown. If the PC pushed only computer categories the editor could
never contain Sleep and a 168-hour plan could not be made. The zeros invent no numbers about
Kevin's life; the page shows an unplanned 168 until he saves a plan, and a saved plan wins over
the snapshot's `planned`.

The PC being off or asleep produces no events, so Sleep, Exercise, Meals & home have no actual
rows here: they are absent, not 0.
"""
import math
import re
from datetime import datetime, timedelta, timezone

DEFAULT_BUCKETS = ["Sleep", "Job search", "Building", "Browsing", "Gaming",
                   "Meals & home", "Exercise", "Social", "Other"]
UNMAPPED_BUCKET = "Other"

# Browsers cannot be split into "Job search" vs "Browsing" from the app name alone, and window
# titles are private. That split is Kevin's decision (a later task could add title keyword rules
# he writes himself); until then every browser is "Browsing".
DEFAULT_APP_BUCKETS = {
    "code.exe": "Building", "claude.exe": "Building", "windowsterminal.exe": "Building",
    "powershell.exe": "Building", "cmd.exe": "Building", "actual.exe": "Building",
    "msedge.exe": "Browsing", "chrome.exe": "Browsing", "firefox.exe": "Browsing",
    "robloxplayerbeta.exe": "Gaming",
    "telegram.exe": "Social",
}

# Lock screen and shell chrome count as "active" to ActivityWatch; they are dropped from totals.
IGNORED_APPS = frozenset({"lockapp.exe", "shellexperiencehost.exe", "searchhost.exe",
                          "startmenuexperiencehost.exe", "textinputhost.exe"})

AW_QUERY = [
    'afk = flood(query_bucket(find_bucket("aw-watcher-afk_")));',
    'win = flood(query_bucket(find_bucket("aw-watcher-window_")));',
    'win = filter_period_intersect(win, filter_keyvals(afk, "status", ["not-afk"]));',
    'ev = merge_events_by_keys(win, ["app"]);',
    'RETURN = sort_by_duration(ev);',
]

_TZ_NAME = "America/Detroit"
MAX_BUCKETS = 30
MAX_BUCKET_LEN = 40
_MAX_SECONDS = 1e12  # clamp so a sum can never reach inf


def _zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(_TZ_NAME), _TZ_NAME
    except Exception:
        return timezone.utc, "UTC"


def _z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def week_bounds(now) -> dict:
    """Monday-to-Monday week containing `now`, in Detroit local time, as UTC instants.

    Each Monday 00:00 is built as a local wall-clock time and converted separately, so a DST week
    is 167 or 169 hours long.
    """
    tz, tz_name = _zone()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(tz)
    monday = local.date() - timedelta(days=local.weekday())
    nxt = monday + timedelta(days=7)
    start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return {"week_of": monday.isoformat(), "start": _z(start), "end": _z(end), "tz": tz_name}


def build_query_body(start, end) -> dict:
    return {"timeperiods": [f"{start}/{end}"], "query": list(AW_QUERY)}


def _is_num(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0)


def app_seconds_from_query(result):
    """/query/ response -> ({lower-cased app: seconds}, unreadable count). Never raises.

    Reads only `data.app` and `duration`; `title` and every other key are never copied.
    """
    if not isinstance(result, list):
        return {}, 1
    # the outer list holds one inner list per RETURN; sum every one rather than ignore extras
    events = []
    for item in result:
        if isinstance(item, list):
            events.extend(item)
        else:
            events.append(item)
    out, bad = {}, 0
    for ev in events:
        data = ev.get("data") if isinstance(ev, dict) else None
        app = data.get("app") if isinstance(data, dict) else None
        dur = ev.get("duration") if isinstance(ev, dict) else None
        if not isinstance(app, str) or not app.strip() or not _is_num(dur):
            bad += 1
            continue
        key = app.strip().lower()
        out[key] = min(out.get(key, 0.0) + float(dur), _MAX_SECONDS)
    return out, bad


def _collapse(s):
    return re.sub(r"\s+", " ", s).strip()


def parse_category_config(obj):
    """{"buckets": [...], "apps": {...}} -> (buckets, app_buckets). Raises ValueError listing
    every problem. Either key is optional; "Other" is appended to the buckets if absent."""
    if obj is None:
        obj = {}
    problems = []
    if not isinstance(obj, dict):
        raise ValueError("config must be a dict")
    buckets = list(DEFAULT_BUCKETS)
    if "buckets" in obj:
        raw = obj["buckets"]
        if not isinstance(raw, list):
            problems.append("buckets must be a list")
        else:
            names, seen = [], set()
            if not 1 <= len(raw) <= MAX_BUCKETS:
                problems.append(f"buckets must hold 1..{MAX_BUCKETS} entries, got {len(raw)}")
            for i, b in enumerate(raw):
                if not isinstance(b, str):
                    problems.append(f"buckets[{i}] is not a string")
                    continue
                name = _collapse(b)
                if not name:
                    problems.append(f"buckets[{i}] is blank")
                elif len(name) > MAX_BUCKET_LEN:
                    problems.append(f"buckets[{i}] is longer than {MAX_BUCKET_LEN} characters")
                elif name.lower() in seen:
                    problems.append(f"buckets[{i}] duplicates an earlier bucket")
                else:
                    seen.add(name.lower())
                    names.append(name)
            buckets = names
    if not any(b.lower() == UNMAPPED_BUCKET.lower() for b in buckets):
        buckets.append(UNMAPPED_BUCKET)
    canon = {b.lower(): b for b in buckets}
    apps = {k: canon[v.lower()] for k, v in DEFAULT_APP_BUCKETS.items() if v.lower() in canon}
    if "apps" in obj:
        raw = obj["apps"]
        if not isinstance(raw, dict):
            problems.append("apps must be a dict")
        else:
            # messages carry only the entry's position, never an app name
            for i, (k, v) in enumerate(raw.items()):
                if not isinstance(k, str) or not k.strip():
                    problems.append(f"apps entry #{i} key is not a non-blank string")
                    continue
                if not isinstance(v, str) or _collapse(v).lower() not in canon:
                    problems.append(f"apps entry #{i} maps to a bucket that is not in buckets")
                    continue
                apps[k.strip().lower()] = canon[_collapse(v).lower()]
    if problems:
        raise ValueError("; ".join(problems))
    return buckets, apps


def _canon_buckets(buckets):
    """Strip/collapse, drop non-str and blanks, dedupe case-insensitively, 'Other' last-resort."""
    out, seen = [], set()
    for b in (buckets if buckets is not None else DEFAULT_BUCKETS):
        if not isinstance(b, str):
            continue
        name = _collapse(b)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(UNMAPPED_BUCKET if name.lower() == UNMAPPED_BUCKET.lower() else name)
    if UNMAPPED_BUCKET not in out:
        out.append(UNMAPPED_BUCKET)
    return out


def _tally(app_seconds, buckets, app_buckets, ignored):
    """-> (bucket seconds, ignored seconds, {unmapped app: seconds}, unreadable count)

    Non-finite, negative or non-numeric seconds are skipped and counted. Sums are clamped.
    """
    canon = {b.lower(): b for b in buckets}
    totals = {b: 0.0 for b in buckets}
    ign, unmapped, bad = 0.0, {}, 0
    for app, secs in app_seconds.items():
        if not _is_num(secs):
            bad += 1
            continue
        a = str(app).strip().lower()
        if a in ignored:
            ign = min(ign + secs, _MAX_SECONDS)
            continue
        v = app_buckets.get(a)
        b = canon.get(_collapse(v).lower()) if isinstance(v, str) else None
        if b is None:
            b = UNMAPPED_BUCKET
            unmapped[a] = min(unmapped.get(a, 0.0) + secs, _MAX_SECONDS)
        totals[b] = min(totals[b] + secs, _MAX_SECONDS)
    return totals, ign, unmapped, bad


def _resolve(buckets, app_buckets, ignored):
    buckets = _canon_buckets(buckets)
    amap = DEFAULT_APP_BUCKETS if app_buckets is None else app_buckets
    amap = {str(k).strip().lower(): v for k, v in amap.items()}
    ign = IGNORED_APPS if ignored is None else {str(a).strip().lower() for a in ignored}
    return buckets, amap, ign


def build_time_section(app_seconds, now, buckets=None, app_buckets=None, ignored=None) -> dict:
    buckets, amap, ign = _resolve(buckets, app_buckets, ignored)
    totals, _, _, _ = _tally(app_seconds or {}, buckets, amap, ign)
    tz, _name = _zone()
    aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    actual = []
    for b in buckets:
        h = round(totals[b] / 3600, 2)
        if h > 0:
            actual.append({"category": b, "hours": h})
    return {"as_of": aware.astimezone(tz).date().isoformat(),
            "week_of": week_bounds(now)["week_of"],
            "planned": [{"category": b, "hours": 0.0} for b in buckets],
            "actual": actual}


def describe_week(app_seconds, section, unreadable=0, buckets=None, app_buckets=None,
                  ignored=None) -> dict:
    """Dry-run summary for the PC script. `apps_unmapped` names apps and is shown ONLY on the
    PC, never pushed. The section carries only four keys (no totals), so tracked/ignored hours
    are summed from the unrounded seconds, which makes them the true totals."""
    buckets, amap, ign = _resolve(buckets, app_buckets, ignored)
    totals, ign_secs, unmapped, bad = _tally(app_seconds or {}, buckets, amap, ign)
    return {"tracked_hours": round(sum(totals.values()) / 3600, 2),
            "ignored_hours": round(ign_secs / 3600, 2),
            "apps_unmapped": [{"app": a, "hours": round(unmapped[a] / 3600, 2)}
                              for a in sorted(unmapped)],
            "unreadable": int(unreadable) + bad,
            "tz": _zone()[1]}
