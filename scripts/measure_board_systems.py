"""Read-only: which job-board system do warm companies with 5+ contacts and no GH/Lever/Ashby board use?

Run by hand against the live Sheet and web:  python scripts/measure_board_systems.py [--cache PATH]
Needs CRM_WEBHOOK_URL (and CRM_SHARED_SECRET) in the environment, like scripts/time_crm_reads.py.

Safety: GET only (plus the one CRM read), descriptive User-Agent, 8 s timeout, one request at a time,
a pause between requests, <= 6 requests per company. Writes nothing to the CRM, SQLite or the repo;
progress goes to a cache file outside the repo (default: temp dir) and a re-run resumes from it.

Buckets (sum to the set): not_located, unknown_system, <named system>..., skipped.
"""
import argparse
import json
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from board_system_classifier import classify_board_system  # noqa: E402
from warm_company_domain import corporate_domain, guess_domain  # noqa: E402

UA = "job-outreach-measure/1.0 (read-only careers page survey)"
TIMEOUT = 8
PAUSE = 1.0
CAREERS_PATHS = ("/careers", "/jobs", "/careers/", "/about/careers")


def _load_main():
    import main  # heavy import, kept lazy
    return main


def _get(requests, url):
    time.sleep(PAUSE)
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA}, allow_redirects=True)
    except Exception:
        return None
    return r if r.status_code == 200 and isinstance(r.text, str) else None


def has_board(requests, slug):
    for url in (f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                f"https://api.lever.co/v0/postings/{slug}?mode=json",
                f"https://api.ashbyhq.com/posting-api/job-board/{slug}"):
        r = _get(requests, url)
        try:
            if r is not None and r.json():
                return True
        except Exception:
            pass
    return False


def measure(requests, name, entry, slug, pu):
    """Return the cache record for one company (3 board probes + up to 3 page fetches)."""
    if has_board(requests, slug):
        return {"bucket": "skipped", "why": "already has a board"}
    emails = [c.get("email", "") for c in entry.get("contacts", [entry])]
    domain = corporate_domain(emails)
    source = "email"
    if not domain:
        domain, source = guess_domain(slug), "guess"
    for path in CAREERS_PATHS[:3]:
        r = _get(requests, f"https://{domain}{path}")
        if r is not None:
            system = classify_board_system(r.url, r.text)
            bucket = "unknown_system" if system == "unknown" else system
            return {"bucket": bucket, "domain": domain, "domain_source": source, "url": r.url}
    return {"bucket": "not_located", "domain": domain, "domain_source": source}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "measure_board_systems.json"))
    args = ap.parse_args()
    import requests
    m = _load_main()
    import pipeline_utils as pu
    import warm_company_kind as wk
    warm = m.get_warm_crm_contacts()
    if not warm:
        print("Carmen Warm read returned nothing (CRM_WEBHOOK_URL unset or read failed). No counts.")
        return 2
    target = {k: v for k, v in warm.items() if (v.get("count") or 0) >= 5}
    print(f"SET: {len(target)} companies with count >= 5 (of {len(warm)} warm companies)")
    cache_path = Path(args.cache)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    for i, (key, entry) in enumerate(sorted(target.items()), 1):
        if key in cache:
            continue
        name = entry.get("raw_company") or key
        if not pu.is_probable_company_name(name):
            cache[key] = {"bucket": "skipped", "why": "not a probable company name"}
        elif wk.non_employer_kind(name):
            cache[key] = {"bucket": "skipped", "why": wk.non_employer_kind(name)}
        else:
            cache[key] = measure(requests, name, entry, pu.ats_slug_guess(name), pu)
        cache[key]["name"] = name
        cache_path.write_text(json.dumps(cache))
        print(f"[{i}/{len(target)}] {name}: {cache[key]['bucket']}", flush=True)
    counts = Counter(v["bucket"] for v in cache.values())
    total = len(target)
    print(f"\nDENOMINATOR {total}")
    for b, n in counts.most_common():
        print(f"  {b}: {n} ({100 * n / total:.1f}%)")
    src = Counter(v.get("domain_source") for v in cache.values() if v.get("domain_source"))
    print("domain evidence vs guess:", dict(src))
    return 0


if __name__ == "__main__":
    sys.exit(main())
