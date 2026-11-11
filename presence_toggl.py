"""Presence policy: a phone join/tap starts a Toggl Focus timer, a leave stops only that timer.

POLICY (decided 2026-11-10)
- Time is the engine's receive time (`now`), never anything the phone sent. Toggl is acted on
  only after presence_events.record returned "ok": a duplicate, rate-limited or invalid call
  never reaches this module (the /presence/event route calls dispatch only on "ok").
- The phone's reply is always the bare {"status": ...} word; dispatch runs on a daemon thread
  and swallows everything, so whatever Toggl does never changes the reply.
- Env, read at call time inside apply, never stored or printed: TOGGL_API_TOKEN, TOGGL_ORG_ID,
  TOGGL_WORKSPACE_ID (digits only). Any missing/blank/non-digit -> "skipped_no_config", Toggl
  not contacted.
- Focus `stop` takes no entry id and ends WHATEVER runs; `start` REPLACES a running timer. So
  every event calls `current` at most once first, and the engine only ever stops or replaces
  the ONE entry it started itself (kept in presence_timer_state.json). Kevin's own timer is
  never stopped or replaced. STOP_ANY_ON_TAP stays False; there is no switch for it.
- Rules for an accepted event, in order:
  1. Quota gate: blocked_until in the future -> skipped_over_quota; a recorded
     X-Toggl-Quota-Remaining below 4 seen within the hour -> skipped_low_quota. An absent
     header is "no information", never zero. No retry, no sleep, no loop. A quota error sets
     blocked_until from resets_in seconds (missing/unreadable -> now + 15 minutes).
  2. join/tap <place> (a tap is a join with no matching leave): own entry for the same place
     still running -> already_tracking; a foreign running timer -> skipped_foreign_timer; own
     entry for another place running -> stop it, then continue; project id from the cache
     (TTL 1h) or one project_ids call, not found -> skipped_no_project (never a project-less
     timer, not cached); then start -> started.
  3. leave <place>: only if the state's place is <place>: current once; own entry running ->
     stop -> stopped; none running -> already_stopped; a foreign one running ->
     skipped_foreign_timer (NO stop call). Empty state or another place -> nothing_to_stop with
     no request. A stop that 404s -> already_stopped.
  4. Charging events (a charger in/out, the `bed` place key) are ordinary join/leave on their
     key; there is no special code. A phone charging all day runs a timer all day: that is the
     mapping in PRESENCE_PLACES, not this module's choice.
- No flap smoothing: each accepted call is acted on in order.
- Failures never propagate: a TogglError becomes "error:<ClassName>", an OSError writing state
  "error:OSError"; a 401/403 leaves the state untouched; a 500 on stop leaves the state intact
  so a later leave retries.
- Request budget: warm-cache tap = current + start = 2; cold-cache tap = 3; switching places
  = 4 cold (3 warm); leave = 2; nothing else is ever requested.
- Files beside the presence log (manual_hours._log_dir): presence_timer_state.json
  ({"entry": {entry_id, place, started_at} | null, "quota": {quota_remaining, quota_seen_at,
  blocked_until}}), presence_project_cache.json ({place: {project_id, at}}), and one line per
  call in presence_actions_<YYYY-MM>.jsonl with exactly {at, place, event, outcome}. Writes are
  atomic (temp file + os.replace). Token, project names, org/workspace ids and response bodies
  are never stored; a corrupt or missing file reads as empty.
"""
import json
import logging
import os
import queue
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import manual_hours
import presence_events
import toggl_timer

STOP_ANY_ON_TAP = False  # never overridden: a tap never stops a timer the engine did not start
LOW_QUOTA = 4
CACHE_TTL = timedelta(hours=1)
DEFAULT_BLOCK = timedelta(minutes=15)
STATE_FILE = "presence_timer_state.json"
CACHE_FILE = "presence_project_cache.json"
_LOCK = threading.Lock()


def _now(now):
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _read_json(name):
    try:
        with open(os.path.join(manual_hours._log_dir(), name), "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _write_json(name, obj):
    d = manual_hours._log_dir()
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(d, name))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _log_outcome(place, event, outcome, now):
    try:
        d = manual_hours._log_dir()
        os.makedirs(d, exist_ok=True)
        line = json.dumps({"at": _iso(now), "place": place, "event": event,
                           "outcome": outcome}) + "\n"
        path = os.path.join(d, "presence_actions_%04d-%02d.jsonl" % (now.year, now.month))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _clean_state(raw):
    e = raw.get("entry")
    entry = None
    if (isinstance(e, dict) and type(e.get("entry_id")) is int
            and isinstance(e.get("place"), str)):
        entry = {"entry_id": e["entry_id"], "place": e["place"],
                 "started_at": e.get("started_at") if isinstance(e.get("started_at"), str) else ""}
    q = raw.get("quota") if isinstance(raw.get("quota"), dict) else {}
    rem = q.get("quota_remaining")
    seen = q.get("quota_seen_at")
    blk = q.get("blocked_until")
    return {"entry": entry,
            "quota": {"quota_remaining": rem if type(rem) is int else None,
                      "quota_seen_at": seen if isinstance(seen, str) else None,
                      "blocked_until": blk if isinstance(blk, str) else None}}


def _config(env):
    env = os.environ if env is None else env
    vals = []
    for k in ("TOGGL_API_TOKEN", "TOGGL_ORG_ID", "TOGGL_WORKSPACE_ID"):
        v = env.get(k)
        v = v.strip() if isinstance(v, str) else ""
        if not v:
            return None
        if k != "TOGGL_API_TOKEN":
            if not re.fullmatch(r"[0-9]+", v):
                return None
            v = int(v)
        vals.append(v)
    return tuple(vals)


