"""Baseline the free resolver, auto_expand_ats_slug, over the fixed 60-company sample. Read-only.

  python scripts/measure_free_resolver.py --sample S.csv --out RESULT.csv [--pause 0.4] [--force]

Runs the REAL function with every write turned off: JOBS_DB_PATH points at a temp file before
`import main`; set_filter and upsert_company_identity are in-memory recorders; get_filter reads
ats_company_slugs as empty (a cold run); crm_post and crm_get raise. requests.get is wrapped to
pause, send a User-Agent and tally (company, host, status). Prints company names and counts only.
Name mismatch between a board name and the slug is NOT checked: the function makes one
request per board and the list endpoints carry no board name, and a second request per hit is
not added here.
"""
import argparse
import csv
import logging
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import warm_sample  # noqa: E402

UA = "job-outreach-measure/1.0 (read-only careers board survey; contact kjmiller406@gmail.com)"
SYSTEMS = ("greenhouse", "lever", "ashby", "smartrecruiters")
HOSTS = {"boards-api.greenhouse.io": "greenhouse", "api.lever.co": "lever",
         "api.ashbyhq.com": "ashby", "api.smartrecruiters.com": "smartrecruiters"}
RESOLVED, MISS, ERROR, SKIPPED = "resolved", "clean miss", "error", "skipped"


def _system_of(url):
    for host, system in HOSTS.items():
        if host in url:
            return system
    return "other"


class CrmCallAttempted(RuntimeError):
    """A CRM call during a read-only run: never swallowed by the per-company guard."""


def _crm_stub(*_a, **_k):
    raise CrmCallAttempted("measure_free_resolver: a CRM call was attempted during a read-only run")


def run_measure(rows, m, pause=0.4, sleep=time.sleep, clock=time.monotonic):
    """Measure `rows` (sample rows) through m.auto_expand_ats_slug. Returns a result dict."""
    calls = []          # (company_key, system, outcome): outcome is an int status or an exception class name
    sr_zero = [0]
    hits_seen = []      # (name, slug, board_system) from the upsert recorder
    filter_writes = []
    state = {"key": None, "n": 0}
    real_get = m.requests.get
    saved = {n: getattr(m, n) for n in ("set_filter", "upsert_company_identity", "get_filter",
                                        "crm_post", "crm_get")}
    orig_get_filter = saved["get_filter"]

    def get_filter(key, default_val=None):
        if key == "ats_company_slugs":
            return []
        return orig_get_filter(key, default_val)

    def wrapped_get(url, *a, **kw):
        if state["n"]:
            sleep(pause)
        state["n"] += 1
        system = _system_of(url)
        headers = dict(kw.pop("headers", None) or {})
        headers.setdefault("User-Agent", UA)
        try:
            res = real_get(url, *a, headers=headers, **kw)
        except Exception as e:
            calls.append((state["key"], system, type(e).__name__))
            raise
        calls.append((state["key"], system, res.status_code))
        if system == "smartrecruiters" and res.status_code == 200:
            try:
                if not ((res.json() or {}).get("totalFound") or 0) > 0:
                    sr_zero[0] += 1
            except Exception:
                pass
        return res

    results = []
    interrupted = False
    t0 = clock()
    try:
        m.set_filter = lambda key, val: filter_writes.append((key, val))
        m.upsert_company_identity = lambda name, **kw: hits_seen.append((name, kw.get("ats_slug"), kw.get("board_system")))
        m.get_filter = get_filter
        m.crm_post = _crm_stub
        m.crm_get = _crm_stub
        m.requests.get = wrapped_get
        for row in rows:
            state["key"] = row["company_key"]
            before_calls, before_hits = len(calls), len(hits_seen)
            errors = []
            try:
                slug = m.auto_expand_ats_slug(row["name"], errors=errors)
            except CrmCallAttempted:
                raise
            except KeyboardInterrupt:
                interrupted = True
                break
            except Exception as e:
                results.append({"company_key": row["company_key"], "tier": row["tier"], "name": row["name"],
                                "status": ERROR, "system": "", "token": ""})
                print(f"unexpected {type(e).__name__} on {row['name']}; counted as error")
                continue
            mine = calls[before_calls:]
            if slug and len(hits_seen) > before_hits:
                status, system = RESOLVED, hits_seen[-1][2]
            elif not mine and not m.is_probable_company_name(row["name"]):
                status, system = SKIPPED, ""
            elif errors or any(isinstance(o, str) or m.ats_status_is_failure(o) for _, _, o in mine):
                status, system = ERROR, ""
            else:
                status, system = MISS, ""
            results.append({"company_key": row["company_key"], "tier": row["tier"], "name": row["name"],
                            "status": status, "system": system, "token": slug or ""})
    finally:
        m.requests.get = real_get
        for n, f in saved.items():
            setattr(m, n, f)
    seconds = clock() - t0
    per_system = {s: Counter() for s in SYSTEMS}
    for _, system, o in calls:
        c = per_system.get(system)
        if c is None:
            continue
        if isinstance(o, str) or m.ats_status_is_failure(o):
            c["failure"] += 1
        elif o == 200:
            c["answered 200"] += 1
        else:
            c["no board"] += 1
    return {"results": results, "per_system": per_system, "sr_zero": sr_zero[0],
            "requests": len(calls), "seconds": seconds, "filter_writes": filter_writes,
            "interrupted": interrupted}


