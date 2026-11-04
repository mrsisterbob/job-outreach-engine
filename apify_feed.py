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
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

ACTOR_ID = "fantastic-jobs~career-site-job-listing-feed"  # the API form uses "~"; from the actor URL
# Free plan (the only MEASURED numbers, 4 runs matched to the cent, memory apify-feed-measured-facts.md):
#   start fee 0.10, per job 0.0025.
# Starter plan below: Kevin-reported from the Starter pricing page, NOT verified by a billed run.
START_FEE_USD = 0.001  # $0.001 per run start (Starter)
PER_JOB_USD = 0.002    # $2.00 per 1,000 jobs (Starter)
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


def _note_dropped(dropped, name, reason):
    """Append (returned_name, reason) to the caller's list. Only a real list is appended to; anything else is ignored."""
    if isinstance(dropped, list):
        dropped.append((str(name or "").strip(), reason))


def split_rows(rows, company_names, normalize, dropped=None):
    """-> (kept_by_company, stats). A row claimed by two companies is kept for neither (unmatched).
    dropped: an optional caller-owned list that gets (returned_name, reason) for every dropped row: "no_match",
    "claimed_by_several", or ("", "malformed") when nothing could be read. Appending is the only mutation."""
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
            _note_dropped(dropped, "", "malformed")
            continue
        owners = [c for c in company_names if row_belongs_to(row, c, normalize)]
        if len(owners) == 1:
            kept.setdefault(owners[0], []).append(parsed)
            stats["kept"] += 1
        else:
            stats["unmatched"] += 1
            _note_dropped(dropped, parsed.get("employer_name"), "claimed_by_several" if len(owners) > 1 else "no_match")
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


# ---------------------------------------------------------------------------
# Spend guard: pure, no I/O, no clock reads (`now` is passed in). Modeled on
# warm_jsearch_plan.plan_warm_jsearch.
APIFY_COMPANIES_PER_RUN = 8  # DESIGN CHOICE, not a measurement: the size of Kevin's measured test batch
# DESIGN CHOICE, not a measurement: the worst case of one run at 8 companies (limit 400) is
# 0.001 + 0.002 * 400 = $0.801. NOT checked: whether the $19 plan includes $19 of usage credit, and the
# ledger only sees /w spend, not Kevin's console use.
APIFY_MONTHLY_CEILING_USD = 10.00
APIFY_COMPANY_RETRY_DAYS = 7  # DESIGN CHOICE, not a measurement: a re-search bills every old row again (no date filter)

ApifyPlan = namedtuple("ApifyPlan", "picks limit worst_case_usd month_spent_usd month_left_usd skip_reason")


def actual_cost(rows_returned):
    """Billed cost of a run (MEASURED formula): excluded rows are free, zero rows is the flat fee."""
    return START_FEE_USD + PER_JOB_USD * rows_returned


def _parse_ts(ts, now):
    """Aware-UTC datetime; malformed or future timestamps count as `now`."""
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now if dt > now else dt


def plan_apify_run(candidates, ledger, now, per_run=APIFY_COMPANIES_PER_RUN,
                   ceiling_usd=APIFY_MONTHLY_CEILING_USD, retry_days=APIFY_COMPANY_RETRY_DAYS):
    """Decide the one batched call: which companies, what limit, worst-case cost.

    candidates: iterable of (company_key, display_name, contact_count)
    ledger: iterable of (iso_timestamp, usd, status, [company_keys]); status "ok" or "errored"
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    retry_gap = timedelta(days=retry_days)
    month_spent = 0.0
    last_ok = {}
    for ts, usd, status, keys in ledger:
        dt = _parse_ts(ts, now)
        try:
            usd = max(0.0, float(usd))
        except (ValueError, TypeError):
            usd = float(ceiling_usd)  # unparseable: assume the worst, never grant budget
        if (dt.year, dt.month) == (now.year, now.month):
            month_spent += usd  # errored rows count too: a failed call may still have billed
        if status == "ok":
            for k in keys or ():
                if k not in last_ok or dt > last_ok[k]:
                    last_ok[k] = dt
    room = ceiling_usd - month_spent

    best = {}
    for c in candidates:
        if c[0] not in best or c[2] > best[c[0]][2]:
            best[c[0]] = c
    ordered = sorted(best.values(), key=lambda c: (-c[2], c[0]))
    eligible = [(k, n) for k, n, cnt in ordered
                if k not in last_ok or now - last_ok[k] >= retry_gap]
    picks = eligible[:max(0, per_run)]
    if not picks:
        return ApifyPlan([], 0, 0.0, month_spent, max(0.0, room), "nothing_eligible")

    while True:
        limit = max(LIMIT_MIN, LIMIT_PER_COMPANY * len(picks))
        worst = actual_cost(limit)
        if round(worst - room, 9) <= 0:
            return ApifyPlan(picks, limit, worst, month_spent, max(0.0, room - worst), "")
        if len(picks) > 1 and LIMIT_PER_COMPANY * (len(picks) - 1) >= LIMIT_MIN:
            picks = picks[:-1]  # drop the smallest company
            continue
        return ApifyPlan([], 0, 0.0, month_spent, max(0.0, room), "monthly_ceiling")


# ---------------------------------------------------------------------------
# Fallthrough: which no-board companies Hirebase could not answer. Pure, no I/O.
def fallthrough_candidates(candidates, searched, registry):
    """Subset of candidates ((key, display, people)), same order, that Hirebase could not answer.
    searched: key -> (outcome, returned, kept); registry: key -> (slug, status), taken AFTER the Hirebase block."""
    out = []
    for cand in candidates or []:
        key = cand[0]
        if key in searched:
            outcome, returned = searched[key][0], searched[key][1]
            if outcome in ("ok", "capped") and returned == 0:
                out.append(cand)
        elif key in registry:
            slug, status = registry[key]
            if status == "no_jobs" or (status == "unknown" and not slug):
                out.append(cand)
    return out


def domain_board(source_domain):
    """'workday' for a host ending .myworkdayjobs.com, else ''. Other boards' domains are unmeasured."""
    host = str(source_domain or "").strip().lower()
    return "workday" if host.endswith(".myworkdayjobs.com") else ""


def summarize_feed_sources(kept_by_company):
    """kept dict from split_rows -> display name -> {source_domain, job_board, rows}."""
    out = {}
    for name, jobs in (kept_by_company or {}).items():
        counts = {}
        for j in jobs:
            d = str(j.get("apify_source_domain") or "").strip()
            if d:
                counts[d] = counts.get(d, 0) + 1
        dom = min(counts, key=lambda d: (-counts[d], d)) if counts else ""
        out[name] = {"source_domain": dom, "job_board": domain_board(dom), "rows": len(jobs)}
    return out
