"""Pure helpers for the Hirebase Free API no-board fallback (no I/O, no clock, no imports from main.py).

Guesses the exact Hirebase company slugs for a warm company, builds the search body, turns one
Hirebase job into the JSearch-shaped posting dict /w already understands, and decides whether a job
really belongs to the warm company. `normalize` is passed in because normalize_company_for_match
lives in main.py.

REAL RISK: the JOB_KEYS and LOCATION_KEYS below are only partly verified against live output. These
tests pass on hand-built jobs that use the same guessed keys, so a wrong key name would still be
green here. TASK-182 turns an all-malformed response into an error so a wrong key name can never
read as "no jobs".
"""
import html
import math
import re
from collections import namedtuple
from datetime import datetime, timedelta, timezone

# Our name -> the Hirebase job key.
# MEASURED present (Kevin, Playground, 2026-10-26): job_title, application_link, location_raw,
#   locations, date_posted, job_board, platform_job_id, company_name, company_slug, company_link,
#   first_ingested.
# NOT measured: the key NAME of the HTML description field ("description" is a guess).
JOB_KEYS = {
    "title": "job_title",              # measured
    "apply_link": "application_link",  # measured
    "location_raw": "location_raw",    # measured, never used for matching
    "locations": "locations",          # measured (array; multi-location rows exist)
    "date_posted": "date_posted",      # measured
    "platform_id": "platform_job_id",  # measured
    "company_name": "company_name",    # measured
    "company_slug": "company_slug",    # measured
    "description": "description",      # NOT measured: guess
}

# Key names inside one locations[] entry. NOT measured: assumed city / region / country.
LOCATION_KEYS = {"city": "city", "region": "region", "country": "country"}

DAYS_AGO_MAX = 30   # NOT measured: an upper clamp guessed for days_ago
LIMIT_MAX = 100     # MEASURED: limit max is 100

_MI_NAMES = {"michigan", "mi"}
_COUNTRIES = {"united states": "US", "usa": "US", "us": "US", "united states of america": "US"}


def _slugify(text):
    """Kebab-case: lowercase, punctuation dropped, whitespace and hyphens become single hyphens.
    NOT measured: how Hirebase forms slugs for names with punctuation ('&' is just dropped here)."""
    cleaned = re.sub(r"[^a-z0-9\s-]", "", str(text or "").lower())
    return "-".join(p for p in re.split(r"[\s-]+", cleaned) if p)


def slug_guesses(company_name, normalize):
    """At most 2 distinct slugs: plain name first ('Fifth Third Bank' -> fifth-third-bank), then the
    normalized name ('Haworth Inc.' -> haworth). A wrong slug costs nothing (MEASURED: a search
    returning 0 rows bills nothing), which is why exact slugs beat the loose company_names filter."""
    out = []
    for cand in (_slugify(company_name), _slugify(normalize(company_name))):
        if cand and cand not in out:
            out.append(cand)
    return out[:2]


def _clamp(value, lo, hi):
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def build_search_body(slugs, days_ago, limit, page=1):
    """Search body. Exact company_slugs only; no title or exclusion filter (none is known to exist).

    page: clamped to an int of at least 1. `page` is a body field and total_pages comes back
    (MEASURED, Fifth Third). NOT measured: that page 2 returns the rows AFTER page 1 with no
    overlap, or what order a search is sorted in (newest first is assumed).
    """
    return {
        "company_slugs": list(slugs),
        "geo_locations": [{"region": "Michigan"}],
        "days_ago": _clamp(days_ago, 1, DAYS_AGO_MAX),
        "page": _clamp(page, 1, 10 ** 6),
        "limit": _clamp(limit, 1, LIMIT_MAX),
    }


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text):
    """Tag removal plus entity unescape; block-ish tags become spaces so words do not fuse."""
    s = re.sub(r"</?(p|br|li|ul|ol|div|h[1-6])\b[^>]*>", " ", str(text or ""), flags=re.I)
    s = html.unescape(_TAG_RE.sub("", s))
    return re.sub(r"\s+", " ", s).strip()


