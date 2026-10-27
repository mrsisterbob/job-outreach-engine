"""Read-only: which job-board system do warm companies with 5+ contacts use?

Reads the warm companies from two exported TSV files on disk (no CRM, no webhook, no secrets):
  python scripts/measure_board_systems.py [--warm PATH] [--alumni PATH] [--cache PATH] [--pause S]
The files hold people's names/emails/links; only company names and counts are ever printed.

Step 1 (no domain needed): for each company in the 5+ set, probe six public board endpoints at the
slug pipeline_utils.ats_slug_guess gives. A hit proves a board EXISTS at that slug, not that it is
this company's; where the response carries the board's own name it is recorded and mismatches are
flagged. Endpoints (each confirmed with a real fetch on 2026-10-26, see the TASK-171 Log):
  greenhouse  boards-api.greenhouse.io/v1/boards/{s}/jobs   200 + non-empty "jobs"
  lever       api.lever.co/v0/postings/{s}?mode=json        200 + non-empty list
  ashby       api.ashbyhq.com/posting-api/job-board/{s}     200 + non-empty "jobs"
  smartrecruiters api.smartrecruiters.com/v1/companies/{s}/postings?limit=1   200 + totalFound > 0
              (answers 200 with totalFound 0 for ANY id, so a board with no open postings is invisible)
  workable    apply.workable.com/api/v1/widget/accounts/{s}  200 + "name" + non-empty "jobs" (404 when absent;
              200 with jobs=[] is a dormant account, not a board)
  bamboohr    {s}.bamboohr.com/careers/list                  200 JSON with "result" (302 when absent;
              a tenant with zero postings still counts as a board)
Step 2 (only for companies with no board hit): careers page at {domain}/careers|/jobs|/careers/,
domain = email evidence when a contact has a corporate email domain, else the guess {slug}.com
(counted separately); the page is classified by board_system_classifier.

Safety: GET only, descriptive User-Agent, 8 s timeout, one request at a time, a pause between
requests, <= 9 requests per company. Writes nothing to the CRM, SQLite or the repo; progress is
cached in a file outside the repo and a re-run resumes from it.
"""
import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import warm_export_reader as wer  # noqa: E402
from board_system_classifier import classify_board_system  # noqa: E402
from warm_company_domain import corporate_domain, guess_domain  # noqa: E402

UA = "job-outreach-measure/1.0 (read-only careers board survey; contact kjmiller406@gmail.com)"
TIMEOUT = 8
PAUSE = 0.7
CAREERS_PATHS = ("/careers", "/jobs", "/careers/")
DEFAULT_WARM = "C:/Users/kjmil/Downloads/Job_Outreach_CRM - Carmen Warm.tsv"
DEFAULT_ALUMNI = "C:/Users/kjmil/Downloads/alumni_master_2026-10-26/PASTE_new.tsv"
CACHE_NAME = "measure_board_systems_171_v2.json"  # bump the version when measurement logic changes
SYSTEMS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "bamboohr")


def _get(requests, url, pause=PAUSE):
    time.sleep(pause)
    try:
        return requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA}, allow_redirects=False)
    except Exception:
        return None


def _json(r):
    try:
        return r.json()
    except Exception:
        return None


def probe_url(system, slug):
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
        "bamboohr": f"https://{slug}.bamboohr.com/careers/list",
    }[system]


