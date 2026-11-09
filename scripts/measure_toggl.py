"""Read-only: ask Kevin's Toggl account what the Track API v9 and Reports API v3 really answer.

  python scripts/measure_toggl.py --record DIR [--days N] [--check-bad-token]

The token comes from the environment variable TOGGL_API_TOKEN only (never an argument) and is never
printed or written: it is redacted from the report, the raw file and the scrubbed file. --record DIR
must resolve outside the repo. --days defaults to 14 and is clamped to 1..31. At most 6 requests
(7 with --check-bad-token); the run stops at the first HTTP 429. Redirects are never followed, so the
Authorization header cannot be forwarded elsewhere. Only https://api.track.toggl.com/ is contacted.

Every request is a GET, except the single Reports API call, which is a POST that only asks for a
summary and creates nothing. Nothing in this file updates or deletes anything on the account.

The report prints SHAPE only: statuses, key names, counts, durations, totals. Project names,
descriptions, tags and emails are never printed; projects appear as Project 1..n.

Writes DIR/toggl_account_<UTC stamp>.json (raw replies) and .scrubbed.json (safe to commit).
This script imports nothing from the repo.
"""
import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PREFIX = "https://api.track.toggl.com/"
ME_URL = PREFIX + "api/v9/me"
ENTRIES_URL = PREFIX + "api/v9/me/time_entries"
PROJECTS_URL = PREFIX + "api/v9/workspaces/%s/projects"
REPORTS_URL = PREFIX + "reports/api/v3/workspace/%s/summary/time_entries"
FAKE_TOKEN = "0" * 32
MAX_REQUESTS = 6
CLOSING = ("Open Toggl in the browser, Reports, the same date range, and write the web total and the "
           "number above in the Log. The two must agree before the snapshot module relies on this. "
           "Then copy the .scrubbed.json file to fixtures/toggl_week.json and paste this report into "
           "the Log.")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


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


def _basic(token):
    # UNVERIFIED: username = the token, password = the literal "api_token" (recalled from Toggl's
    # docs, never tested by us). The report shows what actually happens.
    return base64.b64encode(("%s:api_token" % token).encode("utf-8")).decode("ascii")


def _redactor(token):
    base = [s for s in (_basic(token), token) if s]
    secrets = list(base)
    for s in base:  # the forms a secret takes inside json.dumps output
        for esc in (json.dumps(s)[1:-1], json.dumps(s, ensure_ascii=False)[1:-1]):
            if esc not in secrets:
                secrets.append(esc)

    def red(text):
        text = str(text)
        for s in secrets:
            text = text.replace(s, "***")
        return text
    return red


def _hours(seconds):
    return "%.2f h (%d s)" % (seconds / 3600.0, seconds)


def _keys(obj):
    if isinstance(obj, dict):
        return sorted(obj.keys())
    return None