def _loc_fields(entry):
    if not isinstance(entry, dict):
        return "", "", ""
    city, region, country = (entry.get(LOCATION_KEYS[k]) for k in ("city", "region", "country"))
    city = str(city).strip() if isinstance(city, str) else ""
    region = str(region).strip() if isinstance(region, str) else ""
    country = str(country).strip() if isinstance(country, str) else ""
    if region.lower() in _MI_NAMES:
        region = "MI"
    return city, region, _COUNTRIES.get(country.lower(), country)


def _pick_location(locations):
    """The Michigan filter may leak and rows list several places: first Michigan entry, else the
    first entry, else empty (the existing geography gate then drops it). Never location_raw."""
    if not isinstance(locations, (list, tuple)):
        return "", "", ""
    entries = [_loc_fields(e) for e in locations if isinstance(e, dict)]
    if not entries:
        return "", "", ""
    for ent in entries:
        if ent[1] == "MI":
            return ent
    return entries[0]


def _text(v):
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        return ""
    return str(v).strip()


def parse_job(job):
    """One Hirebase job -> JSearch-shaped posting dict, or None when title, company_name, slug,
    platform_job_id or an https application_link is missing or the wrong type."""
    if not isinstance(job, dict):
        return None
    g = lambda k: job.get(JOB_KEYS[k])  # noqa: E731
    title, company, slug, pid = (_text(g(k)) for k in ("title", "company_name", "company_slug", "platform_id"))
    link = g("apply_link")
    if not (title and company and slug and pid):
        return None
    if not isinstance(g("company_slug"), str) or not isinstance(g("company_name"), str):
        return None   # wrong type; only platform_job_id may legitimately arrive as an int
    if not isinstance(link, str) or not link.strip().lower().startswith("https://"):
        return None
    city, state, country = _pick_location(g("locations"))
    out = {
        "employer_name": company,
        "job_title": title,
        "job_id": "hirebase_" + slug + "_" + pid,
        "job_requisition_id": pid,
        "job_apply_link": link.strip(),
        "job_description": _strip_html(g("description")),
        "job_city": city,
        "job_state": state,
        "job_country": country,
    }
    posted = g("date_posted")
    if posted:
        out["job_posted_at_datetime_utc"] = str(posted)
    return out


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def job_belongs_to(job, company_name, slugs, normalize, name_optional=False):
    """True only when company_slug is exactly one of `slugs` AND the normalized job company name
    equals the normalized warm company name (strict: siblings like Haworth International fail)."""
    if not isinstance(job, dict):
        return False
    slug = job.get(JOB_KEYS["company_slug"])
    if not isinstance(slug, str) or slug not in set(slugs or ()):
        return False
    if name_optional:
        return True
    target = _squash(normalize(company_name))
    theirs = job.get(JOB_KEYS["company_name"])
    if not target or not isinstance(theirs, str):
        return False
    return _squash(normalize(theirs)) == target


def split_jobs(jobs, company_name, slugs, normalize, trusted_slugs=(), dropped=None):
    """One company per call -> (kept, stats); stats = returned / malformed / kept / unmatched.
    dropped: an optional caller-owned list that gets (returned_name, reason) for every dropped job: "no_match", or
    ("", "malformed") when nothing could be read. A non-list is ignored. Appending is the only mutation.

    A row whose company_slug is in `trusted_slugs` (a registry slug, verified against the company)
    passes on the slug alone; any other row keeps the strict slug-plus-name rule."""
    trusted = set(trusted_slugs or ())
    kept = []
    stats = {"returned": 0, "malformed": 0, "kept": 0, "unmatched": 0}
    for job in jobs or []:
        stats["returned"] += 1
        parsed = parse_job(job)
        if parsed is None:
            stats["malformed"] += 1
            if isinstance(dropped, list):
                dropped.append(("", "malformed"))
        elif (job.get(JOB_KEYS["company_slug"]) in trusted and isinstance(job.get(JOB_KEYS["company_slug"]), str)
              and job_belongs_to(job, company_name, slugs, normalize, name_optional=True))                 or job_belongs_to(job, company_name, slugs, normalize):
            kept.append(parsed)
            stats["kept"] += 1
        else:
            stats["unmatched"] += 1
            if isinstance(dropped, list):
                dropped.append((str(parsed.get("employer_name") or "").strip(), "no_match"))
    return kept, stats


