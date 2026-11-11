"""Toggl Focus timer client: read the running timer, look up projects, start and stop.

SAFETY, READ THIS FIRST:
  STOP HAS NO ENTRY ID: IT STOPS WHATEVER TIMER IS RUNNING, EVEN ONE THIS CODE DID NOT START.
  START ALSO REPLACES A RUNNING TIMER (measured 2026-11-10: the old one finished).
  THIS MODULE NEVER DECIDES. THE CALLER MUST CALL current() FIRST AND ONLY START OR STOP
  WHEN THE RUNNING TIMER IS ITS OWN OR NOTHING RUNS.

Design: no environment reads, no module state, no delete. The token, org id and workspace
id are passed in on every call, and the HTTP function is injected:
    http(method, url, headers=None, json=None, timeout=20) -> (status, headers, body_bytes)

Measured (fixtures/toggl_focus_timer.json): GET tracking/current is 204 when idle and a bare
entry when running; POST tracking/start needs `type`; a start without project_id works;
POST tracking/stop with {"end": ...} returns the entry with an integer `duration`.
Documented, NOT measured: the status for a rejected key (401/403 assumed), stop with nothing
running (404 assumed), and quota headers (402 carries X-Toggl-Quota-Remaining and
X-Toggl-Quota-Resets-In on the Free plan, 30 calls per hour). Nothing here retries or sleeps.
"""
import json as _json
import re
from datetime import datetime, timezone

BASE = "https://focus.toggl.com"


class TogglError(Exception):
    pass


class TogglTokenRejected(TogglError):
    pass


class TogglQuotaExceeded(TogglError):
    def __init__(self, message, resets_in=None, remaining=None):
        super().__init__(message)
        self.resets_in = resets_in
        self.remaining = remaining


class TogglEntryGone(TogglError):
    pass


class TogglShapeError(TogglError):
    pass


def _default_http(method, url, headers=None, json=None, timeout=20):
    import requests
    r = requests.request(method, url, headers=headers, json=json, timeout=timeout,
                         allow_redirects=False)
    return r.status_code, dict(r.headers), r.content


def _check_ids(org, ws):
    for v in (org, ws):
        if type(v) is not int:
            raise TogglError("org and workspace ids must be ints")


def _base(org, ws):
    _check_ids(org, ws)
    return "%s/api/organizations/%d/workspaces/%d" % (BASE, org, ws)


def _hdr(headers, name):
    for k, v in (headers or {}).items():
        if str(k).lower() == name.lower():
            return v
    return None


def _iso(now):
    if now.tzinfo is None:
        raise TogglError("now must be timezone-aware (UTC).")
    now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _call(token, method, url, http, on_reply, body=None):
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]+", token):
        raise TogglTokenRejected(
            "The Toggl API key is empty or has spaces or control characters. Check "
            "TOGGL_API_TOKEN.")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        status, rheaders, raw = http(method, url, headers=headers, json=body, timeout=20)
    except Exception:
        raise TogglError("Toggl request failed before any reply (network error).") from None
    if on_reply is not None:
        try:
            on_reply(status, rheaders)
        except Exception:
            pass
    return status, rheaders or {}, raw


def _raise_for(status, rheaders, raw, what, stop=False):
    if status in (401, 403):
        raise TogglTokenRejected(
            "Toggl rejected the API key (HTTP %d). Check that TOGGL_API_TOKEN still holds a "
            "current key from your Toggl Focus profile." % status)
    if status in (402, 429):
        raise TogglQuotaExceeded(
            "Toggl quota exceeded (HTTP %d) on %s." % (status, what),
            resets_in=_hdr(rheaders, "X-Toggl-Quota-Resets-In"),
            remaining=_hdr(rheaders, "X-Toggl-Quota-Remaining"))
    if status == 404 and stop:
        raise TogglEntryGone("Toggl has no running timer to stop (HTTP 404).")
    detail = ""
    if status == 400:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                reason = parsed.get("error_description")
                if isinstance(reason, str):
                    m = re.match(r"Key: '([A-Za-z0-9_.]{1,60})'", reason)
                    if m:
                        detail = " " + m.group(1)
        except Exception:
            pass
    raise TogglError("Toggl %s failed (HTTP %d).%s" % (what, status, detail))


