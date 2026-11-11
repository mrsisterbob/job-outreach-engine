"""Copy the last days of Toggl Focus entries into ActivityWatch's stopwatch bucket.

  python scripts/toggl_to_activitywatch.py --org ORG --workspace WS [--days 7]
         [--aw-url http://localhost:5600] [--bucket aw-stopwatch] [--dry-run]

Toggl side: read-only, 2 requests normally, never more than 5. The key comes from the user
environment variable TOGGL_API_TOKEN only and is never printed. Every ActivityWatch call goes
through local_http (loopback only, no proxy, no redirects). The bucket is never created: open
ActivityWatch's Stopwatch page once first. The logic lives in toggl_aw_sync; this file is I/O.
Exit: 0 done, 1 refused or failed, 2 usage or no token, 3 zero entries.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import activity_snapshot as act  # noqa: E402
import local_http  # noqa: E402
import toggl_aw_sync as sync  # noqa: E402
from toggl_snapshot import TogglReplyError, check_reply  # noqa: E402

PREFIX = "https://focus.toggl.com/"
MAX_TOGGL_REQUESTS = 5
PAGE = 100
MAX_DELETES = 200
TRUNCATED = ("The Toggl window is larger than this script reads in one run. Nothing was written "
             "and nothing was deleted. Try a smaller --days.")
STOPWATCH_TYPE = "general.stopwatch"
TOKEN_HINT = ("No Toggl key: set TOGGL_API_TOKEN in your user environment variables, then open "
              "a new PowerShell. Nothing was requested.")
NO_BUCKET = ('The ActivityWatch bucket "{b}" was not found (or is not a stopwatch bucket). Open '
             "ActivityWatch, go to the Stopwatch page once, then run this again. Nothing was written.")


def _default_http(method, url, headers=None, json=None, timeout=20):
    import requests
    r = requests.request(method, url, headers=headers, json=json, timeout=timeout,
                         allow_redirects=False)
    return r.status_code, dict(r.headers), r.content


def _aw_get_default(url, **kw):
    return local_http.local_get(url, **kw)


def _aw_post_default(url, **kw):
    return local_http.local_post(url, **kw)


def _aw_delete_default(url, **kw):
    return local_http.local_delete(url, **kw)


def _iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aw_url_ok(url):
    try:
        local_http._check_url(url)
        p = urlsplit(url)
        return p.path in ("", "/") and not p.query and not p.fragment             and "?" not in url and "#" not in url
    except ValueError:
        return False


class _Stop(Exception):
    def __init__(self, code):
        self.code = code


def _fetch_toggl(http, token, base, start, end):
    """Return (projects_reply, entries_reply). Raises TogglReplyError; at most 5 requests."""
    state = {"n": 0}
    headers = {"Authorization": "Bearer " + token}

    def get(url):
        if not url.startswith(PREFIX):
            raise TogglReplyError("Refusing a URL outside focus.toggl.com.")
        if state["n"] >= MAX_TOGGL_REQUESTS:
            raise TogglReplyError("Toggl request budget used.")
        state["n"] += 1
        status, rh, raw = http("GET", url, headers=headers, json=None, timeout=20)
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
        check_reply(status, text, rh)
        try:
            return json.loads(text)
        except ValueError:
            raise TogglReplyError("A Toggl reply was not JSON. Nothing was written.") from None

    projects = get(base + "projects?per_page=%d" % PAGE)
    if len(sync._unwrap(projects, "projects")) >= PAGE:
        raise TogglReplyError(TRUNCATED)
    q = urlencode({"date_from": _iso_z(start), "date_to": _iso_z(end),
                   "include_taskless": "true", "per_page": PAGE})
    merged, seen = [], set()

    def take(rows):
        fresh = 0
        for e in rows:
            key = e.get("id") if isinstance(e, dict) else None
            if isinstance(key, bool) or not isinstance(key, (int, str)):
                key = None
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            merged.append(e)
            fresh += 1
        return fresh

    rows = sync._unwrap(get(base + "time-entries?" + q), "time entries")
    take(rows)
    page = 1
    while len(rows) >= PAGE:
        if state["n"] >= MAX_TOGGL_REQUESTS:
            raise TogglReplyError(TRUNCATED)
        page += 1
        rows = sync._unwrap(get(base + "time-entries?" + q + "&page=%d" % page), "time entries")
        if take(rows) == 0:
            break
    return projects, {"data": merged}


def _aw_json(resp):
    try:
        return resp.json()
    except Exception:
        raise _Stop(1) from None


def main(argv=None, toggl_http=None, aw_get=None, aw_post=None, aw_delete=None,
         now=None, out=print):
    ap = argparse.ArgumentParser(description="Copy Toggl Focus entries into ActivityWatch.")
    ap.add_argument("--org", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--days", default="7")
    ap.add_argument("--aw-url", default="http://localhost:5600")
    ap.add_argument("--bucket", default="aw-stopwatch")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not (re.fullmatch(r"[0-9]+", args.org) and re.fullmatch(r"[0-9]+", args.workspace)):
        out("--org and --workspace must be digits only. Nothing was requested.")
        return 2
    if not re.fullmatch(r"[0-9]{1,3}", args.days) or not 1 <= int(args.days) <= 31:
        out("--days must be a whole number from 1 to 31. Nothing was requested.")
        return 2
    days = int(args.days)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", args.bucket):
        out("--bucket has characters this script does not accept. Nothing was requested.")
        return 2
    if not _aw_url_ok(args.aw_url):
        out("Refusing --aw-url: ActivityWatch has no login, so only http://localhost, "
            "127.0.0.1 or [::1] is accepted. Nothing was requested.")
        return 2
    token = os.environ.get("TOGGL_API_TOKEN", "").strip()
    if not token:
        out(TOKEN_HINT)
        return 2
    tz, tz_name = act._zone()
    if tz_name != act._TZ_NAME:
        out("Timezone data missing, so dates are shown in UTC (install tzdata). "
            "Nothing was requested.")
        return 1

    toggl_http = toggl_http or _default_http
    aw_get = aw_get or _aw_get_default
    aw_post = aw_post or _aw_post_default
    aw_delete = aw_delete or _aw_delete_default
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(tz)
    start = datetime.combine(local_now.date() - timedelta(days=days - 1), time(0), tzinfo=tz)
    start = start.astimezone(timezone.utc)
    end = now + timedelta(days=1)
    aw = args.aw_url.rstrip("/")
    bucket_url = aw + "/api/0/buckets/" + quote(args.bucket, safe="")

    base = "%sapi/organizations/%s/workspaces/%s/" % (PREFIX, args.org, args.workspace)
    try:
        projects, entries = _fetch_toggl(toggl_http, token, base, start, end)
    except TogglReplyError as e:
        out(str(e))
        return 1
    except Exception as e:
        out(f"Toggl request failed: {type(e).__name__}")
        return 1
    if not entries["data"]:
        out(f"Toggl returned 0 entries for the last {days} days. Nothing was written and "
            "nothing was deleted.")
        return 3
    try:
        events, rep = sync.build_events(entries, projects, start, end, now)
    except TogglReplyError as e:
        out(str(e))
        return 1
    if not events:
        out(f"Toggl returned no readable entries inside the last {days} days. Nothing was "
            "written and nothing was deleted.")
        return 3

    try:
        return _run_aw(args, bucket_url, events, rep, start, end, tz,
                       aw_get, aw_post, aw_delete, out)
    except _Stop as s:
        return s.code
    except Exception as e:
        out(f"ActivityWatch request failed: {type(e).__name__}. Is it running (tray icon)?")
        return 1


def _run_aw(args, bucket_url, events, rep, start, end, tz, aw_get, aw_post, aw_delete, out):
    def status_fail(resp, what):
        out(f"ActivityWatch answered HTTP {resp.status_code} to {what}.")
        raise _Stop(1)

    resp = aw_get(bucket_url + "/", timeout=10)
    if resp.status_code == 404:
        out(NO_BUCKET.format(b=args.bucket))
        return 1
    if resp.status_code != 200:
        status_fail(resp, "the bucket check")
    info = _aw_json(resp)
    if not isinstance(info, dict) or info.get("type") != STOPWATCH_TYPE:
        out(NO_BUCKET.format(b=args.bucket))
        return 1
    q = urlencode({"start": _iso_z(start), "end": _iso_z(end), "limit": -1})
    resp = aw_get(bucket_url + "/events?" + q, timeout=30)
    if resp.status_code != 200:
        status_fail(resp, "the events query")
    existing = _aw_json(resp)
    if not isinstance(existing, list):
        out("ActivityWatch answered with a reply this script cannot read.")
        return 1

    plan = sync.plan_sync(events, existing, start, protected=rep["unreadable_ids"])
    ins_tids = {w["data"]["toggl_id"] for w in plan["insert"]}
    old_tid = {ev.get("id"): sync._ours(ev) for ev in existing if isinstance(ev, dict)}
    present = {t for t in old_tid.values() if t}
    replaced = sum(1 for t in ins_tids if t in present)
    inserted = len(plan["insert"]) - replaced
    deleted = sum(1 for i in plan["delete"] if old_tid.get(i) not in ins_tids)
    if len(plan["delete"]) > MAX_DELETES:
        out(f"Refusing to delete {len(plan['delete'])} events at once.")
        return 1

    out("Window: %s to %s (%s)" % (start.astimezone(tz).date().isoformat(),
                                    end.astimezone(tz).date().isoformat(), act._TZ_NAME))
    out(f"Toggl: {rep['entries']} entries, {rep['running']} running, "
        f"{rep['unreadable']} skipped as unreadable")
    if args.dry_run:
        out(f"DRY RUN: would insert {inserted}, replace {replaced}, delete {deleted}, "
            f"unchanged {plan['keep']}")
    else:
        if plan["insert"]:
            r = aw_post(bucket_url + "/events", json=plan["insert"], timeout=30)
            if not 200 <= r.status_code < 300:
                status_fail(r, "the insert")
        for i in plan["delete"]:
            r = aw_delete(bucket_url + "/events/" + quote(str(i), safe=""), timeout=30)
            if r.status_code != 404 and not 200 <= r.status_code < 300:
                status_fail(r, "a delete")
        out(f"ActivityWatch: inserted {inserted}, replaced {replaced}, deleted {deleted}, "
            f"unchanged {plan['keep']}")
    totals = {}
    for ev in events:
        lab = ev["data"]["label"]
        totals[lab] = totals.get(lab, 0.0) + ev["duration"]
    for lab, secs in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])):
        out(f"{lab}: {secs / 3600:.2f} h")
    return 0


if __name__ == "__main__":
    sys.exit(main())