SEARCH_URL = "https://api.hirebase.org/v2/jobs/search"
HirebaseResult = namedtuple("HirebaseResult", "status jobs total_count capped error")


def get_key(environ):
    """Stripped HIREBASE_API_KEY from the given environment mapping, or None. Environment only."""
    key = (environ.get("HIREBASE_API_KEY") or "").strip()
    return key or None


def fetch_company_jobs(body, key, http_post, timeout=60):
    """ONE Hirebase search request. status is "ok", "errored" or "unconfigured".

    "ok" with jobs == [] is a clean zero for this request only: the exact slug matched nothing in
    the window (or the slug guess is wrong; the two cannot be told apart). It is NOT proof the
    company has no jobs. `capped` is True when total_count > len(jobs): rows exist this request did
    not return, so the caller must not move that company's date watermark forward.
    Any failure is "errored", never an empty ok: a raising post, non-2xx (429 included), a
    non-object body, jobs missing or not a list, total_count not an int, or a non-empty jobs list
    whose every job fails parse_job (a wrong JOB_KEYS guess). 429 is not told apart from other
    failures: whether it means the monthly cap or the 100 requests per 60 s rate limit (docs, NOT
    measured) is unmeasured. `error` carries only the exception class name or status code, never
    str(exc), a response body, or the key.
    """
    if not key:
        return HirebaseResult("unconfigured", [], 0, False, "no HIREBASE_API_KEY")
    try:
        resp = http_post(SEARCH_URL, json=body, headers={"x-api-key": key}, timeout=timeout)
    except Exception as exc:
        return HirebaseResult("errored", [], 0, False, "post raised " + type(exc).__name__)
    code = getattr(resp, "status_code", None)
    if not isinstance(code, int) or isinstance(code, bool) or not 200 <= code < 300:
        return HirebaseResult("errored", [], 0, False, "http status %s" % (code,))
    try:
        data = resp.json()
    except Exception as exc:
        return HirebaseResult("errored", [], 0, False, "bad json " + type(exc).__name__)
    if not isinstance(data, dict):
        return HirebaseResult("errored", [], 0, False, "body is not an object")
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return HirebaseResult("errored", [], 0, False, "jobs missing or not a list")
    total = data.get("total_count")
    if isinstance(total, bool) or not isinstance(total, int):
        return HirebaseResult("errored", [], 0, False, "total_count is not an int")
    if jobs and not any(parse_job(j) for j in jobs):
        return HirebaseResult("errored", [], 0, False, "all %d jobs malformed" % len(jobs))
    page = body.get("page") if isinstance(body, dict) else None
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        page = 1
    limit = body.get("limit") if isinstance(body, dict) else None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = len(jobs)
    # capped only when rows remain beyond the page just fetched; page 1 equals total > len(jobs).
    return HirebaseResult("ok", jobs, total, total > (page - 1) * limit + len(jobs), "")


HIREBASE_MONTHLY_JOBS = 500      # MEASURED: Free plan cap, job rows returned per billing month
# DESIGN CHOICE, not a measurement: the meter is account-wide, so Playground tests spend from the
# same pool and the ledger cannot see them.
HIREBASE_RESERVE_JOBS = 100
HIREBASE_COMPANIES_PER_RUN = 8   # DESIGN CHOICE, not a measurement
HIREBASE_LIMIT_PER_COMPANY = 25  # DESIGN CHOICE, not a measurement (Fifth Third alone was 38 MI jobs/7 days)
HIREBASE_MIN_LIMIT = 5           # DESIGN CHOICE, not a measurement: a pick under this is not made
HIREBASE_FIRST_DAYS = 14         # DESIGN CHOICE, not a measurement: window for a never-searched company
HIREBASE_RETRY_DAYS = 3          # DESIGN CHOICE, not a measurement: a recent ok search is skipped

