"""Read-only: ask Kevin's Toggl Focus account what the Focus API really answers.

  python scripts/measure_toggl.py --record DIR --org ORG_ID --workspace WS_ID [--days N] [--check-bad-token]

The key comes from the environment variable TOGGL_API_TOKEN only (never an argument) and is never
printed or written: it is redacted from the report, the raw file and the scrubbed file. The org and
workspace ids are digits that go into URL paths; in everything printed and in the scrubbed file they
appear as <org> and <ws>. --record DIR must resolve outside the repo. --days defaults to 14 and is
clamped to 1..31. At most 12 requests (13 with --check-bad-token), at most 3 pages per list query; the
run stops at the first HTTP 402 or 429. Redirects are never followed, so the Authorization header
cannot be forwarded elsewhere. Only https://focus.toggl.com/ is contacted.

Every request is a GET. Nothing in this file creates, updates or deletes anything on the account.

What is known by a real call: only GET /api/users/me/settings with a Bearer key answers 200. Every
other endpoint, parameter name and field name below comes from Toggl's Focus docs and is UNVERIFIED;
the report says what actually happened.

The report prints SHAPE only: statuses, key names, value type names, counts, durations, totals.
Project names, descriptions, tags and emails are never printed; projects appear as Project 1..n.

Writes DIR/toggl_focus_<UTC stamp>.json (redacted raw replies) and .scrubbed.json (safe to commit).
This script imports nothing from the repo.
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PREFIX = "https://focus.toggl.com/"
SETTINGS_PATH = "api/users/me/settings"
FAKE_TOKEN = "0" * 32
MAX_REQUESTS = 12
MAX_PAGES = 3
DUR_KEYS = ("duration", "duration_seconds")
START_KEYS = ("start", "started_at", "start_time")
END_KEYS = ("end", "ended_at", "end_time", "stop")
PROJECT_KEYS = ("project_id", "projectId")
CLOSING = ("Open Toggl Focus in the browser, the same date range, and write the web total next to the "
           "number above in the Log. Then copy the .scrubbed.json file to fixtures/toggl_focus.json "
           "and paste this report into the Log of this task.")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


class _Limited(Exception):
    def __init__(self, status):
        Exception.__init__(self, status)
        self.status = status


def _default_http(method, url, headers=None, json=None, timeout=20):
    import requests
    r = requests.request(method, url, headers=headers, json=json, timeout=timeout,
                         allow_redirects=False)
    return r.status_code, dict(r.headers), r.content


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _redactor(token):
    """Redact the key before anything is truncated or written: raw, base64, json-escaped and
    URL-quoted forms (quote with and without '/' safe, quote_plus)."""
    b64 = base64.b64encode(token.encode("utf-8")).decode("ascii") if token else ""
    base = [x for x in (token, b64) if x]
    secrets = list(base)
    for x in base:
        forms = [json.dumps(x)[1:-1], json.dumps(x, ensure_ascii=False)[1:-1],
                 urllib.parse.quote(x, safe=""), urllib.parse.quote(x), urllib.parse.quote_plus(x)]
        for f in forms:
            if f and f not in secrets:
                secrets.append(f)
    secrets.sort(key=len, reverse=True)

    def red(text):
        text = str(text)
        for x in secrets:
            text = text.replace(x, "***")
        return text
    return red


def _hide_ids(text, org, ws):
    """Whole-number match only, and only used on printed URLs/paths and error text, never on
    fixture values (counts, aliases, durations)."""
    for i, t in ((org, "<org>"), (ws, "<ws>")):
        if i:
            text = re.sub(r"(?<!\d)%s(?!\d)" % re.escape(i), t, text)
    return text


def _hours(seconds):
    return "%.2f h (%d s)" % (seconds / 3600.0, seconds)


def _keys(obj):
    if isinstance(obj, dict):
        return sorted(obj.keys())
    return None


def _typed(obj):
    """'key (type), ...' for a dict: names and value type names only, never values."""
    return ", ".join("%s (%s)" % (k, type(obj[k]).__name__) for k in sorted(obj)) or "(none)"


def _loads(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _items(parsed):
    """(list, holding key or None) for a bare list or an object holding one list; else (None, None)."""
    if isinstance(parsed, list):
        return parsed, None
    if isinstance(parsed, dict):
        for k in sorted(parsed):
            if isinstance(parsed[k], list):
                return parsed[k], k
    return None, None


def _idlike(kl):
    return kl == "id" or kl.endswith("id") or kl.endswith("ids")


def _pid_key(v):
    return None if v is None or isinstance(v, (dict, list)) else str(v)


def _scrub(obj, aliases, projmap=None, in_projects=False, key=""):
    """Keep structure, key names, non-id numbers, booleans, ISO timestamps; alias ids (int or string);
    project names become Project <k>; every other string becomes <text>."""
    kl = key.lower()
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if (in_projects and k == "name" and isinstance(v, str) and projmap
                    and _pid_key(obj.get("id")) in projmap):
                out[k] = "Project %d" % projmap[_pid_key(obj["id"])]
            elif (k == "project_name" and isinstance(v, str) and projmap
                    and _pid_key(obj.get("project_id", obj.get("projectId"))) in projmap):
                out[k] = "Project %d" % projmap[_pid_key(obj.get("project_id", obj.get("projectId")))]
            else:
                out[k] = _scrub(v, aliases, projmap, in_projects, k)
        return out
    if isinstance(obj, list):
        return [_scrub(v, aliases, projmap, in_projects, key) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if _idlike(kl) and (isinstance(obj, str) or _num(obj)):
        return aliases.setdefault(str(obj), 1001 + len(aliases))
    if _num(obj):
        return obj
    if isinstance(obj, str):
        return obj if _ISO.match(obj) else "<text>"
    return "<text>"


def _utc(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def pick_fields(entries):
    """Which field names carry duration, start, end and project (UNVERIFIED names, looked up)."""
    def first(names, test):
        for n in names:
            if any(test(e.get(n)) for e in entries if n in e):
                return n
        return None
    f = {"dur": first(DUR_KEYS, _num), "start": first(START_KEYS, lambda v: v not in (None, "")),
         "end": first(END_KEYS, lambda v: v not in (None, "")),
         "project": next((n for n in PROJECT_KEYS if any(n in e for e in entries)), None)}
    f["mode"] = ("duration" if f["dur"] else
                 "start/end" if f["start"] and f["end"] else None)
    return f


def _duration(e, f):
    if f["dur"]:
        v = e.get(f["dur"])
        return v if _num(v) else None
    try:
        return (_utc(e.get(f["end"])) - _utc(e.get(f["start"]))).total_seconds()
    except Exception:
        return None


def analyse(entries, project_ids, fields=None):
    """Pure: shape facts about time-entry dicts. project_ids None = list not fetched / has no ids."""
    entries = [e for e in entries if isinstance(e, dict)]
    f = fields or pick_fields(entries)
    alias = {pid: i + 1 for i, pid in enumerate(project_ids or [])}
    res = {"count": len(entries), "keys": sorted({k for e in entries for k in e}), "fields": f,
           "total": 0, "no_project": [0, 0], "short": 0, "running": [], "unknown": [0, 0],
           "by_project": {}, "per_day": {}, "durations": [], "starts": []}
    for e in entries:
        dur = _duration(e, f)
        if dur is not None:
            res["durations"].append(dur)
        if f["start"]:
            try:
                st = _utc(e.get(f["start"]))
                res["starts"].append(st)
                day = st.strftime("%Y-%m-%d")
                res["per_day"][day] = res["per_day"].get(day, 0) + 1
            except Exception:
                pass
        if dur is None or dur < 0 or (f["end"] and not e.get(f["end"])):
            res["running"].append(dur if dur is not None else "none")
            continue
        res["total"] += dur
        if dur < 60:
            res["short"] += 1
        pid = _pid_key(e.get(f["project"])) if f["project"] else None
        if pid in (None, ""):
            res["no_project"][0] += 1
            res["no_project"][1] += dur
            label = "(no project)"
        elif project_ids is not None and pid not in alias:
            res["unknown"][0] += 1
            res["unknown"][1] += dur
            label = "unknown project"
        else:
            if pid not in alias:
                alias[pid] = len(alias) + 1
            label = "Project %d" % alias[pid]
        res["by_project"][label] = res["by_project"].get(label, 0) + dur
    return res


def _write_new(directory, base, suffix, data):
    for n in range(1000):
        name = "%s%s%s" % (base, "" if n == 0 else "_%d" % n, suffix)
        try:
            with open(Path(directory) / name, "xb") as fh:
                fh.write(data)
        except FileExistsError:
            continue
        return Path(directory) / name
    raise OSError("no free file name")


def main(argv=None, http=None, out=print, now=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--record", required=True, metavar="DIR")
    ap.add_argument("--org", default="")
    ap.add_argument("--workspace", default="")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--check-bad-token", action="store_true")
    args = ap.parse_args(argv)
    http = http or _default_http
    days = max(1, min(31, args.days))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    token = os.environ.get("TOGGL_API_TOKEN", "").strip()
    red = _redactor(token)

    def say(text, ids=False):
        text = red(text)
        out(_hide_ids(text, args.org, args.workspace) if ids else text)

    if not token:
        say("TOGGL_API_TOKEN is not set; no request made. Set it under your user environment "
            "variables, then open a new terminal.")
        return 2
    if not re.fullmatch(r"\d+", args.org) or not re.fullmatch(r"\d+", args.workspace):
        say("--org and --workspace are required and must be digits only; no request made.")
        return 2
    if _inside(args.record, ROOT):
        say("refused: --record is inside the repo; pick a folder outside it")
        return 2
    try:
        Path(args.record).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        say("cannot create --record: " + type(exc).__name__)
        return 2

    base = "api/organizations/%s/workspaces/%s/" % (args.org, args.workspace)
    date_to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_from = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = []        # raw replies, in order
    bodies = {}     # name -> parsed JSON (successful replies only)
    state = {"n": 0}

    def call(name, url, tok):
        if not url.startswith(PREFIX):
            raise ValueError("refused: only " + PREFIX)
        limit = MAX_REQUESTS + (1 if args.check_bad_token else 0)
        if state["n"] >= limit:
            raise RuntimeError("request budget used")
        state["n"] += 1
        headers = {"Authorization": "Bearer " + tok}
        path = "/" + url[len(PREFIX):].split("?")[0]
        entry = {"name": name, "method": "GET", "url": url, "status": None, "content_type": "",
                 "body": ""}
        log.append(entry)
        try:
            status, rh, raw = http("GET", url, headers=headers, json=None, timeout=20)
        except Exception as exc:
            say("GET %s -> request failed: %s" % (path, type(exc).__name__), ids=True)
            return None, {}, b""
        raw = raw if isinstance(raw, bytes) else b""
        rh = rh or {}
        entry["status"] = status
        entry["content_type"] = next((v for k, v in rh.items() if k.lower() == "content-type"), "")
        entry["body"] = red(raw.decode("utf-8", "replace"))
        parsed = _loads(raw)
        keys = _keys(parsed)
        extra = ["%s=%s" % (k, v) for k, v in rh.items()
                 if any(w in k.lower() for w in ("quota", "rate", "retry"))]
        if parsed is None:
            shape = "(no JSON body)"
        elif keys is not None:
            shape = ", ".join(keys) if keys else "(empty object)"
        elif isinstance(parsed, list):
            shape = "list of %d" % len(parsed)
        else:
            shape = "(JSON %s)" % type(parsed).__name__
        say("GET %s -> HTTP %s; top-level keys: %s%s" % (
            path, status, shape, "; headers: " + ", ".join(extra) if extra else ""), ids=True)
        if status in (402, 429):
            raise _Limited(status)
        if status is not None and 300 <= status < 400:
            say("redirect not followed (status %s)" % status)
        if status == 200 and parsed is not None:
            bodies[name] = parsed
        return status, rh, raw

    def prefix(raw, rh):
        ct = next((v for k, v in rh.items() if k.lower() == "content-type"), "")
        return ct, red(raw.decode("utf-8", "replace"))[:200]

    def paged(name, path, query):
        """Pages until an empty page, max MAX_PAGES. Returns combined items, or None if unavailable.
        bodies[name] always holds what was combined so far, in the shape the real reply has."""
        combined, prev, holder = [], None, None

        def store():
            bodies[name] = {holder: combined} if holder else combined
            return combined if combined else None

        for page in range(1, MAX_PAGES + 1):
            status, rh, raw = call(name, "%s%s%s?%s&page=%d" % (PREFIX, base, path, query, page),
                                   token)
            if status != 200:
                if status is not None and not 300 <= status < 400:
                    ct = next((v for k, v in rh.items() if k.lower() == "content-type"), "")
                    say("%s unavailable: status %s; Content-Type: %s" % (name, status, ct))
                return store() if combined else (bodies.pop(name, None) and None)
            parsed = _loads(raw)
            items, h = _items(parsed)
            if items is None:  # account data: describe the shape, never print the body
                say("%s: unexpected 200 body; JSON type: %s; keys: %s" % (
                    name, type(parsed).__name__, ", ".join(_keys(parsed) or []) or "(none)"))
                return store() if combined else (bodies.pop(name, None) and None)
            if page == 1:
                holder = h
                say("%s: reply is an object holding the list under key '%s'" % (name, h) if h
                    else "%s: reply is a bare list" % name)
            if not items:
                break
            if prev is not None and items == prev:
                say("%s: paging parameter appears ignored (page %d identical to page %d); "
                    "stopped paging this query" % (name, page, page - 1))
                break
            combined.extend(items)
            prev = items
        else:
            say("%s: page cap reached, results truncated (%d pages of up to 100); totals for this "
                "query are undercounted" % (name, MAX_PAGES))
        store()
        return combined

    def finish(code):
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        fname = "toggl_focus_" + stamp
        try:
            raw_doc = json.dumps({"requests": log}, indent=2)
            say("raw replies saved: %s" % _write_new(args.record, fname, ".json",
                                                     red(raw_doc).encode("utf-8")))
            projs = _items(bodies.get("projects"))[0]
            idd = [str(p["id"]) for p in projs
                   if isinstance(p, dict) and _pid_key(p.get("id")) is not None] \
                if isinstance(projs, list) else []
            pm = {pid: i + 1 for i, pid in enumerate(idd)}
            aliases = {}
            scrubbed = {}
            for name in ("settings", "tracking_current", "projects", "time_entries",
                         "time_entries_taskless", "bad_token"):
                if name in bodies:
                    scrubbed[name] = _scrub(bodies[name], aliases, pm, in_projects=(name == "projects"))
            for e in log:
                if e["name"] not in scrubbed:
                    scrubbed.setdefault("not_ok", []).append(
                        {"name": e["name"], "status": e["status"], "content_type": e["content_type"]})
            safe = red(json.dumps(scrubbed, indent=2))
            say("scrubbed copy saved: %s" % _write_new(args.record, fname, ".scrubbed.json",
                                                       safe.encode("utf-8")))
        except Exception as exc:
            say("could not save files: " + type(exc).__name__)
        say(CLOSING)
        return code

    try:
        # 1. the token check
        status, rh, raw = call("settings", PREFIX + SETTINGS_PATH, token)
        if status != 200:
            if status is None:
                say("request failed (no answer)")
            elif not 300 <= status < 400:
                ct, body = prefix(raw, rh)
                say("status %s; Content-Type: %s; body starts: %s" % (status, ct, body), ids=True)
                if status in (401, 403):
                    say("The token was refused by the Focus API. See the status above.")
            return finish(1)
        sett = bodies.get("settings")
        say("settings keys: %s" % (_typed(sett) if isinstance(sett, dict)
                                   else "(JSON %s, not an object)" % type(sett).__name__))

        # 2. running timer (UNVERIFIED path)
        status, rh, raw = call("tracking_current", PREFIX + base + "tracking/current", token)
        if status == 204:
            say("idle (204, empty body)")
        elif status == 200:
            cur = bodies.get("tracking_current")
            if isinstance(cur, dict):
                say("running timer keys: " + _typed(cur))
                say("start field present: %s; project field present: %s" % (
                    "yes" if any(k in cur for k in START_KEYS) else "no",
                    "yes" if any(k in cur for k in PROJECT_KEYS) else "no"))
            else:
                say("tracking/current answered 200; JSON type: %s" % type(cur).__name__)
        elif status is not None and not 300 <= status < 400:
            say("tracking/current answered HTTP %s (reported as is)" % status)

        # 3. projects (UNVERIFIED path)
        projs = paged("projects", "projects", "per_page=100")
        project_ids = None
        if projs is not None:
            say("projects: %d" % len(projs))
            first = next((p for p in projs if isinstance(p, dict)), None)
            if first is not None:
                say("project keys: " + _typed(first))
            ids = [str(p["id"]) for p in projs if isinstance(p, dict) and _pid_key(p.get("id"))]
            project_ids = ids or None

        # 4 and 5. time entries, without and with task-less entries (UNVERIFIED parameters)
        q = "date_from=%s&date_to=%s&per_page=100" % (date_from, date_to)
        default = paged("time_entries", "time-entries", q)
        every = paged("time_entries_taskless", "time-entries", q + "&include_taskless=true")
        say("time entries, default query: %s" % (len(default) if default is not None else "unavailable"))
        say("time entries, include_taskless=true: %s" % (len(every) if every is not None else "unavailable"))
        if default is not None and every is not None:
            say("entries with no task, hidden by the default query: %d" % (len(every) - len(default)))

        # 6. a made-up key, only on request
        if args.check_bad_token:
            status, rh, raw = call("bad_token", PREFIX + SETTINGS_PATH, FAKE_TOKEN)
            ct, body = prefix(raw, rh)
            say("bad token check: status %s; Content-Type: %s; body starts: %s" % (status, ct, body),
                ids=True)
            say("Note: a key you refreshed or revoked in Toggl may answer differently from a made-up "
                "one. If the snapshot ever fails, compare its status and body with this one.")

        # report
        entries = every if every is not None else default
        if not entries:
            say("no time entries to report on.")
            return finish(0)
        say("report uses the %s query" % ("include_taskless=true" if every is not None else "default"))
        r = analyse(entries, project_ids)
        f = r["fields"]
        say("window: %s to %s (UTC), %d days" % (date_from, date_to, days))
        say("entry keys: " + _typed({k: v for k, v in _first_values(entries).items()}))
        if not f["mode"]:
            say("cannot total: no known duration or start/end field; see the key names above")
            return finish(0)
        say("fields used: duration=%s, start=%s, end=%s, project=%s; unit assumed SECONDS, UNVERIFIED"
            % (f["dur"], f["start"], f["end"], f["project"]))
        say("total (running entries excluded): %s" % _hours(r["total"]))
        if r["durations"]:
            say("duration smallest: %s, largest: %s" % (min(r["durations"]), max(r["durations"])))
        say("entries with no project: %d, %s" % (r["no_project"][0], _hours(r["no_project"][1])))
        say("entries shorter than 60 s: %d" % r["short"])
        say("running entries: %d; duration holds: %s" % (len(r["running"]), r["running"]))
        if r["starts"]:
            say("earliest start: %s" % min(r["starts"]).isoformat())
            say("latest start: %s" % max(r["starts"]).isoformat())
        say("entries per UTC day:")
        days_all = {(now - timedelta(days=d)).strftime("%Y-%m-%d"): 0 for d in range(days, -1, -1)}
        days_all.update(r["per_day"])
        for d in sorted(days_all):
            say("  %s: %d" % (d, days_all[d]))
        say("hours per project:")
        for label in sorted(r["by_project"]):
            say("  %s: %s" % (label, _hours(r["by_project"][label])))
        if project_ids is not None:
            say("entries with unknown project: %d, %s" % (r["unknown"][0], _hours(r["unknown"][1])))
        named = [k for k in r["by_project"] if k.startswith("Project ")]
        say("distinct projects: %d" % (len(set(project_ids)) if project_ids is not None else len(named)))
        return finish(0)
    except _Limited as lim:
        say("Toggl is limiting this key (HTTP %s); stopped, no further request made." % lim.status)
        return finish(1)
    except Exception as exc:
        say("stopped: " + type(exc).__name__ + ": " + str(exc), ids=True)
        return finish(1)


def _first_values(entries):
    """One example value per entry key (used only for its type name)."""
    seen = {}
    for e in entries:
        if isinstance(e, dict):
            for k, v in e.items():
                if k not in seen or seen[k] is None:
                    seen[k] = v
    return seen


if __name__ == "__main__":
    sys.exit(main())
