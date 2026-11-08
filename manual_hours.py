"""Hand-logged time ("side work 2h") kept in its own small file on the persistent disk.

These are entries Kevin types, beside the PC-pushed ActivityWatch totals - not a snapshot, so
they do NOT live in the snapshot store (which prunes). Directory: env PERSONAL_LOG_DIR, read at
CALL time (default: a personal_log folder next to this module, which is ephemeral on Render).
File manual_hours.json = {"next_id": int, "entries": [...]}.

Decisions:
- Durations are held in integer quarter-hours, so sums never drift (0.1 + 0.2 style).
- "2h 30m" (hours then minutes, adjacent) and "1h30m" are ADDED. Two hour values, two minute
  values, or a duration written apart from another are rejected. A bare "2" has no unit: invalid.
- Ids only ever increase: after remove_last the next entry does NOT reuse the removed id.
- week_start is the Monday of the week containing logged_at in America/Detroit (UTC if the zone
  database is missing). The Monday math is the one-liner equivalent of pipeline_utils.week_start;
  importing pipeline_utils costs ~260 ms and pulls in `requests`, so it is not imported here.
- Only the newest 500 entries are kept. A corrupt file reads as empty (class name logged).
Single-process assumption: render.yaml runs gunicorn with --workers 1, so the module-level
threading.Lock is the only lock; there is no cross-process file lock.
Pure stdlib.
"""
import json
import logging
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone

_FILE = "manual_hours.json"
_LOCK = threading.Lock()
_MAX_ENTRIES = 500
_WEEK_CAP_QUARTERS = 168 * 4
_log = logging.getLogger(__name__)
_zone_warned = False

_DURATION_RE = re.compile(
    r"(?<![\w.,\-])(?:"
    r"(?P<h>\d{1,6}(?:\.\d{1,6})?)\s*(?:hours?|hrs?|h)(?![a-z])"
    r"(?:\s*(?P<m>\d{1,6})\s*(?:minutes?|mins?|m)(?![a-z]))?"
    r"|(?P<m2>\d{1,6}(?:\.\d{1,6})?)\s*(?:minutes?|mins?|m)(?![a-z])"
    r")",
    re.IGNORECASE,
)


class LogInvalid(ValueError):
    """The text cannot be logged; the message is user-showable."""


def _log_dir() -> str:
    d = os.environ.get("PERSONAL_LOG_DIR")
    if d and d.strip():
        return os.path.abspath(d.strip())
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "personal_log")


def _zone():
    global _zone_warned
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Detroit")
    except Exception as e:  # missing tz database
        if not _zone_warned:
            _zone_warned = True
            _log.warning("manual_hours: America/Detroit unavailable (%s); using UTC", type(e).__name__)
        return timezone.utc