# DESIGN CHOICE, not a measurement. A company whose first completed search returned zero rows and
# which never returned a row since waits this long, not retry_days. TRAP: the body ALWAYS carries
# geo_locations Michigan and days_ago, so that zero means "no Michigan jobs in the window" OR "wrong
# slug" OR "Hirebase holds nothing", and the three cannot be told apart. Hence two weeks, not months.
# The permanent answer is the slug registry, not this bench.
HIREBASE_EMPTY_RETRY_DAYS = 14

# DESIGN CHOICE, not a measurement: at most 50 rows per company per pinned window (Fifth Third's 38
# in 7 days fits in two pages). One big employer can never cost more than
# 2 x HIREBASE_LIMIT_PER_COMPANY per window, which is what stops it eating the monthly 500.
HIREBASE_MAX_PAGES_PER_WINDOW = 2
# DESIGN CHOICE: a continuation must start within 7 days of the capped request, or the window is
# abandoned (a pinned window drifts stale) and the company plans as a plain search.
HIREBASE_CAPPED_CONTINUE_DAYS = 7

# pages: {key: page}; a key absent from it means page 1. picks stay 4-tuples (key, name, days, limit).
HirebasePlan = namedtuple("HirebasePlan", "picks month_used usable_left reserved skip_reason pages",
                          defaults=({},))


def _ledger_rows(ledger):
    """Normalize ledger rows to (ts, jobs, status, key, page, days_ago).

    Rows are 4-tuples (page 1, days_ago unknown) or 6-tuples (iso_ts, jobs, status, key, page, days_ago).
    """
    out = []
    for row in ledger:
        ts, jobs, status, key = row[:4]
        page = row[4] if len(row) > 4 else 1
        days_ago = row[5] if len(row) > 5 else None
        out.append((ts, jobs, status, key, page, days_ago))
    return out


def _plan_ts(ts, now):
    """Aware-UTC datetime; malformed or future timestamps count as `now`."""
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now if dt > now else dt


def _ledger_marks(ledger, now):
    """(last_ok, newest_ok_jobs, ever_rows) per key. errored jobs is a RESERVED limit, never rows.

    Ledger window caveat: hirebase_ledger_read returns only the trailing 60 days, so a company's
    zero row ages out and it plans as never searched again (first window).
    """
    last_ok, newest_jobs, ever_rows = {}, {}, {}
    for ts, jobs, status, key, _pg, _da in _ledger_rows(ledger):
        if status not in ("ok", "capped"):
            continue
        dt = _plan_ts(ts, now)
        try:
            n = max(0, int(jobs))
        except (ValueError, TypeError, OverflowError):
            n = 1  # unparseable: not provably empty
        if n > 0:
            ever_rows[key] = True
        if status == "ok" and (key not in last_ok or dt > last_ok[key]):
            last_ok[key] = dt
            newest_jobs[key] = n
    return last_ok, newest_jobs, ever_rows


def _empty_benched(key, last_ok, newest_jobs, ever_rows):
    return key in last_ok and not ever_rows.get(key) and newest_jobs.get(key) == 0


