"""ONE paid call to the Apify actor glitchbound/ats-jobs-scraper; save the raw reply, print a census.

  python scripts/measure_ats_resolver.py --input-json FILE --record DIR --confirm-spend

--input-json is sent VERBATIM (Kevin writes it from the actor's Input tab; no key is added here).
--record DIR must be outside the repo. Without --confirm-spend nothing is sent (exit 2).
The token comes from APIFY_TOKEN only and is never printed or saved. The run-sync call does not
return the charge, so cost is a manual fact.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import apify_feed  # noqa: E402
import ats_resolver_report as arr  # noqa: E402

ACTOR = "glitchbound~ats-jobs-scraper"
CLOSING = "Now open the run in the Apify console and add its charge to the Log: this script cannot see the bill."


def _default_post(url, **kw):
    import requests
    return requests.post(url, **kw)


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def run(argv, post=None, out=print, root=ROOT, environ=None, clock=time.time):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input-json", required=True, metavar="PATH")
    ap.add_argument("--record", required=True, metavar="DIR")
    ap.add_argument("--confirm-spend", action="store_true")
    args = ap.parse_args(argv)
    post = post or _default_post
    environ = os.environ if environ is None else environ

    if _inside(args.record, root):
        out("refused: --record %s is inside the repo; pick a folder outside it" % args.record)
        return 2
    try:
        with open(args.input_json, "r", encoding="utf-8") as fh:
            run_input = json.load(fh)
    except Exception as exc:
        out("cannot read --input-json: " + type(exc).__name__)
        return 2
    token = apify_feed.get_token(environ)
    url = apify_feed.RUN_SYNC_URL.format(actor=ACTOR)
    if not args.confirm_spend:
        out("would POST %s" % url)
        out("  token: %s" % ("<set>" if token else "<missing>"))
        out("  body (verbatim from %s): %s" % (args.input_json, json.dumps(run_input)[:300]))
        out("no request made: add --confirm-spend to spend money")
        return 2
    if not token:
        out("APIFY_TOKEN is not set; no request made")
        return 2

    started = clock()
    try:
        resp = post(url, json=run_input, headers={"Authorization": "Bearer " + token},
                    timeout=240, allow_redirects=False)
        code, raw = resp.status_code, resp.content
    except Exception as exc:
        out("post raised " + type(exc).__name__)
        return 1
    elapsed = clock() - started
    if not isinstance(code, int) or not 200 <= code < 300:
        out("non-2xx status: %s" % code)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        Path(args.record).mkdir(parents=True, exist_ok=True)
        raw = raw if isinstance(raw, bytes) else b""
        dest = None
        for n in range(1000):
            name = "ats_resolver_%s%s.json" % (stamp, "" if n == 0 else "_%d" % n)
            try:
                with open(Path(args.record) / name, "xb") as fh:
                    fh.write(raw)
            except FileExistsError:
                continue
            dest = Path(args.record) / name
            break
        if dest is None:
            raise OSError("no free file name")
        out("raw reply saved: %s" % dest)
    except Exception as exc:
        out("could not save the raw reply: " + type(exc).__name__)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        out("reply is not JSON")
        return 1
    if not isinstance(data, list):
        out("reply is JSON but not a list (got %s)" % type(data).__name__)
        return 1
    for line in arr.render_census(arr.census(data)):
        out(line)
    out("elapsed seconds: %.1f" % elapsed)
    out(CLOSING)
    return 0


def main():
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
