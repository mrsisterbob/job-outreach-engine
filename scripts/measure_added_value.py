"""Read-only: what do the unwired board classifier and domain logic find beyond the free resolver?

  python scripts/measure_added_value.py --sample S.csv --free FREE_RESULT.csv [--pause 0.7] [--result-out R.csv [--force]]

Runs scripts/measure_board_systems.measure over the sample (GET only, nine requests per company at
most, its User-Agent and timeout, resumable cache next to the sample file) and joins the answer to the
free resolver's RESULT CSV (TASK-223). Prints company names and counts only. Writes nothing to the CRM,
the DB or the repo; --result-out writes the added boards as a RESULT CSV outside the repo.
"""
import argparse
import csv
import importlib.util
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import warm_sample  # noqa: E402

_spec = importlib.util.spec_from_file_location("measure_board_systems", ROOT / "scripts" / "measure_board_systems.py")
mbs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mbs)

CACHE_NAME = "measure_added_value_cache_v1.json"
# Systems this repo has a job-fetch function for (main.fetch_<system>_jobs); a test checks it against main.
FETCHER_SYSTEMS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workday")
PROBE_ONLY = ("workable", "bamboohr")
VENDORS = ("workday", "icims", "taleo", "successfactors", "oracle", "adp", "ukg", "greenhouse", "lever",
           "ashby", "smartrecruiters", "workable", "bamboohr")
CEILING = ("fetchable today", "probe only", "no fetcher", "unknown_system", "not located")


def read_free(path):
    """{company_key: system} from a RESULT CSV (company_key,system,token)."""
    with open(path, encoding="utf-8", newline="") as f:
        return {r["company_key"]: r["system"] for r in csv.DictReader(f) if r.get("company_key")}


def extras_of(rec):
    """Board systems measure() found for a company, in order, deduplicated."""
    found = list(rec.get("boards", []))
    if rec.get("bucket") in VENDORS and rec["bucket"] not in found:
        found.append(rec["bucket"])
    return found


def ceiling_bucket(system, rec):
    if system in FETCHER_SYSTEMS:
        return "fetchable today"
    if system in PROBE_ONLY:
        return "probe only"
    if system in VENDORS or system == "own_site":
        return "no fetcher"
    if rec.get("bucket") == "unknown_system":
        return "unknown_system"
    return "not located"


def classify(row, free, rec):
    """One company's joined record."""
    key = row["company_key"]
    free_sys = free.get(key)
    found = extras_of(rec)
    resolved = free_sys is not None
    extras = [] if resolved else found
    if resolved:
        final = free_sys
    elif found:
        final = found[0]
    elif rec.get("bucket") == "own_site":
        final = "own_site"
    else:
        final = ""
    return {"company_key": key, "name": row["name"], "tier": row["tier"], "already_resolved": resolved,
            "extras": extras, "board_url": rec.get("board_url", "") if extras else "",
            "triple": (rec.get("workday_triple_shape") or "") if "workday" in extras else "",
            "domain_source": rec.get("domain_source", row.get("domain_source", "")),
            "no_answer": bool(rec.get("no_answer")), "final": final,
            "ceiling": ceiling_bucket(final, rec), "slug": row.get("slug", ""), "bucket": rec.get("bucket", "")}


def summarize(joined):
    tiers = ["all"] + sorted({j["tier"] for j in joined})
    out = {}
    for t in tiers:
        js = [j for j in joined if t == "all" or j["tier"] == t]
        ex = [j for j in js if j["extras"]]
        out[t] = {
            "n": len(js),
            "with_extra": len(ex),
            "already_resolved": sum(j["already_resolved"] for j in js),
            "no_answer": sum(j["no_answer"] for j in js),
            "per_system": dict(Counter(s for j in ex for s in j["extras"])),
            "workday_shapes": sum(1 for j in js if j["triple"]),
            "by_domain_source": dict(Counter(j["domain_source"] for j in ex)),
            "ceiling": {c: sum(1 for j in js if j["ceiling"] == c) for c in CEILING},
        }
    return out


def report_lines(joined):
    lines = []
    for j in joined:
        if j["no_answer"]:
            lines.append(f"{j['name']} [{j['tier']}]: no answer (retried next run)")
        elif j["already_resolved"]:
            lines.append(f"{j['name']} [{j['tier']}]: already resolved ({j['final']})")
        elif j["extras"]:
            lines.append(f"{j['name']} [{j['tier']}]: extra {','.join(j['extras'])} board_url={j['board_url'] or '-'}"
                         + (f" workday_shape={j['triple']}" if j["triple"] else "")
                         + f" domain={j['domain_source']}")
    for t, v in summarize(joined).items():
        n = v["n"]
        lines.append(f"TIER {t} (n={n}): with an extra board {v['with_extra']} of {n}; already resolved "
                     f"{v['already_resolved']} of {n}; no answer {v['no_answer']} of {n}")
        lines.append(f"  extras per system: {v['per_system']} (n={v['with_extra']} companies)")
        lines.append(f"  Workday tenant shapes: {v['workday_shapes']} of {n} (a shape is NOT a proven board; "
                     "it needs a real fetch)")
        lines.append(f"  extras by domain source: {v['by_domain_source']} (a guessed domain can be a different "
                     "company's site)")
        lines.append("  ceiling: " + "; ".join(f"{c} {v['ceiling'][c]} of {n}" for c in CEILING))
    lines.append("FOOTER: ceiling is by this repo's fetchers (" + ", ".join(FETCHER_SYSTEMS) + "). Whether "
                 "iCIMS/SuccessFactors/Taleo have a public JSON endpoint is NOT measured here.")
    return lines


def write_result(joined, path):
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["company_key", "system", "token"])
            for j in joined:
                for s in j["extras"]:
                    w.writerow([j["company_key"], s, j["triple"] if s == "workday" and j["triple"] else j["slug"]])
        os.replace(tmp, out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run(rows, free, requests, cache_path, pause):
    cache = mbs.load_cache(cache_path)
    joined = []
    for i, row in enumerate(rows, 1):
        key = row["company_key"]
        rec = cache.get(key)
        if rec is None:
            email = row.get("domain_source") == "email" and row.get("domain")
            entry = {"domains": [row["domain"]] if email else []}
            rec = mbs.measure(requests, entry, row["slug"], pause)
            if not rec.get("no_answer"):
                cache[key] = rec
                mbs.save_cache(cache_path, cache)
        print(f"[{i}/{len(rows)}] {row['name']}", flush=True)
        joined.append(classify(row, free, rec))
    return joined


def main(argv=None, requests=None, repo_root=ROOT):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument("--free", required=True)
    ap.add_argument("--pause", type=float, default=mbs.PAUSE)
    ap.add_argument("--result-out")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    if args.result_out:
        if warm_sample._inside(args.result_out, repo_root):
            print(f"refused: {args.result_out} is inside the repo; write the result outside it")
            return 2
        if Path(args.result_out).exists() and not args.force:
            print(f"refused: {args.result_out} already exists (--force to replace)")
            return 2
    try:
        rows = warm_sample.read_sample(args.sample)
        free = read_free(args.free)
    except (ValueError, OSError, KeyError) as e:
        print(f"could not read input: {type(e).__name__}: {e}")
        return 2
    if requests is None:
        import requests
    cache_path = Path(args.sample).parent / CACHE_NAME
    joined = run(rows, free, requests, cache_path, args.pause)
    for line in report_lines(joined):
        print(line)
    if args.result_out:
        try:
            write_result(joined, args.result_out)
        except OSError as e:
            print(f"could not write the result CSV: {type(e).__name__}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