def first_search_was_empty(key, ledger, now):
    """True when key's newest ok search returned zero rows and it has never returned a row."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    last_ok, newest_jobs, ever_rows = _ledger_marks(ledger, now)
    return _empty_benched(key, last_ok, newest_jobs, ever_rows)


def plan_hirebase_run(candidates, ledger, now, per_run=HIREBASE_COMPANIES_PER_RUN,
                      monthly=HIREBASE_MONTHLY_JOBS, reserve=HIREBASE_RESERVE_JOBS,
                      limit_per_company=HIREBASE_LIMIT_PER_COMPANY, min_limit=HIREBASE_MIN_LIMIT,
                      first_days=HIREBASE_FIRST_DAYS, retry_days=HIREBASE_RETRY_DAYS,
                      empty_retry_days=HIREBASE_EMPTY_RETRY_DAYS, unbench_keys=frozenset()):
    """Pure spend guard: which no-board companies to search, each with days_ago and limit.

    candidates: iterable of (company_key, display_name, contact_count)
    ledger: iterable of (iso_timestamp, jobs, status, company_key); status "ok", "capped" or
    "errored", one row per request. The sum of the picks' limits never exceeds usable_left, because
    the cap check uses the REQUESTED limit as worst case (per docs, NOT measured).
    An empty-benched company (see first_search_was_empty) waits empty_retry_days, not retry_days.
    unbench_keys: keys with a newly resolved slug; for them the bench and the retry gap are ignored
    and they plan as a first search (first_days, no watermark).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    month_used = 0
    last_ok, newest_jobs, ever_rows = _ledger_marks(ledger, now)
    rows = _ledger_rows(ledger)
    for ts, jobs, status, key, _pg, _da in rows:
        dt = _plan_ts(ts, now)
        try:
            n = max(0, int(jobs))
        except (ValueError, TypeError, OverflowError):
            n = monthly  # unparseable: assume the worst, never grant budget
        if (dt.year, dt.month) == (now.year, now.month):
            month_used += n  # errored rows count too: a failed request may have billed
    usable_left = max(0, monthly - reserve - month_used)
    if usable_left < min_limit:
        return HirebasePlan([], month_used, usable_left, 0, "monthly_cap")
    retry_gap = timedelta(days=retry_days)
    empty_gap = timedelta(days=empty_retry_days)
    # Newest capped row per key that is newer than its newest ok row, with a known pinned window.
    capped_last = {}
    for ts, _jobs, status, key, pg, da in rows:
        if status != "capped" or da is None:
            continue
        dt = _plan_ts(ts, now)
        if key in last_ok and dt <= last_ok[key]:
            continue
        if key not in capped_last or dt > capped_last[key][0]:
            capped_last[key] = (dt, pg, da)
    cont_gap = timedelta(days=HIREBASE_CAPPED_CONTINUE_DAYS)
    eligible = []
    cont = []
    pages = {}
    for key, name, contacts in candidates:
        wm = last_ok.get(key)
        cap = capped_last.get(key)
        if cap is not None and key not in unbench_keys and now - cap[0] <= cont_gap:
            try:
                cpage = max(1, int(cap[1]))
            except (ValueError, TypeError, OverflowError):
                cpage = 1
            if cpage < HIREBASE_MAX_PAGES_PER_WINDOW:
                # CONTINUATION: next page of the pinned window, retry gap bypassed.
                cont.append((key, name, contacts, cpage + 1, _clamp(cap[2], 1, DAYS_AGO_MAX)))
                continue
            # Window ended at the last allowed page. The rows beyond it were never fetched (a known,
            # chosen loss, not hidden); the next window starts at this capped row's time.
            wm = cap[0]
        if key in unbench_keys:
            wm = None
        elif wm is not None:
            gap = empty_gap if _empty_benched(key, last_ok, newest_jobs, ever_rows) else retry_gap
            if now - wm < gap:
                continue
        eligible.append((key, name, contacts, wm))
    eligible.sort(key=lambda c: (-(c[2] or 0), c[0]))
    picks = []
    remaining = usable_left
    cont.sort(key=lambda c: (-(c[2] or 0), c[0]))
    for key, name, _contacts, page, days in cont:
        if len(picks) >= per_run or remaining < min_limit:
            break
        if remaining < limit_per_company:
            # Every page of a window must use the same limit (the ledger carries none, and the
            # (page-1)*limit offset assumes it); a smaller limit would re-fetch rows already paid for.
            # Not continued, and not re-planned as page 1 either: the window waits for budget.
            continue
        limit = limit_per_company
        picks.append((key, name, days, limit))
        pages[key] = page
        remaining -= limit
    for key, name, _contacts, wm in eligible:
        if len(picks) >= per_run or remaining < min_limit:
            break
        limit = min(limit_per_company, remaining)
        if wm is None:
            days = first_days
        else:
            days = min(DAYS_AGO_MAX, max(1, math.ceil((now - wm).total_seconds() / 86400) + 1))
        picks.append((key, name, days, limit))
        remaining -= limit
    if not picks:
        return HirebasePlan([], month_used, usable_left, 0, "nothing_eligible")
    return HirebasePlan(picks, month_used, usable_left, usable_left - remaining, "", pages)


