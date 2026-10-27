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


def build_search_body(slugs, days_ago, limit):
    """Search body. Exact company_slugs only; no title or exclusion filter (none is known to exist)."""
    return {
        "company_slugs": list(slugs),
        "geo_locations": [{"region": "Michigan"}],
        "days_ago": _clamp(days_ago, 1, DAYS_AGO_MAX),
        "page": 1,
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


def job_belongs_to(job, company_name, slugs, normalize):
    """True only when company_slug is exactly one of `slugs` AND the normalized job company name
    equals the normalized warm company name (strict: siblings like Haworth International fail)."""
    if not isinstance(job, dict):
        return False
    slug = job.get(JOB_KEYS["company_slug"])
    if not isinstance(slug, str) or slug not in set(slugs or ()):
        return False
    target = _squash(normalize(company_name))
    theirs = job.get(JOB_KEYS["company_name"])
    if not target or not isinstance(theirs, str):
        return False
    return _squash(normalize(theirs)) == target


def split_jobs(jobs, company_name, slugs, normalize):
    """One company per call -> (kept, stats); stats = returned / malformed / kept / unmatched."""
    kept = []
    stats = {"returned": 0, "malformed": 0, "kept": 0, "unmatched": 0}
    for job in jobs or []:
        stats["returned"] += 1
        parsed = parse_job(job)
        if parsed is None:
            stats["malformed"] += 1
        elif job_belongs_to(job, company_name, slugs, normalize):
            kept.append(parsed)
            stats["kept"] += 1
        else:
            stats["unmatched"] += 1
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
    return HirebaseResult("ok", jobs, total, total > len(jobs), "")


HIREBASE_MONTHLY_JOBS = 500      # MEASURED: Free plan cap, job rows returned per billing month
# DESIGN CHOICE, not a measurement: the meter is account-wide, so Playground tests spend from the
# same pool and the ledger cannot see them.
HIREBASE_RESERVE_JOBS = 100
HIREBASE_COMPANIES_PER_RUN = 8   # DESIGN CHOICE, not a measurement
HIREBASE_LIMIT_PER_COMPANY = 25  # DESIGN CHOICE, not a measurement (Fifth Third alone was 38 MI jobs/7 days)
HIREBASE_MIN_LIMIT = 5           # DESIGN CHOICE, not a measurement: a pick under this is not made
HIREBASE_FIRST_DAYS = 14         # DESIGN CHOICE, not a measurement: window for a never-searched company
HIREBASE_RETRY_DAYS = 3          # DESIGN CHOICE, not a measurement: a recent ok search is skipped

HirebasePlan = namedtuple("HirebasePlan", "picks month_used usable_left reserved skip_reason")


def _plan_ts(ts, now):
    """Aware-UTC datetime; malformed or future timestamps count as `now`."""
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now if dt > now else dt


def plan_hirebase_run(candidates, ledger, now, per_run=HIREBASE_COMPANIES_PER_RUN,
                      monthly=HIREBASE_MONTHLY_JOBS, reserve=HIREBASE_RESERVE_JOBS,
                      limit_per_company=HIREBASE_LIMIT_PER_COMPANY, min_limit=HIREBASE_MIN_LIMIT,
                      first_days=HIREBASE_FIRST_DAYS, retry_days=HIREBASE_RETRY_DAYS):
    """Pure spend guard: which no-board companies to search, each with days_ago and limit.

    candidates: iterable of (company_key, display_name, contact_count)
    ledger: iterable of (iso_timestamp, jobs, status, company_key); status "ok", "capped" or
    "errored", one row per request. The sum of the picks' limits never exceeds usable_left, because
    the cap check uses the REQUESTED limit as worst case (per docs, NOT measured).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    month_used = 0
    last_ok = {}
    for ts, jobs, status, key in ledger:
        dt = _plan_ts(ts, now)
        try:
            n = max(0, int(jobs))
        except (ValueError, TypeError, OverflowError):
            n = monthly  # unparseable: assume the worst, never grant budget
        if (dt.year, dt.month) == (now.year, now.month):
            month_used += n  # errored rows count too: a failed request may have billed
        if status == "ok" and (key not in last_ok or dt > last_ok[key]):
            last_ok[key] = dt  # a capped row never advances the watermark
    usable_left = max(0, monthly - reserve - month_used)
    if usable_left < min_limit:
        return HirebasePlan([], month_used, usable_left, 0, "monthly_cap")
    retry_gap = timedelta(days=retry_days)
    eligible = []
    for key, name, contacts in candidates:
        wm = last_ok.get(key)
        if wm is not None and now - wm < retry_gap:
            continue
        eligible.append((key, name, contacts, wm))
    eligible.sort(key=lambda c: (-(c[2] or 0), c[0]))
    picks = []
    remaining = usable_left
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
    return HirebasePlan(picks, month_used, usable_left, usable_left - remaining, "")