def _loads(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _scrub(obj, aliases, projmap=None, in_projects=False, key=""):
    """Keep structure, key names, non-id numbers, booleans, ISO timestamps; alias ids; blank text."""
    kl = key.lower()
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if (in_projects and k == "name" and isinstance(v, str) and projmap
                    and obj.get("id") in projmap):
                out[k] = "Project %d" % projmap[obj["id"]]
            elif (k == "project_name" and isinstance(v, str) and projmap
                    and obj.get("project_id") in projmap):
                out[k] = "Project %d" % projmap[obj["project_id"]]
            else:
                out[k] = _scrub(v, aliases, projmap, in_projects, k)
        return out
    if isinstance(obj, list):
        return [_scrub(v, aliases, projmap, in_projects, key) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if _num(obj):
        if (kl == "id" or kl.endswith("id") or kl.endswith("ids")) and isinstance(obj, int):
            return aliases.setdefault(obj, 1001 + len(aliases))
        return obj
    if isinstance(obj, str):
        return obj if _ISO.match(obj) else "<text>"
    return "<text>"


def _utc(s):
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def analyse(entries, project_ids, window_days):
    """Pure: shape facts about a list of time-entry dicts. project_ids None = list not fetched."""
    entries = [e for e in entries if isinstance(e, dict)]
    alias = {pid: i + 1 for i, pid in enumerate(project_ids or [])}
    res = {"count": len(entries), "keys": sorted({k for e in entries for k in e}),
           "total": 0, "no_project": [0, 0], "short": 0, "running": [], "unknown": [0, 0],
           "by_project": {}, "per_day": {}, "durations": [], "starts": []}
    for e in entries:
        dur = e.get("duration")
        if _num(dur):
            res["durations"].append(dur)
        try:
            st = _utc(e.get("start"))
            res["starts"].append(st)
            day = st.strftime("%Y-%m-%d")
            res["per_day"][day] = res["per_day"].get(day, 0) + 1
        except Exception:
            pass
        if not _num(dur) or dur < 0 or not e.get("stop"):
            res["running"].append(dur)
            continue
        res["total"] += dur
        if dur < 60:
            res["short"] += 1
        pid = e.get("project_id")
        if pid is None:
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


def reports_total_seconds(body):
    """Total seconds in a Reports v3 summary reply (shape recalled, UNVERIFIED), or None."""
    if not isinstance(body, dict) or not isinstance(body.get("groups"), list):
        return None
    total = 0
    for g in body["groups"]:
        if not isinstance(g, dict):
            return None
        if _num(g.get("seconds")):
            total += g["seconds"]
        else:
            total += sum(s.get("seconds", 0) for s in g.get("sub_groups") or []
                         if isinstance(s, dict) and _num(s.get("seconds")))
    return total


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

    def say(text):
        out(red(text))

    if not token:
        say("TOGGL_API_TOKEN is not set; no request made. Set it under your user environment "
            "variables, then open a new terminal.")
        return 2
    if _inside(args.record, ROOT):
        say("refused: --record %s is inside the repo; pick a folder outside it" % args.record)
        return 2
    try:
        Path(args.record).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        say("cannot create --record: " + type(exc).__name__)
        return 2

    start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    log = []        # raw replies, in order
    bodies = {}     # name -> parsed JSON (successful replies only)
    state = {"n": 0}

    def call(name, method, url, tok, payload=None):
        if not url.startswith(PREFIX):
            raise ValueError("refused: only " + PREFIX)
        limit = MAX_REQUESTS + (1 if args.check_bad_token else 0)
        if state["n"] >= limit:
            raise RuntimeError("request budget used")
        state["n"] += 1
        headers = {"Authorization": "Basic " + _basic(tok)}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        path = url.split("?")[0][len(PREFIX) - 1:]
        entry = {"name": name, "method": method, "url": url, "status": None, "content_type": "",
                 "body": ""}
        log.append(entry)
        try:
            status, rh, raw = http(method, url, headers=headers, json=payload, timeout=20)
        except Exception as exc:
            say("%s %s -> request failed: %s" % (method, path, type(exc).__name__))
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
        say("%s %s -> HTTP %s; top-level keys: %s%s" % (
            method, path, status,
            ", ".join(keys) if keys is not None else
            ("list of %d" % len(parsed) if isinstance(parsed, list) else "(not a JSON object)"),
            "; headers: " + ", ".join(extra) if extra else ""))
        if status == 200 and parsed is not None:
            bodies[name] = parsed
        return status, rh, raw

    def prefix(entry_raw, rh):
        ct = next((v for k, v in rh.items() if k.lower() == "content-type"), "")
        return ct, red(entry_raw.decode("utf-8", "replace"))[:200]

    rc = [0]

    def finish(code):
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        base = "toggl_account_" + stamp
        try:
            raw_doc = json.dumps({"requests": log}, indent=2)
            say("raw replies saved: %s" % _write_new(args.record, base, ".json",
                                                     red(raw_doc).encode("utf-8")))
            projs = bodies.get("projects")
            idd = [p["id"] for p in projs if isinstance(p, dict) and "id" in p]                 if isinstance(projs, list) else []
            pm = {pid: i + 1 for i, pid in enumerate(idd)}
            aliases = {}
            scrubbed = {}
            for name in ("me", "time_entries", "projects", "reports"):
                if name in bodies:
                    scrubbed[name] = _scrub(bodies[name], aliases, pm, in_projects=(name == "projects"))
            for e in log:
                if e["name"] not in scrubbed:
                    scrubbed.setdefault("not_ok", []).append(
                        {"name": e["name"], "status": e["status"], "content_type": e["content_type"]})
            safe = red(json.dumps(scrubbed, indent=2))
            say("scrubbed copy saved: %s" % _write_new(args.record, base, ".scrubbed.json",
                                                       safe.encode("utf-8")))
        except Exception as exc:
            say("could not save files: " + type(exc).__name__)
        say(CLOSING)
        return code

    def stop429(status):
        if status == 429:
            say("HTTP 429: Toggl is rate limiting this token; stopped, no further request made.")
            return True
        return False

    try:
        # 1. who am I
        status, rh, raw = call("me", "GET", ME_URL, token)
        if status != 200:
            if stop429(status):
                return finish(1)
            if status is None:
                say("request failed (no answer)")
                return finish(1)
            if 300 <= status < 400:
                say("redirect not followed (status %s)" % status)
                return finish(1)
            ct, body = prefix(raw, rh)
            say("status %s; Content-Type: %s; body starts: %s" % (status, ct, body))
            say("The token was refused by the Track API v9. See the status above.")
            return finish(1)
        me = bodies.get("me")
        wid = None
        if isinstance(me, dict):
            for k in ("default_workspace_id", "workspace_id"):
                if me.get(k) is not None:
                    wid = me[k]
                    break
        if wid is not None and not re.fullmatch(r"\d+", str(wid)):
            wid = None
        if wid is None:
            say("/me shape is unverified: no default_workspace_id or workspace_id field (keys listed "
                "above); skipping the projects and Reports requests.")

        # 2. time entries
        status, rh, raw = call("time_entries", "GET",
                               "%s?start_date=%s&end_date=%s" % (ENTRIES_URL, start, end), token)
        if stop429(status):
            return finish(1)
        entries = bodies.get("time_entries")
        if status != 200 or not isinstance(entries, list):
            ct, body = prefix(raw, rh)
            if status == 200:  # account data: describe the shape, never print the body
                parsed = bodies.get("time_entries")
                say("time entries unavailable: status 200; Content-Type: %s; JSON type: %s; keys: %s"
                    % (ct, type(parsed).__name__, ", ".join(_keys(parsed) or []) or "(none)"))
            else:
                say("time entries unavailable: status %s; Content-Type: %s; body starts: %s"
                    % (status, ct, body))
            return finish(1)

        # 3. projects
        project_ids = None
        if wid is not None:
            status, rh, raw = call("projects", "GET", PROJECTS_URL % wid, token)
            if stop429(status):
                return finish(1)
            projs = bodies.get("projects")
            if status == 200 and isinstance(projs, list):
                project_ids = [p["id"] for p in projs if isinstance(p, dict) and "id" in p]
            else:
                say("project list unavailable (HTTP %s); entries cannot be matched to projects." % status)

        # 4. Reports API (a POST that creates nothing)
        rep_total = None
        if wid is not None:
            status, rh, raw = call("reports", "POST", REPORTS_URL % wid, token,
                                   {"start_date": start, "end_date": end})
            if stop429(status):
                return finish(1)
            if status in (402, 403, 404):
                say("Reports API not available to this token/plan (HTTP %s)" % status)
            elif status == 200:
                rep_total = reports_total_seconds(bodies.get("reports"))
                if rep_total is None:
                    say("Reports API answered 200 but the total could not be read: shape unverified.")
            else:
                say("Reports API answered HTTP %s" % status)

        # 5. a bad token, only on request, with a made-up token
        if args.check_bad_token:
            status, rh, raw = call("bad_token", "GET", ME_URL, FAKE_TOKEN)
            if stop429(status):
                return finish(1)
            ct, body = prefix(raw, rh)
            say("bad token check: status %s; Content-Type: %s; body starts: %s" % (status, ct, body))
            say("Note: a token you reset or revoked in Toggl may answer differently from a made-up "
                "one. If the snapshot ever fails, compare its status and body with this one.")

        # report
        r = analyse(entries, project_ids, days)
        say("window: %s to %s (UTC), %d days" % (start, end, days))
        say("time entries: %d" % r["count"])
        say("entry keys: " + ", ".join(r["keys"]))
        say("total (running timers excluded): %s; duration assumed SECONDS, UNVERIFIED" % _hours(r["total"]))
        if r["durations"]:
            say("duration smallest: %s, largest: %s" % (min(r["durations"]), max(r["durations"])))
        say("entries with no project: %d, %s" % (r["no_project"][0], _hours(r["no_project"][1])))
        say("entries shorter than 60 s: %d" % r["short"])
        say("running timers: %d; duration holds: %s" % (len(r["running"]), r["running"]))
        if r["starts"]:
            say("earliest start: %s" % min(r["starts"]).isoformat())
            say("latest start: %s" % max(r["starts"]).isoformat())
        say("entries per UTC day:")
        days_all = {(now - timedelta(days=d)).strftime("%Y-%m-%d"): 0 for d in range(days, -2, -1)}
        days_all.update(r["per_day"])
        for d in sorted(days_all):
            say("  %s: %d" % (d, days_all[d]))
        say("hours per project:")
        for label in sorted(r["by_project"]):
            say("  %s: %s" % (label, _hours(r["by_project"][label])))
        if project_ids is not None:
            say("entries with unknown project: %d, %s" % (r["unknown"][0], _hours(r["unknown"][1])))
        if rep_total is not None:
            diff = round(rep_total / 60.0) - round(r["total"] / 60.0)
            verdict = ("agree to the minute" if diff == 0 else
                       "differ by %d minutes" % abs(diff))
            say("Reports API total %s vs time-entries total %s: %s" % (
                _hours(rep_total), _hours(r["total"]), verdict))
        say("distinct projects: %d" % (len(set(project_ids)) if project_ids is not None
                                       else len(r["by_project"])))
        return finish(rc[0])
    except Exception as exc:
        say("stopped: " + type(exc).__name__ + ": " + str(exc))
        return finish(1)


if __name__ == "__main__":
    sys.exit(main())
