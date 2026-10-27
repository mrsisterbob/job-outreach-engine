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
import re

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
