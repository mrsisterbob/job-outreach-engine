"""Pure helpers for the Apify career-site job feed (no I/O, no clock, no imports from main.py).

Builds the actor run input for a batch of warm companies, turns one raw feed row into the
JSearch-shaped posting dict /w already understands, and decides whether a row really belongs to a
given warm company.

REAL RISK: the ROW_KEYS below are only partly verified against live output. These tests pass on
hand-built rows that use the same guessed keys, so a wrong key name would still be green here.
TASK-176 turns an all-malformed batch into an error so wrong key names can never read as "no jobs".
"""
import re
from collections import namedtuple
from urllib.parse import urlparse

ACTOR_ID = "fantastic-jobs~career-site-job-listing-feed"  # the API form uses "~"; from the actor URL
START_FEE_USD = 0.10   # MEASURED: 4 runs matched to the cent (memory apify-feed-measured-facts.md)
PER_JOB_USD = 0.0025   # MEASURED: 4 runs matched to the cent
LIMIT_MIN = 200        # MEASURED: the actor refuses a lower limit
LIMIT_PER_COMPANY = 50  # NOT measured: three data points were 46, 81, 148 jobs per company; change freely
# Full strings: the exclusion appears to match whole words, so "CSR" alone would not remove "CSR/Teller".
# Not measured beyond that observation.
TITLE_EXCLUSIONS = ("(1099)", "Loan Officer", "CSR/Teller", "Teller", "Personal Banker")

# Our name -> the feed row key. MEASURED present: organization_url (null on Workday rows) and
# source_domain. NOT verified, recalled from the actor public output: id, title, organization,
# url, locations_derived (list of "City, State, Country" strings), date_posted, description_text.
ROW_KEYS = {
    "id": "id",
    "title": "title",
    "organization": "organization",
    "url": "url",
    "locations": "locations_derived",
    "date_posted": "date_posted",
    "description": "description_text",
    "organization_url": "organization_url",
    "source_domain": "source_domain",
}

_MI_NAMES = {"michigan", "mi"}


def build_run_input(company_names, limit):
    """Actor input for a batch: plain names only (a "Name:*" prefix pulled affiliates), no date key
    (the actor has none), limit clamped up to LIMIT_MIN."""
    return {
        "limit": max(int(limit or 0), LIMIT_MIN),
        "organizationSearch": [str(n) for n in company_names],
        "locationSearch": ["Michigan"],
        "titleExclusionSearch": list(TITLE_EXCLUSIONS),
    }


def _get(row, name):
    return row.get(ROW_KEYS[name]) if isinstance(row, dict) else None


_COUNTRIES = {"united states": "US", "usa": "US", "us": "US", "united states of america": "US"}