def _aware(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def current_week_start(now) -> str:
    """The Detroit Monday (YYYY-MM-DD) of the week containing `now`."""
    day = _aware(now).astimezone(_zone()).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def parse_log_text(text):
    """Parse `<category> <duration>` or `<duration> <category>` -> (category, hours, rounded)."""
    if not isinstance(text, str):
        raise LogInvalid("Say what and how long, like: side work 2h")
    s = text.strip()
    if re.search(r"\d,\d", s):
        raise LogInvalid("Use a dot for decimals, like: gym 1.5h")
    if re.search(r"(?<![\w-])-\.?\d", s):
        raise LogInvalid("Time cannot be negative. Say how long, like: side work 2h")
    matches = list(_DURATION_RE.finditer(s))
    if len(matches) != 1:
        raise LogInvalid("Say how long, like: side work 2h" if not matches
                         else "Give one duration only, like: side work 2h")
    m = matches[0]
    minutes = 0.0
    if m.group("h") is not None:
        minutes += float(m.group("h")) * 60
        if m.group("m") is not None:
            minutes += float(m.group("m"))
    else:
        minutes += float(m.group("m2"))
    quarters = int(minutes / 15 + 0.5)
    if quarters < 1:
        raise LogInvalid("Under 15 minutes is not logged.")
    if quarters > 96:
        raise LogInvalid("One entry can be at most 24 hours.")
    rounded = abs(quarters * 15 - minutes) > 1e-9
    rest = s[:m.start()] + " " + s[m.end():]
    cat = " ".join(rest.split())
    # strip separators/punctuation and the connectors "for"/"on" at either end (repeat until stable)
    while True:
        new = cat.strip(" ,.;:-–—")
        new = re.sub(r"^(?:for|on)(?:\s+|$)", "", new, flags=re.IGNORECASE)
        new = re.sub(r"(?:^|\s+)(?:for|on)$", "", new, flags=re.IGNORECASE)
        if new == cat:
            break
        cat = new
    if any(re.fullmatch(r"\d+(?:\.\d+)?", t) for t in cat.split()):
        raise LogInvalid("A number needs a unit (h or m), like: side work 2h")
    if not cat:
        raise LogInvalid("Say what it was for, like: side work 2h")
    if "<" in cat or ">" in cat:
        raise LogInvalid("The category cannot contain < or >.")
    if len(cat) > 60:
        raise LogInvalid("The category is too long (60 characters at most).")
    if not any(c.isalpha() for c in cat):
        raise LogInvalid("The category needs a word, like: side work 2h")
    return cat, quarters / 4, rounded


def canonical_category(name, known_categories) -> str:
    """The spelling from known_categories matching `name` case-insensitively, else `name`."""
    key = " ".join(str(name).split()).casefold()
    for k in known_categories or ():
        if isinstance(k, str) and " ".join(k.split()).casefold() == key:
            return k
    return name


def _entry_ok(e) -> bool:
    if not isinstance(e, dict):
        return False
    i, h = e.get("id"), e.get("hours")
    if not isinstance(i, int) or isinstance(i, bool):
        return False
    return isinstance(h, (int, float)) and not isinstance(h, bool) and math.isfinite(h)


def _read():
    path = os.path.join(_log_dir(), _FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"next_id": 1, "entries": []}
    except Exception as e:
        _log.warning("manual_hours: %s unreadable (%s); treating as empty", _FILE, type(e).__name__)
        return {"next_id": 1, "entries": []}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        _log.warning("manual_hours: %s has an unexpected shape; treating as empty", _FILE)
        return {"next_id": 1, "entries": []}
    entries = [e for e in data["entries"] if _entry_ok(e)]
    if len(entries) != len(data["entries"]):
        _log.warning("manual_hours: dropped %d malformed entries from %s",
                     len(data["entries"]) - len(entries), _FILE)
    nid = data.get("next_id")
    if not isinstance(nid, int) or isinstance(nid, bool):
        nid = 1
    nid = max(nid, max((e["id"] for e in entries), default=0) + 1)
    return {"next_id": nid, "entries": entries}


def _write(data):
    d = _log_dir()
    os.makedirs(d, exist_ok=True)
    data = {"next_id": data["next_id"], "entries": data["entries"][-_MAX_ENTRIES:]}
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".manual_hours.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(d, _FILE))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _q(hours) -> int:
    return int(round(hours * 4))


def log_entry(text, known_categories=(), now=None) -> dict:
    """Parse, canonicalize, append and return the stored entry (ids never reused)."""
    cat, hours, _rounded = parse_log_text(text)
    cat = canonical_category(cat, known_categories)
    when = _aware(now).astimezone(timezone.utc)
    week = current_week_start(when)
    with _LOCK:
        data = _read()
        used = sum(_q(e["hours"]) for e in data["entries"] if e.get("week_start") == week)
        if used + _q(hours) > _WEEK_CAP_QUARTERS:
            raise LogInvalid(f"That would put this week over 168 hours (already {used / 4:g}h logged by hand).")
        entry = {"id": data["next_id"], "logged_at": when.isoformat(timespec="seconds"),
                 "week_start": week, "category": cat, "hours": hours}
        data["entries"].append(entry)
        data["next_id"] += 1
        _write(data)
    return dict(entry)


def list_all() -> list:
    with _LOCK:
        return [dict(e) for e in _read()["entries"]]


def list_week(week_start: str) -> list:
    """Entries of the given Monday week, oldest first."""
    return [e for e in list_all() if e.get("week_start") == week_start]


def remove_last(week_start=None):
    """Remove and return the newest entry (of that week if given), or None."""
    with _LOCK:
        data = _read()
        pool = [e for e in data["entries"] if week_start is None or e.get("week_start") == week_start]
        if not pool:
            return None
        last = max(pool, key=lambda e: e["id"])
        data["entries"] = [e for e in data["entries"] if e is not last]
        _write(data)
        return dict(last)