def _loads(raw, what):
    try:
        return _json.loads(raw)
    except Exception:
        raise TogglShapeError("Toggl %s reply was not JSON." % what)


def _reduce(entry, what):
    if (not isinstance(entry, dict) or type(entry.get("id")) is not int
            or not isinstance(entry.get("start"), str)):
        raise TogglShapeError("Toggl %s reply is not a timer entry." % what)
    pid = entry.get("project_id")
    if pid is not None and type(pid) is not int:
        raise TogglShapeError("Toggl %s reply has an odd project_id." % what)
    return {"id": entry["id"], "project_id": pid, "start": entry["start"]}


def current(token, org, ws, http, on_reply=None):
    url = _base(org, ws) + "/tracking/current"
    status, rh, raw = _call(token, "GET", url, http, on_reply)
    if status == 204:
        return None
    if status != 200:
        _raise_for(status, rh, raw, "current")
    return _reduce(_loads(raw, "current"), "current")


def _norm(name):
    return re.sub(r"\s+", " ", str(name)).strip().casefold()


def project_ids(token, org, ws, names, http, on_reply=None):
    base = _base(org, ws) + "/projects?per_page=100"
    found = {}
    page = 1
    while True:
        url = base if page == 1 else base + "&page=%d" % page
        status, rh, raw = _call(token, "GET", url, http, on_reply)
        if status != 200:
            _raise_for(status, rh, raw, "project list")
        parsed = _loads(raw, "project list")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), list):
            raise TogglShapeError("Toggl project list reply is not {\"data\": [...]}.")
        data = parsed["data"]
        for p in data:
            if not isinstance(p, dict) or type(p.get("id")) is not int or not isinstance(p.get("name"), str):
                continue
            key = _norm(p["name"])
            if p.get("archived_at") is not None:
                found.setdefault(key, None)
            elif found.get(key) is None:
                found[key] = p["id"]
        if len(data) < 100:
            break
        if page >= 3:
            raise TogglError("project list too long to read completely")
        page += 1
    return {n: found.get(_norm(n)) for n in names}


def start(token, org, ws, project_id, now, http, description="", on_reply=None):
    url = _base(org, ws) + "/tracking/start"
    body = {"type": "activity", "start": _iso(now), "description": description}
    if project_id is not None:
        if type(project_id) is not int:
            raise TogglError("project_id must be an int or None")
        body["project_id"] = project_id
    status, rh, raw = _call(token, "POST", url, http, on_reply, body)
    if not 200 <= status < 300:
        _raise_for(status, rh, raw, "start")
    return _reduce(_loads(raw, "start"), "start")


def stop(token, org, ws, now, http, on_reply=None):
    url = _base(org, ws) + "/tracking/stop"
    status, rh, raw = _call(token, "POST", url, http, on_reply, {"end": _iso(now)})
    if not 200 <= status < 300:
        _raise_for(status, rh, raw, "stop", stop=True)
    entry = _loads(raw, "stop")
    if not isinstance(entry, dict) or type(entry.get("id")) is not int:
        raise TogglShapeError("Toggl stop reply is not a timer entry.")
    dur = entry.get("duration")
    if type(dur) is not int:
        # The measured reply carries an integer duration; compute from start and now only if absent.
        if not isinstance(entry.get("start"), str):
            raise TogglShapeError("Toggl stop reply has no duration or start.")
        try:
            dur = max(0, int((now - _parse_ts(entry["start"])).total_seconds()))
        except Exception:
            raise TogglShapeError("Toggl stop reply has an unreadable start.")
    return {"id": entry["id"], "duration_seconds": dur}