def _split_location(loc):
    """'City, State, Country' -> (city, state, country), tolerating short forms: 'Michigan' or 'MI'
    alone is a state; 'Michigan, United States' is state + country."""
    parts = [p.strip() for p in str(loc or "").split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        if parts[0].lower() in _MI_NAMES:
            return "", "MI", ""
        return parts[0], "", ""
    if len(parts) == 2 and parts[1].lower() in _COUNTRIES:
        country = _COUNTRIES[parts[1].lower()]
        if parts[0].lower() in _MI_NAMES:
            return "", "MI", country
        return parts[0], "", country
    city, state = parts[0], parts[1]
    country = parts[2] if len(parts) > 2 else ""
    if state.lower() == "michigan":
        state = "MI"
    country = _COUNTRIES.get(country.lower(), country)
    return city, state, country


def _pick_location(locations):
    """The Michigan filter leaks, so take the first Michigan entry; else the first entry; else empty."""
    if not isinstance(locations, (list, tuple)):
        return "", "", ""
    entries = [loc for loc in locations if isinstance(loc, str) and loc.strip()]
    if not entries:
        return "", "", ""
    for loc in entries:
        if _split_location(loc)[1] == "MI":
            return _split_location(loc)
    return _split_location(entries[0])


def parse_row(row):
    """One feed row -> JSearch-shaped posting dict, or None when id, title, organization or url is missing."""
    if not isinstance(row, dict):
        return None
    ident, title, org, url = (_get(row, k) for k in ("id", "title", "organization", "url"))
    for v in (ident, title, org):
        if isinstance(v, bool) or not isinstance(v, (str, int)) or not str(v).strip():
            return None
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        return None
    city, state, country = _pick_location(_get(row, "locations"))
    job = {
        "employer_name": str(org).strip(),
        "job_title": str(title).strip(),
        "job_id": "apify_" + str(ident).strip(),
        "job_apply_link": str(url).strip(),
        "job_description": str(_get(row, "description") or ""),
        "job_city": city,
        "job_state": state,
        "job_country": country,
        "apify_source_domain": _get(row, "source_domain"),
        "apify_organization_url": _get(row, "organization_url"),
    }
    posted = _get(row, "date_posted")
    if posted:
        job["job_posted_at_datetime_utc"] = str(posted)
    return job


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def row_belongs_to(row, company_name, normalize):
    """True only when the normalized organization equals the company (strict: siblings like Haworth
    International are not Haworth Inc.) AND the company compact name appears in source_domain or
    the organization_url host. A row with neither domain field never belongs."""
    if not isinstance(row, dict):
        return False
    target = normalize(company_name)
    if not target or _squash(normalize(_get(row, "organization"))) != _squash(target):
        return False
    words = target.split()
    # Full compact name, then leading words only ("Fifth Third Bank" -> fifththird, the Workday tenant).
    needles = {_squash("".join(words[:n])) for n in range(len(words), 0, -1)}
    needles = {x for x in needles if len(x) >= 4}
    if not needles:
        return False
    texts = []
    domain = _get(row, "source_domain")
    if domain:
        texts.append(str(domain))
    org_url = _get(row, "organization_url")
    if org_url:
        s = str(org_url)
        texts.append(urlparse(s if "//" in s else "//" + s).netloc or s)
    return any(x in _squash(t) for x in needles for t in texts)


def split_rows(rows, company_names, normalize):
    """-> (kept_by_company, stats). A row claimed by two companies is kept for neither (unmatched)."""
    kept = {}
    seen, names = set(), []
    for c in company_names:   # a company listed twice (or spelled two ways) must not own a row twice
        key = _squash(normalize(c))
        if key and key not in seen:
            seen.add(key)
            names.append(c)
    company_names = names
    stats = {"returned": 0, "malformed": 0, "kept": 0, "unmatched": 0}
    for row in rows or []:
        stats["returned"] += 1
        parsed = parse_row(row)
        if parsed is None:
            stats["malformed"] += 1
            continue
        owners = [c for c in company_names if row_belongs_to(row, c, normalize)]
        if len(owners) == 1:
            kept.setdefault(owners[0], []).append(parsed)
            stats["kept"] += 1
        else:
            stats["unmatched"] += 1
    return kept, stats


# ---------------------------------------------------------------------------
# Fetch: ONE batched call. Endpoint form is from Apify's public API docs, NOT measured here.
RUN_SYNC_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

FetchResult = namedtuple("FetchResult", "status rows capped error")


def get_token(environ):
    """Stripped APIFY_TOKEN from the given environment mapping, or None. Environment only: no default, no file."""
    tok = (environ.get("APIFY_TOKEN") or "").strip()
    return tok or None


def fetch_feed(run_input, token, http_post, timeout=240):
    """One batched feed call. status is "ok", "errored" or "unconfigured".

    "ok" with rows == [] means the feed does not cover these companies. It is NOT "no jobs".
    A failed call, non-2xx, non-list body, or a non-empty list whose rows ALL fail parse_row
    (a wrong ROW_KEYS guess) is "errored", never an empty ok. `error` carries only the
    exception class name or status code, never str(exc), a body, or the token.
    """
    if not token:
        return FetchResult("unconfigured", [], False, "no APIFY_TOKEN")
    url = RUN_SYNC_URL.format(actor=ACTOR_ID)
    try:
        resp = http_post(url, json=run_input, headers={"Authorization": "Bearer " + token}, timeout=timeout)
    except Exception as exc:
        return FetchResult("errored", [], False, "post raised " + type(exc).__name__)
    code = getattr(resp, "status_code", None)
    if not isinstance(code, int) or not 200 <= code < 300:
        return FetchResult("errored", [], False, "http status %s" % (code,))
    try:
        body = resp.json()
    except Exception as exc:
        return FetchResult("errored", [], False, "bad json " + type(exc).__name__)
    if not isinstance(body, list):
        return FetchResult("errored", [], False, "body is not a list")
    if body and not any(parse_row(r) for r in body):
        return FetchResult("errored", [], False, "all %d rows malformed" % len(body))
    limit = run_input.get("limit") if isinstance(run_input, dict) else None
    return FetchResult("ok", body, limit is not None and len(body) == limit, "")