# ---- Company Search (find the real company row; TASK-192) ------------------------------------
# WHAT IS MEASURED AND WHAT IS NOT. Everything about this endpoint comes from the Hirebase docs
# page read 2026-10-27, NOT from a real call we made: POST /v2/hirebase/companies/search, header
# x-api-key; unknown body fields return 422; optional body fields query, company_name ("exact or
# partial"), keywords, company_link, parent_company, page, limit (default 10, max 100), sort_by;
# there is no slug filter and no job-count field; response companies[] with company_name,
# company_slug, company_link, aliases, parent_company, subsidiaries, linkedin_link, score, plus
# total_count, page, limit, total_pages; billed as m_company_api_calls, 1 unit per company row
# returned. Kevin's 7 of 50 after three runs at limit 3 fits per-row billing but one test cannot
# separate it from per-call billing. The first real run is the measurement; TASK-194 logs the
# meter delta. MEASURED by Kevin 2026-10-27: "corewell" returned the subsidiary "Corewell Health
# Urgent Care" as row 1, so row 1 is not always the parent.
COMPANY_SEARCH_URL = "https://api.hirebase.org/v2/hirebase/companies/search"  # docs, not measured by us
COMPANY_LOOKUP_LIMIT = 2     # DESIGN CHOICE
COMPANY_MONTHLY_UNITS = 24   # DESIGN CHOICE: about half the Free plan's 50, leaving the rest for Kevin's own Playground use
COMPANY_LOOKUPS_PER_RUN = 3  # DESIGN CHOICE
COMPANY_RETRY_DAYS = 7       # DESIGN CHOICE
COMPANY_WINDOW_DAYS = 30

CompanyResult = namedtuple("CompanyResult", "status rows error")
CompanyLookupPlan = namedtuple("CompanyLookupPlan", "picks month_units_used usable_left skip_reason")


def build_company_search_body(name, limit=COMPANY_LOOKUP_LIMIT):
    """Exactly company_name, page, limit (unknown fields 422). Blank name -> None."""
    text = str(name or "").strip()
    if not text:
        return None
    return {"company_name": text, "page": 1, "limit": _clamp(limit, 1, COMPANY_LOOKUP_LIMIT)}


def fetch_company_rows(name, api_key, post=None, timeout=30):
    """ONE Company Search request. status "ok" (rows may be [] = honest no-match) or "errored".

    Errored: no key or blank name, a raising post, non-2xx, bad JSON, companies missing or not a
    list, or a non-empty list where no row has a non-empty string company_slug. error is the
    exception class name or the HTTP status only, never the key, URL, body or response text.
    """
    body = build_company_search_body(name)
    if body is None:
        return CompanyResult("errored", [], "blank name")
    if not api_key:
        return CompanyResult("errored", [], "no api key")
    if post is None:
        import requests
        post = requests.post
    try:
        resp = post(COMPANY_SEARCH_URL, json=body, headers={"x-api-key": api_key}, timeout=timeout)
    except Exception as exc:
        return CompanyResult("errored", [], type(exc).__name__)
    code = getattr(resp, "status_code", None)
    if not isinstance(code, int) or isinstance(code, bool) or not 200 <= code < 300:
        return CompanyResult("errored", [], "http status %s" % (code,))
    try:
        data = resp.json()
    except Exception as exc:
        return CompanyResult("errored", [], type(exc).__name__)
    companies = data.get("companies") if isinstance(data, dict) else None
    if not isinstance(companies, list):
        return CompanyResult("errored", [], "companies missing or not a list")
    rows = [r for r in companies if isinstance(r, dict)]
    if companies and not any(isinstance(r.get("company_slug"), str) and r["company_slug"].strip()
                             for r in rows):
        return CompanyResult("errored", [], "no row has a company_slug")
    return CompanyResult("ok", rows, "")