def judge(system, response):
    """(hit, board_name) for one endpoint response. hit is None when there was no answer at all."""
    if response is None:
        return None, ""
    if response.status_code == 429 or response.status_code >= 500:
        return None, ""  # throttled / server error: no answer, not "no board"
    if response.status_code != 200:
        return False, ""
    data = _json(response)
    if system in ("greenhouse", "ashby"):
        jobs = data.get("jobs") if isinstance(data, dict) else None
        name = ""
        if jobs and system == "greenhouse" and isinstance(jobs[0], dict):
            name = str(jobs[0].get("company_name") or "")
        return bool(jobs), name
    if system == "lever":
        return bool(isinstance(data, list) and data), ""
    if system == "smartrecruiters":
        if not isinstance(data, dict) or not (data.get("totalFound") or 0) > 0:
            return False, ""
        first = (data.get("content") or [{}])[0]
        company = first.get("company") if isinstance(first, dict) else None
        return True, str((company or {}).get("name") or "")
    if system == "workable":
        # Workable answers 200 + name for dormant/reserved accounts with zero jobs (measured: ford,
        # nasa, deloitte all do), so a board needs at least one job.
        ok = isinstance(data, dict) and bool(data.get("name")) and bool(data.get("jobs"))
        return ok, str(data.get("name") or "") if ok else ""
    if system == "bamboohr":
        return bool(isinstance(data, dict) and "result" in data), ""
    return False, ""


def _get_url(requests, url, pause):
    time.sleep(pause)
    try:
        return requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA}, allow_redirects=False)
    except Exception:
        return None


def _unanswered(r):
    return r is None or r.status_code == 429 or r.status_code >= 500


def measure(requests, entry, slug, pause):
    """Cache record for one company: 6 board probes + at most 3 careers fetches (9 requests).

    rec["no_answer"] lists systems (and "careers") that gave no usable answer (timeout, 429, 5xx);
    a record with any is not final and the caller must not cache it.
    """
    boards, names, no_answer = [], {}, []
    for system in SYSTEMS:
        hit, name = judge(system, _get(requests, probe_url(system, slug), pause))
        if hit is None:
            no_answer.append(system)
        elif hit:
            boards.append(system)
            if name:
                names[system] = name
    rec = {"boards": boards, "board_names": names, "no_answer": no_answer}
    if boards:
        rec["bucket"] = "has_board"
        return rec
    domain = corporate_domain([f"x@{d}" for d in entry.get("domains", [])])
    source = "email"
    if not domain:
        domain, source = guess_domain(slug), "guess"
    rec.update(domain=domain, domain_source=source, bucket="not_located")
    budget = len(CAREERS_PATHS)
    for path in CAREERS_PATHS:
        if budget <= 0:
            break
        budget -= 1
        url = f"https://{domain}{path}"
        r = _get_url(requests, url, pause)
        if _unanswered(r):
            if "careers" not in no_answer:
                no_answer.append("careers")
            continue
        if r.status_code in (301, 302, 303, 307, 308):
            loc = urljoin(url, r.headers.get("Location", "") or "")
            if not loc.startswith(("http://", "https://")):
                continue
            # a redirect straight onto a vendor host is evidence by itself, whatever it then answers
            via_host = classify_board_system(loc, "")
            if via_host not in ("unknown", "own_site"):
                rec["bucket"] = via_host
                return rec
            if budget <= 0:
                continue
            budget -= 1
            url = loc
            r = _get_url(requests, url, pause)
            if _unanswered(r):
                if "careers" not in no_answer:
                    no_answer.append("careers")
                continue
        if r.status_code == 200 and isinstance(r.text, str):
            system = classify_board_system(getattr(r, "url", None) or url, r.text)
            rec["bucket"] = "unknown_system" if system == "unknown" else system
            return rec
    return rec


def load_cache(path):
    """Read the cache; a corrupt or unreadable file is ignored with a message, never fatal."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        print(f"cache {path} is not a JSON object; ignoring it", file=sys.stderr)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"cache {path} unreadable ({type(e).__name__}); ignoring it", file=sys.stderr)
    return {}


def save_cache(path, cache):
    """Atomic write: temp file in the same directory, then os.replace."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, path)


def mismatch(slug, board_name, normalizer):
    """True when a board's own name does not match the company's slug."""
    if not board_name:
        return False
    return re.sub(r"[^a-z0-9]", "", normalizer(board_name).lower()) != slug