def _gate(st, now, event):
    q = st["quota"]
    b = _ts(q["blocked_until"]) if q["blocked_until"] else None
    if b and b > now:
        return "skipped_over_quota"
    seen = _ts(q["quota_seen_at"]) if q["quota_seen_at"] else None
    if event == "leave":
        return None  # a stop is never skipped for low quota, only for a real block
    if (q["quota_remaining"] is not None and seen and now - seen < CACHE_TTL
            and q["quota_remaining"] < LOW_QUOTA):
        return "skipped_low_quota"
    return None


def _project_id(token, org, ws, place, now, http, on_reply):
    cache = _read_json(CACHE_FILE)
    try:
        name = presence_events.parse_places(os.environ.get("PRESENCE_PLACES")).get(place)
    except presence_events.PresenceInvalid:
        name = None
    if not name:
        return None
    hit = cache.get(place)
    if (isinstance(hit, dict) and type(hit.get("project_id")) is int
            and hit.get("label") == name):
        at = _ts(hit.get("at"))
        if at and now - at < CACHE_TTL:
            return hit["project_id"]
    pid = toggl_timer.project_ids(token, org, ws, [name], http, on_reply)[name]
    if pid is None:
        return None
    cache[place] = {"project_id": pid, "label": name, "at": _iso(now)}
    _write_json(CACHE_FILE, {k: v for k, v in cache.items() if isinstance(v, dict)})
    return pid


def _act(place, event, now, http, cfg, st, on_reply):
    token, org, ws = cfg
    cur_box = []

    def get_cur():
        if not cur_box:
            cur_box.append(toggl_timer.current(token, org, ws, http, on_reply))
        return cur_box[0]

    entry = st["entry"]
    if event == "leave":
        if not entry or entry["place"] != place:
            return "nothing_to_stop"
        cur = get_cur()
        if cur and cur["id"] == entry["entry_id"]:
            try:
                toggl_timer.stop(token, org, ws, now, http, on_reply)
            except toggl_timer.TogglEntryGone:
                st["entry"] = None
                return "already_stopped"
            st["entry"] = None
            return "stopped"
        st["entry"] = None
        return "skipped_foreign_timer" if cur else "already_stopped"

    cur = get_cur()
    if entry:
        if cur and cur["id"] == entry["entry_id"]:
            if entry["place"] == place:
                return "already_tracking"
            try:
                toggl_timer.stop(token, org, ws, now, http, on_reply)
            except toggl_timer.TogglEntryGone:
                pass  # already stopped by hand: carry on to the new place
            st["entry"] = None
            cur = None
        else:
            st["entry"] = None
    if cur:
        return "skipped_foreign_timer"
    pid = _project_id(token, org, ws, place, now, http, on_reply)
    if pid is None:
        return "skipped_no_project"
    started = toggl_timer.start(token, org, ws, pid, now, http, on_reply=on_reply)
    st["entry"] = {"entry_id": started["id"], "place": place, "started_at": _iso(now)}
    return "started"


def apply(place, event, now=None, http=None, env=None):
    """Run the policy for one accepted event synchronously; returns the outcome word."""
    now = _now(now)
    with _LOCK:
        outcome = _apply_locked(place, event, now, http, env)
    _log_outcome(place, event, outcome, now)
    return outcome


def _apply_locked(place, event, now, http, env):
    cfg = _config(env)
    if cfg is None:
        logging.warning("[PRESENCE] toggl not configured; skipping")
        return "skipped_no_config"
    http = http or toggl_timer._default_http
    st = _clean_state(_read_json(STATE_FILE))
    before = json.dumps(st, sort_keys=True)
    blocked = _gate(st, now, event)
    if blocked:
        return blocked
    q = st["quota"]

    def on_reply(status, headers):
        raw = toggl_timer._hdr(headers, "X-Toggl-Quota-Remaining")
        try:
            q["quota_remaining"] = int(str(raw).strip())
            q["quota_seen_at"] = _iso(now)
        except (ValueError, TypeError):
            pass

    try:
        outcome = _act(place, event, now, http, cfg, st, on_reply)
    except toggl_timer.TogglQuotaExceeded as e:
        try:
            secs = int(str(e.resets_in).strip())
            if secs < 0:
                raise ValueError
            delta = timedelta(seconds=secs)
        except (ValueError, TypeError):
            delta = DEFAULT_BLOCK
        q["blocked_until"] = _iso(now + delta)
        outcome = "error:TogglQuotaExceeded"
    except toggl_timer.TogglError as e:
        # A failed call persists nothing (no quota or entry changes made along the way).
        return "error:" + type(e).__name__
    except OSError:
        return "error:OSError"
    if json.dumps(st, sort_keys=True) != before:
        try:
            _write_json(STATE_FILE, st)
        except OSError:
            outcome = "error:OSError"
    return outcome


_QUEUE = queue.Queue()
_WORKER_LOCK = threading.Lock()
_worker = None


def _work():
    while True:
        place, event, done = _QUEUE.get()
        try:
            apply(place, event)
        except BaseException as e:
            try:
                logging.warning("[PRESENCE] toggl apply failed: %s", type(e).__name__)
            except Exception:
                pass
        finally:
            done.set()


def dispatch(place, event):
    """Queue an accepted event for the single FIFO worker and return at once.

    One worker applies events in the order they were accepted, so a leave can never overtake
    its join. Never raises. Returns an Event that is set when that event has been applied.
    """
    global _worker
    done = threading.Event()
    try:
        with _WORKER_LOCK:
            _QUEUE.put((place, event, done))
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_work, daemon=True)
                _worker.start()
    except Exception:
        done.set()
    return done