def _host(link):
    text = str(link or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    host = re.split(r"[/?#]", text, maxsplit=1)[0].split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def pick_company_row(rows, name, normalize, known_domain=None):
    """The one row that really is `name`, or None. Never falls back to row 1.

    Match: host of company_link equals known_domain, OR normalize(company_name) equals
    normalize(name), OR a string in aliases does. Malformed aliases are ignored. Several matching
    rows with different slugs are ambiguous -> None.
    """
    want = normalize(name)
    domain = _host(known_domain) if known_domain else ""
    hits = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        slug = row.get("company_slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        ok = bool(domain) and _host(row.get("company_link")) == domain
        if not ok and want and isinstance(row.get("company_name"), str):
            ok = normalize(row["company_name"]) == want
        if not ok and want and isinstance(row.get("aliases"), list):
            ok = any(isinstance(a, str) and normalize(a) == want for a in row["aliases"])
        if ok:
            hits.append(row)
    if len({h["company_slug"] for h in hits}) != 1:
        return None
    return hits[0]


def plan_company_lookups(candidates, ledger, now, *, per_run=COMPANY_LOOKUPS_PER_RUN,
                         monthly_units=COMPANY_MONTHLY_UNITS, retry_days=COMPANY_RETRY_DAYS,
                         limit=COMPANY_LOOKUP_LIMIT):
    """Which companies may get a Company Search lookup this run.

    candidates: (key, display, people), already filtered by the caller. ledger: (spent_at, units,
    status, company_key). month_units_used sums units over ok AND errored rows in the trailing 30
    days (a failed call may have billed). Each pick reserves `limit` units. A key with any ledger
    row in the last retry_days is skipped. Biggest network first, ties by key.

    ARITHMETIC on design constants (not measurements): 24 units a month at 2 rows per lookup is at
    most 12 lookups a month, 24 if every lookup returns one row. 160-180 no-board companies is many
    months, which is why TASK-194 only looks up companies whose job search came back empty, biggest
    network first.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window = timedelta(days=COMPANY_WINDOW_DAYS)
    retry = timedelta(days=retry_days)
    used = 0
    recent = set()
    for spent_at, units, status, key in ledger or []:
        dt = _plan_ts(spent_at, now)
        if status in ("ok", "errored") and now - dt <= window:
            try:
                used += max(0, int(units))
            except (ValueError, TypeError, OverflowError):
                used += monthly_units  # unparseable: assume the worst
        if now - dt < retry:
            recent.add(key)
    usable_left = max(0, monthly_units - used)
    cands = list(candidates or [])
    if not cands:
        return CompanyLookupPlan([], used, usable_left, "no candidates")
    eligible = [c for c in cands if c[0] not in recent]
    if not eligible:
        return CompanyLookupPlan([], used, usable_left, "all recently tried")
    eligible.sort(key=lambda c: (-(c[2] or 0), c[0]))
    picks, remaining = [], usable_left
    for key, display, people in eligible:
        if len(picks) >= per_run or remaining < limit:
            break
        picks.append((key, display, people))
        remaining -= limit
    if not picks:
        return CompanyLookupPlan([], used, usable_left, "monthly cap")
    return CompanyLookupPlan(picks, used, usable_left, "")
