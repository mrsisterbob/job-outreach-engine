"""Build the money snapshot from Kevin's Actual budget and post it to the engine.

  python scripts/push_actual_snapshot.py [PATH] [--month YYYY-MM] [--dry-run] [--url URL]

PATH is a budget folder, a .zip export/backup or a db.sqlite. Omitted: the one budget folder under
~/OneDrive/Documents/Actual/*/ or ~/Documents/Actual/*/ that holds a db.sqlite.
--dry-run prints the summary and sends nothing. Otherwise the URL comes from --url or ENGINE_URL and
the token from SNAPSHOT_INGEST_TOKEN only (never an argument, never printed, never written).
Only https:// (or http://localhost / 127.0.0.1) URLs are accepted. This script writes no files.
Exit: 0 sent (or dry run), 1 refused/failed, 2 bad usage or input.
"""
import argparse
import os
import re
import sqlite3
import sys
import zipfile
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import actual_snapshot  # noqa: E402

STALE_DAYS = 7
REPLY_TEXT = {
    403: "The engine refused the token (403). Check SNAPSHOT_INGEST_TOKEN matches the server's.",
    503: "The engine has no ingest token configured (503). Set SNAPSHOT_INGEST_TOKEN on the server.",
    413: "The engine says the snapshot is too large (413).",
}


def _default_post(*args, **kw):
    import requests
    return requests.post(*args, **kw)


def find_budget_folders(home=None):
    home = Path(home or os.path.expanduser("~"))
    found = []
    for base in (home / "OneDrive" / "Documents" / "Actual", home / "Documents" / "Actual"):
        if base.is_dir():
            found += sorted(p for p in base.iterdir() if p.is_dir() and (p / "db.sqlite").is_file())
    return found


def url_ok(url):
    """https://host[:port] (or http://localhost / 127.0.0.1); no userinfo, path, query or fragment."""
    try:
        u = urlparse(url)
        u.port
    except ValueError:
        return False
    if u.username is not None or u.password is not None or u.query or u.fragment or u.params:
        return False
    if u.path not in ("", "/") or "@" in u.netloc:
        return False
    if u.scheme == "https" and u.hostname:
        return True
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")


def _clean(text, limit=200):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(text))[:limit]


def summary_lines(snapshot, report):
    m = snapshot["money"]
    out = [f"Period: {m['period']}   as_of: {m['as_of']}", "", "category | bucket | amount"]
    for r in sorted(m["flow"], key=lambda r: -r["amount"]):
        out.append(f"{r['category']} | {r['bucket']} | {r['amount']:.2f}")
    if not m["flow"]:
        out.append("(no spending categories)")
    out.append("")
    if m["off_budget"]:
        out.append("Off-budget:")
        out += [f"  {a['name']} | {a['balance']:.2f}" for a in m["off_budget"]]
    else:
        out.append("Off-budget: none - no off-budget accounts in this file")
    excluded = sum(v for k, v in report.items()
                   if k.startswith("excluded_") and isinstance(v, int) and not isinstance(v, bool))
    out += ["", f"Excluded rows (total): {excluded}"]
    out += [f"  {k}: {v}" for k, v in sorted(report.items())
            if k.startswith("excluded_") and isinstance(v, int) and v]
    out += [f"Other non-zero counts: "
            + ", ".join(f"{k}={v}" for k, v in sorted(report.items())
                        if not k.startswith("excluded_") and isinstance(v, int)
                        and not isinstance(v, bool) and v
                        and k != "categories_omitted_net_refund") or "none"]
    out += [
            f"Omitted net-refund categories: {report.get('categories_omitted_net_refund', 0)}",
            f"Newest transaction date: {report.get('newest_txn_date')}",
            f"as_of: {m['as_of']}"]
    newest = report.get("newest_txn_date")
    if newest:
        try:
            gap = (date.fromisoformat(m["as_of"]) - date.fromisoformat(newest)).days
        except (ValueError, TypeError):
            gap = 0
            out.append("Could not compare the last transaction date with as_of - check the file's dates.")
        if gap > STALE_DAYS:
            out.append(f"Last transaction in this file is {gap} days older than the file itself - "
                       f"the page will say as-of {m['as_of']}.")
    return out


def push(snapshot, url, token, post=_default_post):
    return post(url.rstrip("/") + "/snapshots/ingest", json=snapshot,
                headers={"X-Snapshot-Token": token}, timeout=20, allow_redirects=False)


def main(argv=None, post=None):
    ap = argparse.ArgumentParser(description="Push the Actual money snapshot to the engine.")
    ap.add_argument("path", nargs="?")
    ap.add_argument("--month")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--url")
    args = ap.parse_args(argv)

    path = args.path
    if not path:
        folders = find_budget_folders()
        if len(folders) != 1:
            names = ", ".join(f.name for f in folders) or "none"
            print(f"Expected exactly one Actual budget folder, found {len(folders)}: {names}. "
                  "Pass PATH.")
            return 2
        path = folders[0]
    try:
        snapshot, report = actual_snapshot.build_snapshot(path, month=args.month)
    except actual_snapshot.ExportError as e:
        print(f"Cannot build snapshot: {_clean(e, 400)}")
        return 2
    except (sqlite3.Error, OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as e:
        print(f"Cannot read the budget file: {type(e).__name__}")
        return 2

    for line in summary_lines(snapshot, report):
        print(line)
    if args.dry_run:
        if not snapshot["money"]["flow"] and not snapshot["money"]["off_budget"]:
            print("a real run would refuse: nothing to send")
        print("Nothing was sent.")
        return 0

    url = args.url or os.environ.get("ENGINE_URL", "")
    token = os.environ.get("SNAPSHOT_INGEST_TOKEN", "")
    if not url:
        print("No engine URL: pass --url or set ENGINE_URL. Nothing was sent.")
        return 2
    if not token:
        print("No token: set SNAPSHOT_INGEST_TOKEN. Nothing was sent.")
        return 2
    if not url_ok(url):
        print("Refusing the URL: it must be https:// (or http://localhost). Nothing was sent.")
        return 2
    m = snapshot["money"]
    if not m["flow"] and not m["off_budget"]:
        print(f"Nothing to send for {m['period']}.")
        return 1

    try:
        resp = push(snapshot, url, token, post=post or _default_post)
    except Exception as e:
        print(f"Send failed: {type(e).__name__}")
        return 1
    code = resp.status_code
    print(f"HTTP {code}")
    if code == 200:
        try:
            print(f"received_at: {_clean(resp.json().get('received_at'), 64)}")
        except Exception:
            print("Unexpected reply body: the engine said 200 but sent no readable received_at.")
        return 0
    if code in REPLY_TEXT:
        print(REPLY_TEXT[code])
    elif code == 400:
        try:
            print(f"The engine rejected the snapshot: {_clean(resp.json().get('error'))}")
        except Exception:
            print("The engine rejected the snapshot (400).")
    else:
        print("Unexpected reply from the engine.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