def report(cache, target, norm):
    total = len(target)
    recs = [cache[k] for k in sorted(target) if k in cache]
    print(f"\nDENOMINATOR {total}")
    counts = Counter(r["bucket"] for r in recs)
    for b, n in counts.most_common():
        print(f"  {b}: {n} ({100 * n / total:.1f}%)")
    print("slug hits per system (a company can hit several):")
    for s in SYSTEMS:
        print(f"  {s}: {sum(1 for r in recs if s in r.get('boards', []))}")
    print("endpoint gave no answer (timeout/error) per system:",
          dict(Counter(s for r in recs for s in r.get("no_answer", []))))
    print("domain evidence vs guess (no-board companies):",
          dict(Counter(r.get("domain_source") for r in recs if r.get("domain_source"))))
    print("10+ contacts with a board hit (company | slug | systems | board names | mismatch):")
    for r in sorted(recs, key=lambda r: -r.get("count", 0)):
        if r.get("count", 0) >= 10 and r.get("boards"):
            names = dict(r["board_names"])
            flag = [s for s, n in names.items() if mismatch(r["slug"], n, norm)]
            print(f"  {r['name']} | {r['slug']} | {r['boards']} | {names} | mismatch={flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warm", default=DEFAULT_WARM)
    ap.add_argument("--alumni", default=DEFAULT_ALUMNI)
    ap.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / CACHE_NAME))
    ap.add_argument("--pause", type=float, default=PAUSE)
    ap.add_argument("--count-only", action="store_true")
    args = ap.parse_args()
    import requests
    logging.disable(logging.CRITICAL)
    import main as m  # heavy import with logging side effects; used only for normalize_company_for_match
    import pipeline_utils as pu
    import warm_company_kind as wk
    norm = m.normalize_company_for_match
    companies, stats = wer.read_files([args.warm, args.alumni], normalizer=norm)
    target = wer.companies_at_least(companies)
    people = sum(c["count"] for c in companies.values())
    print(f"rows parsed {stats['rows']}, bad-column rows {stats['bad_rows']}, no company {stats['no_company']}")
    print(f"people {people}, companies {len(companies)}, with 10+ contacts "
          f"{sum(1 for c in companies.values() if c['count'] >= 10)}")
    print(f"SET: {len(target)} companies with 5+ contacts")
    by_src = Counter()
    for c in target.values():
        for s, n in c["by_source"].items():
            by_src[s] += n
    tot = sum(by_src.values()) or 1
    print("contacts in the set by file (file1=warm, file2=alumni):",
          {s: f"{n} ({100 * n / tot:.1f}%)" for s, n in sorted(by_src.items())})
    with_email = sum(1 for c in target.values() if corporate_domain([f"x@{d}" for d in c["domains"]]))
    print(f"companies in the set with a usable email domain: {with_email} of {len(target)}")
    if args.count_only:
        return 0
    cache_path = Path(args.cache)
    cache = load_cache(cache_path)
    session = {}  # this run's records, including non-final ones (any no-answer) that are not cached
    for i, (key, entry) in enumerate(sorted(target.items()), 1):
        if key in cache:
            continue
        name = entry["name"]
        slug = pu.ats_slug_guess(norm(name))  # from the normalized name, never the raw display name
        if not pu.is_probable_company_name(name):
            rec = {"bucket": "skipped", "why": "not a probable company name"}
        elif wk.non_employer_kind(name):
            rec = {"bucket": "skipped", "why": wk.non_employer_kind(name)}
        else:
            rec = measure(requests, entry, slug, args.pause)
        rec.update(name=name, count=entry["count"], slug=slug)
        session[key] = rec
        if not rec.get("no_answer"):
            cache[key] = rec
            save_cache(cache_path, cache)
        print(f"[{i}/{len(target)}] {name}: {rec['bucket']}"
              + (f" (no answer: {rec['no_answer']}, retried next run)" if rec.get("no_answer") else ""), flush=True)
    merged = {**cache, **session}
    report(merged, target, norm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
