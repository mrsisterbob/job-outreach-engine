"""Phone presence events ("I joined / left / tapped <place key>") logged on the persistent disk.

Phone contract (the only description of it):
  POST /presence/event
  header  X-Presence-Token: <PRESENCE_TOKEN>
  body    JSON {"place": "library", "event": "join"}   (at most MAX_BODY_BYTES)
The phone sends a short PLACE KEY typed into the macro, never the wifi name (the SSID never
leaves the phone). The engine decides what a key means from env PRESENCE_PLACES, a JSON object
{"library": "Job search", "home": "Home"} (place key -> label). Allowlist: parse_places.

Three events:
  join   a state begins (a wifi joined, a charger plugged in).
  leave  that state ends. join/leave are a pair.
  tap    a single event with NO leave: a sticker touched means "I am doing this activity now";
         the place key names the activity ({"place": "desk", "event": "tap"}).

Rules: the same place and event within DEDUP_SECONDS of the last one is a "duplicate" (this also
absorbs a double NFC read); more than RATE_LIMIT_PER_HOUR events in the previous 60 minutes is
"rate_limited". Either way nothing is written. A leave with no earlier join is recorded as is;
a policy decides what it means later.

Storage: env PERSONAL_LOG_DIR (same rule as manual_hours: blank -> folder beside the module),
file presence_<YYYY-MM>.jsonl by UTC month, one JSON object per line, append-only, never
rewritten or trimmed (a new month starts a new file, so there is no cleanup and no race).
Each append is one write() of one line plus fsync. Corrupt lines are skipped on read; an
unwritable directory raises OSError from record. Time is stamped by the server (`now`, or the
system clock only when now is None). The request body and place text are never logged.
Pure stdlib plus manual_hours (stdlib only) for the directory rule.
"""
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone

import manual_hours

EVENTS = ("join", "leave", "tap")
MAX_BODY_BYTES = 1024
DEDUP_SECONDS = 90
RATE_LIMIT_PER_HOUR = 60
_MAX_PLACES = 30
_MAX_LABEL = 40
_KEY_RE = re.compile(r"[a-z0-9_-]{1,32}")
_LOCK = threading.Lock()


class PresenceInvalid(ValueError):
    """The input is not acceptable; the message is fixed and never echoes the input."""


def parse_places(text):
    """Env text -> {place key: label}. None/blank -> {}. Anything malformed raises."""
    if text is None:
        return {}
    if not isinstance(text, str):
        raise PresenceInvalid("bad places")
    if not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise PresenceInvalid("bad places") from None
    if not isinstance(obj, dict) or len(obj) > _MAX_PLACES:
        raise PresenceInvalid("bad places")
    out = {}
    for k, v in obj.items():
        if not isinstance(k, str) or not _KEY_RE.fullmatch(k):
            raise PresenceInvalid("bad places")
        if not isinstance(v, str):
            raise PresenceInvalid("bad places")
        label = " ".join(v.split())
        if not label or len(label) > _MAX_LABEL:
            raise PresenceInvalid("bad places")
        out[k] = label
    return out


def parse_event(payload, places):
    """Validate one call -> (place, event). Fixed messages, no echo of the input."""
    if not isinstance(payload, dict) or not isinstance(places, dict):
        raise PresenceInvalid("bad payload")
    place = payload.get("place")
    event = payload.get("event")
    if not isinstance(place, str) or place not in places:
        raise PresenceInvalid("unknown place")
    if not isinstance(event, str):
        raise PresenceInvalid("bad event")
    event = event.strip().lower()
    if event not in EVENTS:
        raise PresenceInvalid("bad event")
    return place, event


def _aware(now):
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _month_name(dt):
    return "presence_%04d-%02d.jsonl" % (dt.year, dt.month)


def _prev_month(dt):
    first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first - timedelta(days=1)


def _read_file(path):
    """Valid entries of one file in file order; unreadable file or corrupt lines are skipped."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if (isinstance(e, dict) and isinstance(e.get("place"), str)
                    and e.get("event") in EVENTS):
                t = datetime.fromisoformat(e["received_at"])
                e["_t"] = (t.replace(tzinfo=timezone.utc) if t.tzinfo is None
                           else t.astimezone(timezone.utc))
                out.append(e)
        except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
            continue
    return out


def _load(now):
    """Entries from the previous and current month files, oldest first (file order)."""
    d = manual_hours._log_dir()
    entries = []
    for dt in (_prev_month(now), now):
        entries.extend(_read_file(os.path.join(d, _month_name(dt))))
    return entries


def _public(e):
    return {k: e.get(k) for k in ("received_at", "place", "event")}


def record(place, event, now=None):
    """Log one event -> {"status": "ok"|"duplicate"|"rate_limited", ...}. OSError if unwritable."""
    if not isinstance(place, str) or not _KEY_RE.fullmatch(place):
        raise PresenceInvalid("unknown place")
    if not isinstance(event, str) or event not in EVENTS:
        raise PresenceInvalid("bad event")
    now = _aware(now)
    with _LOCK:
        # a line stamped in the future (clock skew, bad write) must not block real events
        entries = [e for e in _load(now) if e["_t"] <= now + timedelta(seconds=60)]
        last = None
        for e in entries:
            if e["place"] == place:
                last = e
        if last is not None and last["event"] == event:
            if (now - last["_t"]).total_seconds() < DEDUP_SECONDS:
                return {"status": "duplicate"}
        window = now - timedelta(hours=1)
        if sum(1 for e in entries if window < e["_t"] <= now) >= RATE_LIMIT_PER_HOUR:
            return {"status": "rate_limited"}
        entry = {"received_at": now.isoformat(), "place": place, "event": event}
        d = manual_hours._log_dir()
        os.makedirs(d, exist_ok=True)
        data = (json.dumps(entry) + "\n").encode("utf-8")
        path = os.path.join(d, _month_name(now))
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        data = b"\n" + data  # torn tail: do not glue onto a partial line
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            view = memoryview(data)
            while len(view):
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("short write")
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"status": "ok", "entry": entry}


def recent(limit=50, now=None):
    """Newest-first entries across the current and previous month; limit clamped to 1..500."""
    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        limit = 50
    limit = max(1, min(500, limit))
    entries = _load(_aware(now))
    entries.sort(key=lambda e: e["_t"])
    return [_public(e) for e in reversed(entries)][:limit]


def place_state(place, now=None):
    """"in" if the place's latest event is join/tap, "out" if leave, else "unknown"."""
    latest = None
    for e in _load(_aware(now)):
        if e["place"] == place and (latest is None or e["_t"] >= latest["_t"]):
            latest = e
    if latest is None:
        return "unknown"
    return "out" if latest["event"] == "leave" else "in"
