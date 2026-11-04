"""Read-only: GET the Apify account's usage/limits replies, save them, print what they contain.

  python scripts/measure_apify_usage.py --record DIR [--url https://api.apify.com/...]

--record DIR must be outside the repo. Both requests are GETs and spend nothing. Without APIFY_TOKEN
nothing is requested (exit 2). The token comes from APIFY_TOKEN only and is never printed or saved.
Writes DIR/<slug>_<UTC stamp>.json (raw bytes) and .scrubbed.json (safe to commit).
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import apify_feed  # noqa: E402
import apify_usage_report as aur  # noqa: E402

# UNVERIFIED: recalled from Apify's API docs, not tested by us. A 404 or an odd shape is reported as such.
CANDIDATE_ENDPOINTS = [
    "https://api.apify.com/v2/users/me/limits",
    "https://api.apify.com/v2/users/me/usage/monthly",
]
PREFIX = "https://api.apify.com/"
CLOSING = ("Compare the money candidates with the Apify console Billing page and write both numbers "
           "in the Log; a field only counts as the monthly figure once they match.")


def _default_get(url, **kw):
    import requests
    return requests.get(url, **kw)


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _slug(url):
    s = re.sub(r"[^A-Za-z0-9]+", "-", url[len(PREFIX):].split("?")[0]).strip("-")
    return s or "root"


def _redact(text, token):
    text = re.sub(r"(?i)(token=)[^&\s#]*", r"\1***", str(text))
    return text.replace(token, "***") if token else text


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


def run(argv, get=None, out=print, root=ROOT, environ=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--record", required=True, metavar="DIR")
    ap.add_argument("--url", action="append", default=[], metavar="URL")
    args = ap.parse_args(argv)
    get = get or _default_get
    environ = os.environ if environ is None else environ

    if _inside(args.record, root):
        out("refused: --record %s is inside the repo; pick a folder outside it" % args.record)
        return 2
    for u in args.url:
        if not u.startswith(PREFIX):
            out("refused: --url must start with " + PREFIX)
            return 2
    token = apify_feed.get_token(environ)
    raw_out = out

    def out(text):
        raw_out(_redact(text, token))

    if not token:
        out("APIFY_TOKEN is not set; no request made")
        return 2
    urls = []
    for u in list(CANDIDATE_ENDPOINTS) + args.url:
        if u not in urls:
            urls.append(u)
    try:
        Path(args.record).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        out("cannot create --record: " + type(exc).__name__)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = {}
    answered = 0
    for url in urls:
        res = {"status": None, "error": None, "json": False, "body": None}
        results[_redact(url, token)] = res
        try:
            resp = get(url, headers={"Authorization": "Bearer " + token}, timeout=20,
                       allow_redirects=False)
            res["status"] = resp.status_code
            raw = resp.content if isinstance(resp.content, bytes) else b""
        except Exception as exc:
            res["error"] = type(exc).__name__
            continue
        base = "%s_%s" % (_slug(url), stamp)
        try:
            out("raw reply saved: %s" % _write_new(args.record, base, ".json", raw))
        except Exception as exc:
            out("could not save the raw reply: " + type(exc).__name__)
        try:
            res["body"] = json.loads(raw.decode("utf-8"))
            res["json"] = True
        except Exception:
            continue
        if not 200 <= res["status"] < 300:
            continue
        try:
            safe = json.dumps(aur.scrub(res["body"]), indent=2).replace(token, "<token>")
            out("scrubbed copy saved: %s" % _write_new(args.record, base, ".scrubbed.json",
                                                      safe.encode("utf-8")))
        except Exception as exc:
            out("could not save the scrubbed copy: " + type(exc).__name__)
        answered += 1
    for line in aur.render_report(results):
        out(line)
    out(CLOSING)
    return 0 if answered else 1


def main():
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
