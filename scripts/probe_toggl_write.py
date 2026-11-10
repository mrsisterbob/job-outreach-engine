"""Supervised write probe: Kevin runs this himself, watching, on throwaway Toggl Focus entries.

  python scripts/probe_toggl_write.py --record DIR --org ORG_ID --workspace WS_ID --yes [--project-name NAME]

It starts two throwaway entries ("probe A/B - safe to delete"), checks what is running, stops the
second, deletes both and prints the SHAPE of every reply, so the timer client is built from measured
Focus write calls and not documented ones. Without --yes it prints the step names and exits 2 with
no request constructed.

The key comes from the environment variable TOGGL_API_TOKEN only (never an argument), is sent as a
Bearer header, and is never printed or written (redacted from the report, the raw file and the
scrubbed file). --org and --workspace are digits; in everything printed and in the scrubbed file they
appear as <org> and <ws>. --record DIR must resolve outside the repo. Only https://focus.toggl.com/
is contacted, redirects are never followed, at most 14 requests are sent, and the run stops at the
first HTTP 402 or 429. If a timer is already running the probe stops at once: it never stops, alters
or deletes an entry it did not create. A stop is sent ONLY right after a `current` that returned one
of the probe's own entries.

What is MEASURED (by Kevin's read-only run): Bearer auth works, lists are {"data": [...]}, one bare
entry is returned by tracking/current while a timer runs, a running entry has no `duration`.
EVERYTHING ELSE below is DOCUMENTED ONLY and UNVERIFIED, which is why this probe exists:
  - POST tracking/start: body fields description, project_id, start, ...; starting while a timer runs
    is documented to stop that timer and return the new entry.
  - POST tracking/stop: body {"end": ...}; takes NO entry id, stops whatever runs; 404 when idle.
  - GET tracking/current: 200 with the entry, 204 when idle.
  - DELETE time-entries/bulk?ids=A,B: only a bulk delete is documented (204); path and method unverified.
  - Quota per hour (Free 30); over quota is 402 with X-Toggl-Quota-Remaining / -Resets-In headers.
The report says what actually happened. The only methods used are GET, POST and DELETE.

Writes DIR/toggl_focus_probe_<UTC stamp>.json (redacted raw replies) and .scrubbed.json.
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
MAX_REQUESTS = 14
MIN_QUOTA = 14
DESC_A = "probe A - safe to delete"
DESC_B = "probe B - safe to delete"
START_TYPE = "activity"      # required on start (measured: HTTP 400 without it); or "break"
PLAN = ("Plan (nothing is sent without --yes):\n"
        "  1. check that no timer is running\n"
        "  2. optionally look up the project named by --project-name\n"
        "  3. start throwaway entry A\n"
        "  4. check that A is the running entry\n"
        "  5. start throwaway entry B while A runs\n"
        "  6. check which entry is running now\n"
        "  7. stop the running entry (only if it is A or B)\n"
        "  8. list recent entries and look at A and B\n"
        "  9. delete A and B\n"
        " 10. check that nothing is running")
RUNNING_MSG = "A timer is already running. Stop it in Toggl, then run this again. Nothing was started."
LOW_QUOTA_MSG = ("Not enough Toggl quota left this hour (%s); wait for the reset and run again. "
                 "Nothing was started.")
FOREIGN_MSG = ("A timer you did not start is running; the probe left it alone. Check Toggl: probe "
               "entries may still exist (\"probe - safe to delete\").")
MAY_EXIST_MSG = ("A probe entry may still exist in your Toggl account (\"probe A/B - safe to delete\"). "
                 "Open Toggl, find it and delete it.")
CLOSING = ("Check Toggl in the browser: both probe entries should be gone. Then copy the .scrubbed.json "
           "file to fixtures/toggl_focus_timer.json and paste this report into the Log of this task.")

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


class _Exit(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class _Abort(Exception):
    pass


def _default_http(method, url, headers=None, json=None, timeout=20):
    import requests
    # trust_env=False: no netrc, no proxy or CA environment may alter or reroute the Bearer header.
    with requests.Session() as session:
        session.trust_env = False
        r = session.request(method, url, headers=headers, json=json, timeout=timeout,
                            allow_redirects=False)
        return r.status_code, dict(r.headers), r.content


MAIN_CHECKOUT = Path("C:/Users/kjmil/job-outreach-engine")


def _in_git_tree(path):
    """True if the resolved path, or any parent, is a git work tree (has a .git entry), or lies under
    the main checkout or the repo this script lives in (which covers sibling worktrees)."""
    p = Path(path).resolve()
    if _inside(p, ROOT) or _inside(p, MAIN_CHECKOUT):
        return True
    for d in [p] + list(p.parents):
        if (d / ".git").exists():
            return True
    return False


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _redactor(token):
    """Redact the key (raw, base64, json-escaped, URL-quoted forms) before anything is written."""
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
    for i, t in ((org, "<org>"), (ws, "<ws>")):
        if i:
            text = re.sub(r"(?<!\d)%s(?!\d)" % re.escape(i), t, text)
    return text


def _loads(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _idlike(kl):
    return kl == "id" or kl.endswith("id") or kl.endswith("ids")


SAFE_NUMBER_KEYS = ("duration", "duration_seconds", "per_page", "page", "limit", "count", "total")


def _scrub(obj, aliases, key="", known=()):
    """Keep structure, key names, small numbers, booleans, ISO timestamps; alias ids (any key named
    like an id) and ANY integer of 1000 or more, or equal to the org/workspace id, unless the key is a
    known duration/paging field; every other string becomes <text>."""
    kl = key.lower()
    if isinstance(obj, dict):
        return {k: _scrub(v, aliases, k, known) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, aliases, key, known) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if _idlike(kl) and (isinstance(obj, str) or _num(obj)):
        return aliases.setdefault(str(obj), 1001 + len(aliases))
    if _num(obj) and kl not in SAFE_NUMBER_KEYS and (
            str(obj) in known or (isinstance(obj, int) and abs(obj) >= 1000)):
        return aliases.setdefault(str(obj), 1001 + len(aliases))
    if _num(obj):
        return obj
    if isinstance(obj, str):
        return obj if _ISO.match(obj) else "<text>"
    return "<text>"


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _entry(parsed):
    """The entry object of a reply: the bare dict, or one under "data". Else None."""
    if isinstance(parsed, dict):
        if isinstance(parsed.get("data"), dict):
            return parsed["data"]
        return parsed
    return None


def _entry_id(parsed):
    e = _entry(parsed)
    if not e:
        return None
    v = e.get("id")
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None
    return str(v) if re.fullmatch(r"\d+", str(v)) else None


def _items(parsed):
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in sorted(parsed):
            if isinstance(parsed[k], list):
                return parsed[k]
    return []


def main(argv=None, http=None, out=print, now=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    ap.add_argument("--record", required=True, metavar="DIR")
    ap.add_argument("--org", default="")
    ap.add_argument("--workspace", default="")
    ap.add_argument("--project-name", default="")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    http = http or _default_http

    def clock():
        d = now if now is not None else datetime.now(timezone.utc)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)

    token = os.environ.get("TOGGL_API_TOKEN", "").strip()
    red = _redactor(token)

    def say(text, ids=True):
        text = red(text)
        out(_hide_ids(text, args.org, args.workspace) if ids else text)

    if not args.yes:
        say(PLAN)
        say("Run again with --yes to start the probe. No request was constructed.")
        return 2
    if not token:
        say("TOGGL_API_TOKEN is not set; no request made. Set it under your user environment "
            "variables, then open a new terminal.")
        return 2
    if not re.fullmatch(r"\d+", args.org) or not re.fullmatch(r"\d+", args.workspace):
        say("--org and --workspace are required and must be digits only; no request made.")
        return 2
    if _in_git_tree(args.record):
        say("refused: --record is inside a git work tree; pick a folder outside every repo")
        return 2
    try:
        Path(args.record).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        say("cannot create --record: " + type(exc).__name__)
        return 2

    base = "%sapi/organizations/%s/workspaces/%s" % (PREFIX, args.org, args.workspace)
    log = []
    st = {"n": 0, "halted": False}
    ids = []                  # entries this run created (ids the POSTs returned)
    m = {}                    # measurements
    flags = {"bad": False, "foreign": False, "unknown_entry": False,
             "delete_ok": False, "delete_tried": False}
    aliases = {}

    def call(name, method, url, body=None, force=False):
        if method not in ("GET", "POST", "DELETE") or not url.startswith(PREFIX):
            raise ValueError("refused: only GET/POST/DELETE on " + PREFIX)
        if st["halted"] and not force:
            raise _Abort("limit")
        if st["n"] >= MAX_REQUESTS:
            raise _Abort("request cap")
        st["n"] += 1
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        path = "/" + url[len(PREFIX):].split("?")[0]
        entry = {"name": name, "method": method, "url": url, "path": path, "request": body,
                 "status": None, "headers": {}, "body": ""}
        log.append(entry)
        try:
            status, rh, raw = http(method, url, headers=headers, json=body, timeout=20)
        except BaseException as exc:
            if name.startswith("start"):
                flags["unknown_entry"] = True
            if not isinstance(exc, Exception):
                raise
            say("%s %s -> request failed: %s" % (method, path, type(exc).__name__))
            flags["bad"] = True
            return None, {}, None
        raw = raw if isinstance(raw, bytes) else b""
        rh = rh or {}
        entry["status"] = status
        extra = {k: v for k, v in rh.items() if any(w in k.lower() for w in ("quota", "rate", "retry"))}
        entry["headers"] = extra
        entry["body"] = red(raw.decode("utf-8", "replace"))
        parsed = _loads(raw)
        entry["parsed"] = parsed
        if parsed is None:
            shape = "(no JSON body)"
        elif isinstance(parsed, dict):
            shape = ", ".join(sorted(parsed)) if parsed else "(empty object)"
        elif isinstance(parsed, list):
            shape = "list of %d" % len(parsed)
        else:
            shape = type(parsed).__name__
        hdr = "".join("; %s=%s" % (k, v) for k, v in extra.items())
        say("%s %s -> %s; keys: %s%s" % (method, path, status, shape, hdr))
        if status in (402, 429):
            st["halted"] = True
            flags["bad"] = True
            say("Stopped: HTTP %s (quota/rate limit)." % status)
            if not force:
                raise _Abort("limit")
        return status, rh, parsed

    def ok(status):
        return isinstance(status, int) and 200 <= status < 300

    def refusal(name, status, parsed):
        """Report why a start was refused: the reply's own reason, sanitized; keys only, no values."""
        if not isinstance(status, int):
            m[name] = "request failed (no reply)"
            return
        reason = None
        if isinstance(parsed, dict):
            for k in ("error_description", "error"):
                if isinstance(parsed.get(k), str) and parsed[k]:
                    reason = parsed[k]
                    break
        reason = red(reason if reason is not None else "no reason in reply")[:160]
        reason = re.sub(r"[^A-Za-z0-9 .,:;'_()<>=/-]", "?", reason)
        m[name] = "refused (HTTP %s): %s" % (status, reason)
        if isinstance(parsed, dict) and isinstance(parsed.get("error_values"), dict):
            m[name + " error_values keys"] = ", ".join(
                re.sub(r"[^A-Za-z0-9 .,:;'_()<>=/-]", "?", str(k)) for k in sorted(parsed["error_values"], key=str))

    def current():
        s, rh, p = call("current", "GET", base + "/tracking/current")
        cid = _entry_id(p) if s == 200 else None
        return s, rh, p, cid

    def stop():
        s, rh, p = call("stop", "POST", base + "/tracking/stop", {"end": _iso(clock())})
        return s, rh, p

    def adopt(p):
        """A `current` reply that shows a probe entry we do not hold an id for (a start whose outcome
        was unknown): take its id so it is stopped and deleted under the pre-stop rule."""
        e = _entry(p)
        i = _entry_id(p)
        if i and isinstance(e, dict) and e.get("description") in (DESC_A, DESC_B) and i not in ids:
            ids.append(i)
            flags["unknown_entry"] = False
            return True
        return False

    def drain(limit):
        """current -> stop, only while current shows one of our entries. Stops at idle or foreign."""
        stops = 0
        while stops < limit and st["n"] <= MAX_REQUESTS - 6 and not st["halted"]:
            s, rh, p, cid = current()
            if cid is None or cid not in ids:
                if cid is not None:
                    flags["foreign"] = True
                    say(FOREIGN_MSG)
                return
            s2, _, _ = stop()
            stops += 1
            if not ok(s2):
                flags["bad"] = True
                return

    def steps():
        # 1. nothing may be running
        s, rh, p, cid = current()
        if s == 200:
            say(RUNNING_MSG)
            raise _Exit(1)
        if s != 204:
            say("Unexpected reply to the first check; nothing was started.")
            raise _Exit(1)
        m["current idle"] = "204"
        q = next((v for k, v in rh.items() if k.lower() == "x-toggl-quota-remaining"), None)
        if q is None:
            m["quota header"] = "absent after step 1"
        else:
            m["quota header"] = "present"
            try:
                low = int(str(q).strip()) < MIN_QUOTA
            except ValueError:
                low = False
            if low:
                say(LOW_QUOTA_MSG % str(q).strip())
                raise _Exit(1)
        # 2. project
        project_id = None
        if args.project_name:
            s, _, p = call("projects", "GET", base + "/projects?per_page=100")
            want = args.project_name.strip().casefold()
            for pr in _items(p):
                if (isinstance(pr, dict) and pr.get("archived_at") is None
                        and str(pr.get("name", "")).strip().casefold() == want
                        and isinstance(pr.get("id"), (int, str)) and not isinstance(pr.get("id"), bool)):
                    project_id = pr["id"]
                    break
            if project_id is None:
                say("Project not found.")
                raise _Exit(1)
        # 3. start A
        body_a = {"description": DESC_A, "start": _iso(clock()), "type": START_TYPE}
        if project_id is not None:
            body_a["project_id"] = project_id
        s, _, p = call("start A", "POST", base + "/tracking/start", body_a)
        a = _entry_id(p) if ok(s) else None
        e = _entry(p) if ok(s) else None
        label = "start with a project" if project_id is not None else "start without a project"
        if ok(s):
            m[label] = "works"
        else:
            refusal("start A", s, p)
        if not ok(s):
            # unknown outcome (timeout, 5xx, refusal): cleanup looks at `current` for a probe entry
            flags["unknown_entry"] = True
            flags["bad"] = True
            raise _Abort("start A failed")
        if a is None:
            flags["unknown_entry"] = True
            flags["bad"] = True
            raise _Abort("no id")
        ids.append(a)
        if isinstance(e, dict):
            m["running entry has no duration key"] = "yes" if "duration" not in e else "no"
            m["project_id null"] = "yes" if e.get("project_id") is None else "no"
        # 4. current must be A
        s, _, p, cid = current()
        if cid != a:
            if cid is not None:
                flags["foreign"] = True
                say(FOREIGN_MSG)
            else:
                say("The running entry is not the one the probe started; going to cleanup.")
            flags["bad"] = True
            return
        # 5. start B while A runs
        body_b = {"description": DESC_B, "start": _iso(clock()), "type": START_TYPE}
        if project_id is not None:
            body_b["project_id"] = project_id
        s, _, p = call("start B", "POST", base + "/tracking/start", body_b)
        start_b_ok = ok(s)
        if not start_b_ok:
            refusal("start B", s, p)
        b = _entry_id(p) if start_b_ok else None
        if b is None:
            flags["unknown_entry"] = True     # unknown outcome until `current` says otherwise
            flags["bad"] = True
        if b is not None and b not in ids:
            ids.append(b)
        # 6. who runs now
        s, _, p, cid = current()
        if b is None and adopt(p):
            b = cid
        if not start_b_ok or b is None:
            m["start while running"] = "refused"
        elif cid == b:
            m["start while running"] = "replaced the timer"
        elif cid == a:
            m["start while running"] = "both running"
        else:
            m["start while running"] = "unclear"
        if cid is not None and cid not in ids:
            flags["foreign"] = True
            flags["bad"] = True
            say(FOREIGN_MSG)
            return
        # 7. stop (pre-stop rule: cid is A or B)
        if cid is not None:
            s, _, p = stop()
            m["stop body {\"end\"}"] = "accepted" if ok(s) else "refused"
            if not ok(s):
                flags["bad"] = True
                return
            e = _entry(p)
            if isinstance(e, dict):
                d = e.get("duration")
                m["stop reply"] = "keys: %s; duration: %s; end key: %s" % (
                    ", ".join(sorted(e)) or "(none)",
                    "absent" if "duration" not in e else type(d).__name__,
                    "yes" if any(k in e for k in ("end", "stop", "ended_at")) else "no")
            else:
                m["stop reply"] = "no entry object"
            if m["start while running"] != "replaced the timer":
                drain(MAX_REQUESTS)
                if flags["foreign"]:
                    return
        else:
            m["stop reply"] = "not sent: nothing was running"
        # 8. list
        t = clock()
        url = base + "/time-entries?date_from=%s&date_to=%s&include_taskless=true&per_page=100" % (
            urllib.parse.quote(_iso(t - timedelta(minutes=30)), safe=":"),
            urllib.parse.quote(_iso(t + timedelta(minutes=5)), safe=":"))
        s, _, p = call("list", "GET", url)
        if ok(s):
            for nm, i in (("A", a), ("B", b)):
                if i is None:
                    continue
                row = next((r for r in _items(p) if isinstance(r, dict) and str(r.get("id")) == i), None)
                if row is None:
                    m["entry %s in list" % nm] = "not found"
                elif isinstance(row.get("duration"), int) and not isinstance(row.get("duration"), bool):
                    m["entry %s in list" % nm] = "finished (integer duration)"
                else:
                    m["entry %s in list" % nm] = "still running (no duration)"
        else:
            flags["bad"] = True

    def cleanup():
        """Always: look for an unknown probe entry, bulk DELETE, stop only a timer that is ours,
        then RE-READ current and report that fresh value. Never raises Exception."""
        try:
            if flags["unknown_entry"] and not st["halted"] and st["n"] < MAX_REQUESTS:
                s, _, p, cid = current()
                adopt(p)
        except _Abort:
            pass
        except Exception:
            flags["bad"] = True
        if ids and not flags["delete_tried"]:
            flags["delete_tried"] = True
            try:
                s, _, _ = call("delete", "DELETE", base + "/time-entries/bulk?ids=" + ",".join(ids),
                               force=True)
                flags["delete_ok"] = ok(s)
                if ok(s):
                    m["delete"] = "status %s" % s
                else:
                    m["delete"] = "refused (HTTP %s) - delete the two probe entries by hand in Focus" % s
            except _Abort:
                m["delete"] = "not sent"
            except Exception:
                m["delete"] = "failed"
        if not flags["delete_ok"] and ids:
            flags["bad"] = True
        if st["halted"] or st["n"] >= MAX_REQUESTS:
            flags["bad"] = True      # could not confirm that nothing is left running
            m["current idle"] = "not checked (request cap or limit)"
            return
        try:
            s, _, p, cid = current()
            if cid is not None and cid in ids:
                flags["bad"] = True
                if st["n"] >= MAX_REQUESTS:
                    m["current idle"] = "not re-read (request cap)"
                    return
                s2, _, _ = stop()
                m["final stop"] = "accepted" if ok(s2) else "refused"
                if not ok(s2):
                    flags["bad"] = True
                if st["n"] >= MAX_REQUESTS:
                    m["current idle"] = "not re-read (request cap)"
                    return
                s, _, p, cid = current()
            m["current idle"] = "204" if s == 204 else "other (status %s)" % s
            if s != 204:
                flags["bad"] = True
            if cid is not None and cid not in ids:
                flags["foreign"] = True
                say(FOREIGN_MSG)
        except _Abort:
            flags["bad"] = True
        except Exception:
            flags["bad"] = True

    def finish(code):
        if log:
            try:
                stamp = clock().strftime("%Y%m%dT%H%M%SZ")
                base_name = "toggl_focus_probe_" + stamp
                raw_doc = [{"name": e["name"], "method": e["method"], "url": red(e["url"]),
                            "request": e["request"], "status": e["status"], "headers": e["headers"],
                            "body": e["body"]} for e in log]
                scrub_doc = []
                known = {args.org, args.workspace}
                for e in log:
                    hdrs = {k: (v if re.fullmatch(r"\d+", str(v)) else "<text>")
                            for k, v in e["headers"].items()}
                    scrub_doc.append({"name": e["name"], "method": e["method"],
                                      "path": _hide_ids(e["path"], args.org, args.workspace),
                                      "status": e["status"], "headers": hdrs,
                                      "request": _scrub(e["request"], aliases, known=known),
                                      "reply": _scrub(e.get("parsed"), aliases, known=known)})
                _write_new(args.record, base_name, ".json",
                           red(json.dumps(raw_doc, indent=1)).encode("utf-8"))
                _write_new(args.record, base_name, ".scrubbed.json",
                           red(json.dumps(scrub_doc, indent=1)).encode("utf-8"))
            except Exception as exc:
                say("could not write the record files: " + type(exc).__name__)
        say("")
        say("Report (shape only; aliases, no names or ids):")
        for k, v in m.items():
            say("  %s: %s" % (k, v))
        say("  stop when idle: not tested (a stop that could end your own timer is never sent)")
        say("  requests sent: %d of %d" % (st["n"], MAX_REQUESTS))
        if code is None:
            code = 1 if (flags["bad"] or flags["foreign"] or flags["unknown_entry"]) else 0
        if flags["unknown_entry"] or (ids and not flags["delete_ok"]):
            say(MAY_EXIST_MSG)
            code = 1
        say(CLOSING)
        return code

    code = None
    pending = None
    try:
        steps()
    except _Abort:
        flags["bad"] = True
    except _Exit as ex:
        code = ex.code
    except BaseException as exc:        # KeyboardInterrupt and anything unexpected
        pending = exc
        flags["bad"] = True
        say("interrupted (%s); cleaning up" % type(exc).__name__)
    finally:
        if ids or flags["unknown_entry"]:
            cleanup()
        code = finish(code)
    if pending is not None:
        raise pending
    return code


if __name__ == "__main__":
    sys.exit(main())
