"""Read the week from the local ActivityWatch and push category hours to the engine.

  python scripts/push_time_snapshot.py [--week current|last] [--map PATH] [--aw-url URL]
                                       [--dry-run] [--url URL]

--dry-run shows the week and sends nothing. Otherwise the engine URL comes from --url or ENGINE_URL
and the token from SNAPSHOT_INGEST_TOKEN only (never an argument, never printed). The pushed body
is {"time": section}: category hours only, no app names or window titles. Writes no files.
Exit: 0 sent (or dry run), 1 refused/failed, 2 bad usage or input.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import activity_snapshot as act  # noqa: E402
import push_actual_snapshot as pas  # noqa: E402
import local_http  # noqa: E402

CAVEAT = ("ActivityWatch only sees this PC while it is on and you are at it. Sleep, gym, meals "
          "and time away are not in these hours - log them with /life log.")
LAST_WEEK_NOTE = ("The finance page shows the newest snapshot. Push the current week again "
                  "afterwards or it will show last week.")


def _aw_post_default(url, **kw):
    return local_http.local_post(url, **kw)


def _aw_get_default(url, **kw):
    return local_http.local_get(url, **kw)


def aw_url_ok(url):
    """Same-machine http only: reuses the engine URL check with a loopback-only host list."""
    return pas.url_ok(url, http_hosts=("localhost", "127.0.0.1", "::1"), allow_https=False)


def _parse_z(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _is_timeout(e):
    if isinstance(e, TimeoutError):
        return True
    try:
        import requests
        return isinstance(e, requests.exceptions.Timeout)
    except Exception:
        return False


def _is_conn_error(e):
    if isinstance(e, ConnectionError):
        return True
    try:
        import requests
        return isinstance(e, requests.exceptions.ConnectionError)
    except Exception:
        return False


_FRACTION = re.compile(r"\.(\d+)")


def _parse_created(text):
    """ISO timestamp -> aware datetime on any Python 3.x (fractions normalised to 6 digits,
    trailing Z turned into +00:00). Raises ValueError when unreadable."""
    t = text.strip()
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    t = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], t, count=1)
    dt = datetime.fromisoformat(t)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _earliest_created(buckets):
    """(earliest `created` among afk/window buckets or None, number of unreadable dates).
    Reads only id and created."""
    best, bad = None, 0
    if not isinstance(buckets, dict):
        return None, 1
    for bid, info in buckets.items():
        if not isinstance(bid, str) or not bid.startswith(("aw-watcher-afk", "aw-watcher-window")):
            continue
        created = info.get("created") if isinstance(info, dict) else None
        try:
            dt = _parse_created(created)
        except (ValueError, AttributeError, TypeError):
            bad += 1
            continue
        if best is None or dt < best:
            best = dt
    return best, bad


def _local_date(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Detroit")).date().isoformat()
    except Exception:
        return dt.astimezone(timezone.utc).date().isoformat()


def _tz_available():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/Detroit")
        return True
    except Exception:
        return False


def _load_config(map_path, out):
    """Return (buckets, app_buckets, source label) or None after printing problems."""
    if map_path:
        path = Path(map_path)
    else:
        path = Path.home() / "time_categories.json"
        if not path.is_file():
            return None, None, "defaults"
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        out(f"Cannot read the category map {pas.clean_text(path.name, 80)}: {type(e).__name__}")
        return False
    try:
        buckets, apps = act.parse_category_config(obj)
    except ValueError as e:
        out(f"The category map {pas.clean_text(path.name, 80)} has problems:")
        for line in str(e).splitlines():
            out(f"  {pas.clean_text(line, 200)}")
        return False
    return buckets, apps, pas.clean_text(path.name, 80)


def main(argv=None, aw_post=None, aw_get=None, post=None, now=None, out=print):
    ap = argparse.ArgumentParser(description="Push the ActivityWatch week to the engine.")
    ap.add_argument("--week", choices=["current", "last"], default="current")
    ap.add_argument("--map")
    ap.add_argument("--aw-url", default="http://localhost:5600")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--url")
    args = ap.parse_args(argv)

    aw_post = aw_post or _aw_post_default
    aw_get = aw_get or _aw_get_default
    if not aw_url_ok(args.aw_url):
        out("Refusing --aw-url: ActivityWatch has no login, so only http://localhost, "
            "127.0.0.1 or [::1] is accepted. Nothing was fetched.")
        return 2
    aw = args.aw_url.rstrip("/")

    cfg = _load_config(args.map, out)
    if cfg is False:
        out("Nothing was fetched or sent.")
        return 2
    buckets, app_buckets, source = cfg

    now = now or datetime.now(timezone.utc)
    bounds = act.week_bounds(now)
    section_now = now
    if args.week == "last":
        prev = _parse_z(bounds["start"]) - timedelta(hours=1)
        bounds = act.week_bounds(prev)
        section_now = _parse_z(bounds["end"]) - timedelta(hours=1)

    body = act.build_query_body(bounds["start"], bounds["end"])
    try:
        resp = aw_post(aw + "/api/0/query/", json=body, timeout=30)
    except Exception as e:
        if _is_conn_error(e):
            out(f"ActivityWatch is not answering at {aw}. Is it running (tray icon)?")
            return 2
        if _is_timeout(e):
            out(f"ActivityWatch took too long to answer at {aw}. Try again in a minute.")
            return 1
        out(f"ActivityWatch query failed: {type(e).__name__}")
        return 1
    if resp.status_code != 200:
        out(f"ActivityWatch answered HTTP {resp.status_code} to the query.")
        return 1
    try:
        result = resp.json()
    except Exception:
        out(f"ActivityWatch answered HTTP {resp.status_code} but not with JSON.")
        return 1
    if not isinstance(result, list):
        out(f"ActivityWatch answered HTTP {resp.status_code} with a reply this script cannot read.")
        return 1
    app_seconds, unreadable = act.app_seconds_from_query(result)

    try:
        section = act.build_time_section(app_seconds, section_now, buckets, app_buckets)
        desc = act.describe_week(app_seconds, section, unreadable, buckets, app_buckets)
    except Exception as e:
        out(f"Cannot build the snapshot: {type(e).__name__}")
        return 2

    partial = None
    try:
        bresp = aw_get(aw + "/api/0/buckets/", timeout=10)
        if bresp.status_code == 200:
            earliest, bad = _earliest_created(bresp.json())
            if earliest is None and bad:
                partial = False
            elif earliest is not None and earliest > _parse_z(bounds["start"]):
                partial = _local_date(earliest)
    except Exception:
        pass

    if not _tz_available():
        out("Timezone data missing, so dates are shown in UTC (install tzdata).")
    out(f"Week of {section['week_of']} to {_local_date(_parse_z(bounds['end']) - timedelta(hours=1))}"
        f"   (categories from {source})")
    if partial is False:
        out("Could not read bucket start dates, cannot tell if the week is partial.")
    elif partial:
        out(f"ActivityWatch only started on {partial}, so this week is partial.")
    out(f"Tracked: {desc['tracked_hours']} h of 168")
    out("")
    out("category | hours")
    for r in sorted(section["actual"], key=lambda r: -r["hours"]):
        out(f"{r['category']} | {r['hours']:.2f}")
    if not section["actual"]:
        out("(nothing tracked)")
    if desc["ignored_hours"]:
        out(f"Ignored (lock screen etc.): {desc['ignored_hours']} h")
    if desc["apps_unmapped"]:
        out("")
        out("Unmapped apps (shown here only, never sent):")
        for a in desc["apps_unmapped"]:
            out(f"  {pas.clean_text(a['app'], 60)} | {a['hours']:.2f}")
    if desc["unreadable"]:
        out(f"Unreadable rows skipped: {desc['unreadable']}")
    out("")
    out(CAVEAT)
    if args.week == "last":
        out(LAST_WEEK_NOTE)

    if args.dry_run:
        out("Nothing was sent.")
        return 0

    url = args.url or os.environ.get("ENGINE_URL", "")
    token = os.environ.get("SNAPSHOT_INGEST_TOKEN", "")
    if not url:
        out("No engine URL: pass --url or set ENGINE_URL. Nothing was sent.")
        return 2
    if not token:
        out("No token: set SNAPSHOT_INGEST_TOKEN. Nothing was sent.")
        return 2
    if not pas.url_ok(url):
        out("Refusing the URL: it must be https:// (or http://localhost). Nothing was sent.")
        return 2

    if not section["actual"]:
        out(f"Nothing tracked for the week of {section['week_of']}.")
        return 1

    try:
        resp = pas.push({"time": section}, url, token, post=post or pas.default_post)
    except Exception as e:
        out(f"Send failed: {type(e).__name__}")
        return 1
    return pas.report_reply(resp, out=out)


if __name__ == "__main__":
    sys.exit(main())