def report_lines(out):
    res = out["results"]
    lines = []

    def split(rs):
        c = Counter(r["status"] for r in rs)
        return (f"n={len(rs)} resolved={c[RESOLVED]} clean_miss={c[MISS]} "
                f"error={c[ERROR]} skipped={c[SKIPPED]}")

    lines.append("ALL " + split(res))
    for t in warm_sample.TIERS:
        lines.append(f"TIER {t} " + split([r for r in res if r["tier"] == t]))
    resolved = [r for r in res if r["status"] == RESOLVED]
    for s in SYSTEMS:
        lines.append(f"RESOLVED {s} {sum(1 for r in resolved if r['system'] == s)} of n={len(res)}")
    for s in SYSTEMS:
        c = out["per_system"][s]
        lines.append(f"CHECKS {s} answered_200={c['answered 200']} no_board={c['no board']} failure={c['failure']}")
    lines.append(f"SMARTRECRUITERS 200 with totalFound 0 (clean miss, board may exist): {out['sr_zero']}")
    lines.append(f"REQUESTS {out['requests']}")
    lines.append(f"SECONDS {out['seconds']:.1f}")
    if out.get("interrupted"):
        lines.append(f"INTERRUPTED: measured {len(res)} companies before Ctrl-C")
    lines.append("NOTE this script sends its own User-Agent; production sends the default requests one")
    lines.append("NOTE resolved includes a greenhouse board that exists with zero jobs")
    lines.append("NOTE answered_200 is not resolved: Lever [] / Ashby {} / SmartRecruiters totalFound 0 are 200 but a clean miss")
    lines.append("NAME MISMATCH unchecked (no board name without a second request per hit)")
    return lines


def write_result(results, path):
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["company_key", "system", "token"])
            for r in results:
                if r["status"] == RESOLVED:
                    w.writerow([r["company_key"], r["system"], r["token"]])
        os.replace(tmp, out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None, m=None, repo_root=ROOT, sleep=time.sleep):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pause", type=float, default=0.4)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    if warm_sample._inside(args.out, repo_root):
        print(f"refused: {args.out} is inside the repo; write the result outside it")
        return 2
    if Path(args.out).exists() and not args.force:
        print(f"refused: {args.out} already exists (--force to replace)")
        return 2
    try:
        rows = warm_sample.read_sample(args.sample)
    except (ValueError, OSError) as e:
        print(str(e))
        return 2
    if m is None:
        os.environ["JOBS_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="free_resolver_"), "scratch.db")
        logging.disable(logging.CRITICAL)
        import main as m
    out = run_measure(rows, m, pause=args.pause, sleep=sleep)
    for line in report_lines(out):
        print(line)
    try:
        write_result(out["results"], args.out)
    except OSError as e:
        print(f"could not write the result CSV to {args.out}: {type(e).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
