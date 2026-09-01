"""Pipeline helpers - mostly pure/dependency-free (no Flask/DB calls), unit-tested in isolation.

Split out of main.py so the scoring/dedup/dork/formatting logic can be unit-tested in isolation
and so main.py itself shrinks toward being just orchestration (routes, DB, CRM, Telegram, AI calls).

Exception: resolve_email_waterfall() below does live network I/O (Hunter.io/Prospeo/GetProspect) -
it lives here for architectural cohesion with the rest of the outreach-resolution helpers, but it
is not covered by the no-network guarantee the rest of this module provides.
"""
import hashlib
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone

import requests


def build_apollo_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', "", str(company_name or "")).strip()
    encoded = urllib.parse.quote(f"{clean_company} Operations")
    return f"https://app.apollo.io/#/people?qKeywords={encoded}"


def build_linkedin_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', "", str(company_name or "")).strip()
    encoded = urllib.parse.quote(f'{clean_company} ("VP" OR "Director" OR "Manager") ("Operations" OR "Compliance")')
    return f"https://www.linkedin.com/search/results/people/?keywords={encoded}"


def _strip_legal_suffixes(company_name):
    """Strip common legal-entity suffixes (Inc, LLC, Corp, Holdings, etc.) and punctuation noise
    so decision-maker dorks never search on a garbled/truncated company name.
    """
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', str(company_name or '')).strip()
    clean = re.sub(r'\b(inc|llc|ltd|corp|corporation|co|holdings|plc|group)\b', '', clean, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', clean).strip()


def build_hiring_manager_dork(company_name, job_title=""):
    """Google dork to surface a company's Head/Director/VP of Operations or COO on LinkedIn."""
    clean_comp = _strip_legal_suffixes(company_name)
    query = f'site:linkedin.com/in "{clean_comp}" ("Head of Operations" OR "Director of Operations" OR "Operations Manager" OR "VP of Operations" OR "COO")'
    return f"https://www.google.com/search?q={urllib.parse.quote(query)}"


def build_recruiter_dork(company_name):
    """Google dork targeting in-house talent acquisition for the company on LinkedIn."""
    clean_comp = _strip_legal_suffixes(company_name)
    query = f'site:linkedin.com/in "{clean_comp}" ("Technical Recruiter" OR "Talent Acquisition" OR "Senior Recruiter" OR "Corporate Recruiter")'
    return f"https://www.google.com/search?q={urllib.parse.quote(query)}"


def build_alumni_dork(company_name, school="Hope College"):
    """Google dork to surface shared-alma-mater employees at a target company on LinkedIn."""
    clean_comp = re.sub(r'[^a-zA-Z0-9\s]', '', str(company_name or '')).strip()
    clean_school = re.sub(r'[^a-zA-Z0-9\s]', '', str(school or '')).strip()
    query = f'site:linkedin.com/in "{clean_comp}" "{clean_school}"'
    return f"https://www.google.com/search?q={urllib.parse.quote(query)}"


def normalize_priority_value(raw_value):
    """Normalize free-text ("High"/"Medium"/"Low") or numeric 1-10 priority values into an int 1-10
    (10 = highest priority). Mirrors Code.gs's mapPriorityValue() but keeps a direct (non-inverted)
    scale so it can drive the Dynamic Contact Quality Multiplier's score boost. Defaults to 5.
    """
    text = str(raw_value or "").strip()
    lower = text.lower()
    if "high" in lower:
        return 9
    if "medium" in lower:
        return 5
    if "low" in lower:
        return 2
    match = re.search(r'\d+', text)
    if match:
        try:
            return max(1, min(10, int(match.group())))
        except (ValueError, TypeError):
            return 5
    return 5


def calculate_followup_interval(priority_score):
    try:
        p = float(priority_score)
        return max(3, int(round(35.0 - (p * 3.2))))
    except Exception:
        return 14


def resolve_smart_target_tab(source_tab, direction):
    """Smart auto-routing for /warm, /cold, /x: Carmen-family tabs stay in the Carmen pipeline;
    Tetiana-family tabs, "Clavicular" (warm-referral ATS matches), and "Pipeline_Candidates" (the
    pre-CRM staging tab for fresh job cards) all route through the Tetiana pipeline. direction is
    "warm", "cold", or "kill" (e.g. Clavicular + "kill" -> "Died").
    """
    is_carmen = str(source_tab or "").startswith("Carmen")
    if direction == "kill":
        return "Killed" if is_carmen else "Died"
    if direction == "warm":
        return "Carmen Warm" if is_carmen else "Tetiana Warm"
    return "Carmen Cold" if is_carmen else "Tetiana Cold"


def enforce_sentence_limit(text, max_sentences):
    """Truncate text to at most max_sentences sentences."""
    sentences = [s for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s]
    return ' '.join(sentences[:max_sentences])


def get_fit_score_indicator(score):
    if score >= 80:
        return "🟢"
    elif score >= 65:
        return "🟡"
    return "🔴"


def generate_dedup_hash(company, title):
    """Legal-suffix-aware so 'Acme Corp' and 'Acme Corp Inc.' postings dedup as the same company."""
    clean_company = _strip_legal_suffixes(company).lower()
    clean_title = str(title or "").lower().strip()
    return hashlib.md5(f"{clean_company}_{clean_title}".encode()).hexdigest()


# Filler / legal-entity tokens dropped from both halves of a dedup key so "AAA, Inc." / "aaa"
# and "The Blue Chip Co." / "Blue Chip" collapse to the same key.
_DEDUP_STOP_TOKENS = {"inc", "llc", "corp", "co", "ltd", "the"}


def normalize_dedup_key(company, role):
    """Canonical key for spotting a JOBS row logged twice (same Company + Role). Lowercases,
    replaces punctuation with spaces, collapses internal whitespace, and drops the filler tokens
    in _DEDUP_STOP_TOKENS from each half. Returns "<company>|<role>"; empty/None inputs yield "|".

    Mirrored by Code.gs's normalizeDedupKey() - keep the two in sync (used by dedupeJobsTabs and
    the in-append dedup guard).
    """
    def _clean(part):
        spaced = re.sub(r'[^a-z0-9\s]', ' ', str(part or "").lower())
        tokens = [t for t in spaced.split() if t and t not in _DEDUP_STOP_TOKENS]
        return " ".join(tokens)

    return f"{_clean(company)}|{_clean(role)}"


# Canonical Status vocabulary, ordered from earliest pipeline stage to latest. This is the
# single source of truth for Status ordering; Code.gs mirrors it as STATUS_VOCAB / statusRank().
STATUS_VOCAB = ["Matched", "Applied", "Replied", "Screening", "Interviewing", "Offer", "Rejected"]


def status_rank(value):
    """0-based ordinal of `value` within STATUS_VOCAB (case-insensitive, surrounding whitespace
    tolerated); -1 for anything unrecognized (blank, None, typo, pre-migration free text).
    """
    needle = str(value or "").strip().lower()
    for idx, canonical in enumerate(STATUS_VOCAB):
        if canonical.lower() == needle:
            return idx
    return -1


# ==============================================================================
# FOLLOW-UP SEQUENCER POLICY (pure, deterministic, no I/O)
#
# Decides the single thing that should happen to a JOBS row today from four inputs
# that already exist on the row - Status (Col F), Date Added (Col A), Next Followup
# Date (Col G) - plus today's date. No schema change: nothing new is stored.
# ==============================================================================

# ------------------------------------------------------------------------------
# TUNABLE CADENCE KNOBS (days). Single source of truth for the sequencer's timing -
# change them here and both the pure policy (followup_action) and the nightly job
# (main.run_followup_sequencer, which imports these names) pick the new values up.
# Nothing on the Code.gs / Sheet side needs to change: all sequencing state is
# derived Python-side from Status, Date Added and Next Followup Date.
#
# All four are counted from the anchor = followup_anchor() = Date Added (Col A).
# Constraints when retuning:
#   * 0 < FOLLOWUP_1_DAYS < FOLLOWUP_2_DAYS < FOLLOWUP_BURY_DAYS  (strictly increasing -
#     the job pushes Next Followup Date to the next boundary, so out-of-order values
#     would skip or repeat a step).
#   * STALE_HOT_DAYS is independent (it only gates the read-only stale_nudge on hot
#     statuses, which never auto-bury).
# ------------------------------------------------------------------------------
FOLLOWUP_1_DAYS = 4       # Applied + no reply -> follow-up #1 becomes due at anchor + 4d
FOLLOWUP_2_DAYS = 9       # Applied + no reply -> follow-up #2 becomes due at anchor + 9d
FOLLOWUP_BURY_DAYS = 16   # Applied + no reply -> auto-bury as ghosted at anchor + 16d
STALE_HOT_DAYS = 5        # Replied/Screening/Interviewing untouched > 5d -> stale_nudge

# Every value followup_action() can return.
FOLLOWUP_ACTIONS = ("none", "send_followup_1", "send_followup_2", "bury_ghosted", "stale_nudge")

# Code.gs's get_followups read path emits this for a blank Next Followup Date cell
# (see formatFollowupDate); treat it as "unset", not as a real 1970 date. The coercion is
# load-bearing on the Apps Script side - it keeps `new Date(...)` in the overdue sort from
# returning NaN on an empty string - so Python is where it has to be translated back.
FOLLOWUP_BLANK_DATE_SENTINEL = "1970-01-01"


def is_followup_unscheduled(value):
    """True when a Next Followup Date is blank or carries Code.gs's blank-date sentinel.

    An unscheduled record is NOT overdue - it was never given a date to be late against.
    Carmen Warm is personal networking contacts and almost none of them are dated, so
    reading the sentinel as a real 1970 due date flagged that whole tab as permanently
    overdue and buried the handful of records that were genuinely due.
    """
    return str(value or "").strip()[:10] in ("", FOLLOWUP_BLANK_DATE_SENTINEL)


def _parse_sequencer_date(value):
    """Lenient 'YYYY-MM-DD' -> datetime.date, or None for blank / the blank-date sentinel /
    anything unparseable. Only the first 10 chars are read, so an ISO datetime works too."""
    text = str(value or "").strip()[:10]
    if is_followup_unscheduled(text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def followup_anchor(date_added, next_followup):
    """The stable day-0 the +4/+9/+16 windows count from.

    Deliberately prefers Date Added over Next Followup Date: the nightly job pushes Next
    Followup Date forward every time it queues a bump, and re-anchoring on that moving value
    would make the windows drift and re-fire follow-up #1 indefinitely. Next Followup Date is
    only the fallback for when Date Added is blank/malformed. Returns a datetime.date or None.
    """
    return _parse_sequencer_date(date_added) or _parse_sequencer_date(next_followup)


def followup_action(status, date_added, next_followup, today):
    """Pure: the one thing that should happen to a JOBS row today. Side-effect free.

    Returns one of FOLLOWUP_ACTIONS:
      - "send_followup_1" / "send_followup_2": Applied, no reply, in the +4..+9 / +9..+16 window
      - "bury_ghosted": Applied, no reply, >= +16 days -> move to Died + note the reason
      - "stale_nudge": Replied/Screening/Interviewing untouched > STALE_HOT_DAYS (never auto-buried)
      - "none": nothing due

    Anchor = followup_anchor(date_added, next_followup) (Date Added, stable). A row whose Next
    Followup Date is still in the future is always left alone - that covers a manual /f snooze
    and the nightly job's own re-fire guard. `status` is matched case-insensitively against
    STATUS_VOCAB; unrecognized / blank / legacy free-text Status -> "none" (never touched).

    `today` is a datetime.date (a datetime is accepted and coerced). `date_added` / `next_followup`
    are 'YYYY-MM-DD' strings; blank or the '1970-01-01' sentinel both mean unset.
    """
    if isinstance(today, datetime):
        today = today.date()

    rank = status_rank(status)
    if rank == -1:
        return "none"
    canonical = STATUS_VOCAB[rank]

    nf = _parse_sequencer_date(next_followup)
    if nf is not None and nf > today:
        return "none"

    if canonical in ("Matched", "Offer", "Rejected"):
        return "none"

    anchor = _parse_sequencer_date(date_added) or nf
    if anchor is None:
        return "none"
    days = (today - anchor).days

    if canonical in ("Replied", "Screening", "Interviewing"):
        return "stale_nudge" if days > STALE_HOT_DAYS else "none"

    # canonical == "Applied"
    if days >= FOLLOWUP_BURY_DAYS:
        return "bury_ghosted"
    if days >= FOLLOWUP_2_DAYS:
        return "send_followup_2"
    if days >= FOLLOWUP_1_DAYS:
        return "send_followup_1"
    return "none"


# ==============================================================================
# OUTREACH VOICE LINTER (pure, no I/O)
#
# One rulebook for every candidate-facing sentence, whichever path renders it:
# the templates/*.json banks the Telegram card interpolates, and the Gmail bodies
# main.generate_cold_email()/generate_warm_email()/generate_bump_email() return.
# Both are linted by the same tests, which is what stops the two paths drifting
# back apart. Also runs on /edit so a phone-typed template gets a warning
# (never a block - Kevin has to be able to override from Telegram).
#
# Two tiers: lint_outreach_template() returns hard violations, advise_outreach_template()
# returns style nudges. Only the hard tier is asserted on in the tests, because a rule
# that would force a rewrite of copy that already reads well is a bad rule.
# ==============================================================================

# (compiled pattern, human-readable violation message). Patterns are matched
# case-insensitively against the rendered text.
_OUTREACH_BANNED_PATTERNS = [
    (r"best regards", "'Best regards' - use 'Thanks,' or 'Best,'"),
    (r"\balign\w*\b", "'align/alignment' - name the work instead"),
    (r"\bfits?\b(?!\s+(?:in|into)\b)", "'fit' as a skills claim - name the work instead"),
    (r"hope (?:you|things) (?:are|have been|'ve been|is)[^.]{0,20}\bwell\b", "'hope you have been doing well' filler opener"),
    (r"\bhi there\b", "'Hi there' - the {name} placeholder renders a bare 'Hi,' when the name is unknown"),
    (r"\b(?:leverag|utiliz|spearhead|synerg|optimiz)\w*\b", "corporate verb (leverage/utilize/spearhead/synergy/optimize)"),
    (r"looking forward to hearing", "'Looking forward to hearing from you'"),
    (r"\b(?:truly|deeply|highly|significantly)\b", "filler adverb (truly/deeply/highly/significantly)"),
    (r"\b(?:furthermore|additionally|moreover)\b", "essay transition (Furthermore/Additionally/Moreover)"),
    (r"proven track record", "self-praise ('proven track record')"),
    (r"my (?:experience|background) (?:centers|is in|lies)", "abstract capability claim - use past-tense proof"),
    (r"what you(?:'re| are) looking for", "self-deprecating hedge"),
    (r"if you think I", "self-deprecating hedge"),
    (r"\b(?:excited|thrilled|exciting|admire|impressive)\b", "performed enthusiasm / company praise"),
    (r"\b(?:mission|culture|rapid growth)\b", "praise for the company's mission/culture/growth"),
    (r"\b(?:five|ten|fifteen|thirty|5|10|15|30)[- ]minutes?\b", "timeboxed ask - use 'a quick chat' / 'a few minutes'"),
]

_OUTREACH_BANNED_RULES = [(re.compile(p, re.IGNORECASE), msg) for p, msg in _OUTREACH_BANNED_PATTERNS]

# A contraction usually makes the copy read like a person wrote it, but it is a style
# preference, not a rule: cold_ops[2] and followup_bumps[0] are among the strongest
# entries in the bank and have no natural place for an apostrophe, and wedging one in
# makes them worse. So this is reported by advise_outreach_template() as a nudge and is
# never a violation. An explicit list rather than a generic apostrophe search, so a
# possessive ("my team's plate") never counts as a contraction.
_CONTRACTION_RE = re.compile(
    r"\b(?:I'm|I've|I'd|I'll|you're|you've|you'd|you'll|we're|we've|we'd|they're|it's|that's|there's|"
    r"here's|let's|isn't|aren't|wasn't|don't|doesn't|didn't|can't|won't|wouldn't|couldn't|shouldn't|"
    r"haven't|hasn't|hadn't)\b".replace("'", "['’]"),
    re.IGNORECASE,
)

OUTREACH_EMAIL_WORD_CAP = 75
OUTREACH_LINKEDIN_CHAR_CAP = 220


def lint_outreach_template(text, kind="email"):
    """Returns a list of hard rule-violation strings for one rendered outreach string, empty if clean.

    Hard rules only - things that are wrong however good the copy is: banned phrases,
    punctuation sanitize_text() would eat, the {name} spacing bug, and the length caps.
    Style preferences live in advise_outreach_template() so they can never fail a template.

    `kind` is "email" (cold_ops / warm_alumni / followup_bumps, 75-word cap) or "linkedin"
    (linkedin_templates, 220-char cap). Measure AFTER interpolation - the caps are on what the
    recipient actually reads, not on the template with its placeholders still in it.

    Punctuation rules exist because main.sanitize_text() *deletes* em/en-dashes, colons and
    semicolons rather than rewriting around them, so "Hi Dana - saw the role" silently ships as
    "Hi Dana saw the role". Lint the raw template for those; the banned-phrase and length rules
    hold on the sanitized render too.
    """
    body = str(text or "")
    violations = []

    for pattern, message in _OUTREACH_BANNED_RULES:
        match = pattern.search(body)
        if match:
            violations.append(f"banned phrase {match.group(0)!r} ({message})")

    if re.search(r"[—–]", body):
        violations.append("em/en-dash - sanitize_text() deletes it, joining the two clauses")
    if ":" in body:
        violations.append("colon - sanitize_text() deletes it, joining the two clauses")
    if ";" in body:
        violations.append("semicolon - sanitize_text() deletes it, joining the two clauses")
    if "!" in body:
        violations.append("exclamation point")

    if " {name}" in body:
        violations.append("space before {name} - the placeholder supplies its own leading space, "
                          "so write 'Hi{name},' not 'Hi {name},'")

    if kind == "linkedin":
        if len(body) > OUTREACH_LINKEDIN_CHAR_CAP:
            violations.append(f"{len(body)} chars, over the {OUTREACH_LINKEDIN_CHAR_CAP}-char LinkedIn note cap")
    else:
        words = len(body.split())
        if words > OUTREACH_EMAIL_WORD_CAP:
            violations.append(f"{words} words, over the {OUTREACH_EMAIL_WORD_CAP}-word cold email cap")

    return violations


def advise_outreach_template(text, kind="email"):
    """Soft style notes for one rendered outreach string - a nudge, never a failure.

    Kept separate from lint_outreach_template() so /edit can surface both while the test
    suite only holds the shipped banks to the hard rules. A note here is a suggestion to
    read the line again, not a defect: ignoring it is a legitimate call.
    """
    body = str(text or "")
    notes = []

    if not _CONTRACTION_RE.search(body):
        notes.append("no contractions - \"I've\"/\"I'm\"/\"you're\" read warmer than \"I have\"/\"I am\", "
                     "but leave it alone if the line has no natural place for one")

    return notes


def generate_short_key(raw_id, fallback=None):
    """fallback replaces time.time() as the entropy source when raw_id is falsy, keeping this pure."""
    return hashlib.md5(str(raw_id or fallback or "0").encode()).hexdigest()[:12]


def parse_posted_hours(posted_utc_str):
    if not posted_utc_str:
        return 48
    try:
        dt = datetime.fromisoformat(str(posted_utc_str).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return int((now - dt).total_seconds() / 3600)
    except Exception:
        return 48


def get_age_badge(posted_hours):
    if posted_hours < 24:
        return "🔥 [< 24h FRESH]"
    elif posted_hours < 72:
        return "⚡ [1-3d RECENT]"
    elif posted_hours < 168:
        return "🟢 [3-7d ACTIVE]"
    elif posted_hours < 336:
        return "🟡 [7-14d AGING]"
    else:
        return "🔴 [14-30d STALE]"


def extract_salary(job):
    try:
        min_sal = float(job.get("job_min_salary") or 0)
        max_sal = float(job.get("job_max_salary") or 0)
        curr = str(job.get("job_salary_currency") or "USD")
        period = str(job.get("job_salary_period") or "year").lower()
        if "hour" in period or period == "hr":
            min_sal = min_sal * 2080
            max_sal = max_sal * 2080
            period = "year"
        if min_sal and max_sal:
            return f"${min_sal:,.0f} - ${max_sal:,.0f} {curr}/{period}", max_sal
        elif min_sal or max_sal:
            val = min_sal or max_sal
            return f"${val:,.0f} {curr}/{period}", val
    except Exception:
        pass
    return "Salary Unlisted", 0


def extract_work_style(job):
    desc = str(job.get("job_description") or "").lower()
    is_remote = job.get("job_is_remote", False) or "remote" in desc[:300] or "work from home" in desc[:300]
    if "hybrid" in desc:
        return "Hybrid"
    elif is_remote:
        return "Remote"
    return "On-Site / Unspecified"


def compute_description_simhash(text: str) -> str:
    """Computes a normalized SimHash token on the core job description."""
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', str(text or "")[:400].lower())
    tokens = clean.split()
    if not tokens:
        return hashlib.md5(b"").hexdigest()
    # Normalize 3-grams to catch reworded titles with identical bodies
    shingles = [" ".join(tokens[i:i+3]) for i in range(max(1, len(tokens)-2))]
    return hashlib.md5("".join(sorted(shingles)).encode()).hexdigest()


def resolve_email_waterfall(full_name, company_name, domain_hint=None, on_provider_attempt=None):
    """Cascading email discovery for a named contact: Hunter.io -> Prospeo -> GetProspect ->
    deterministic guess. Tries each configured provider in order and returns the first hit
    immediately (early-exit, no downstream providers are called once a match is found); falls
    back to a flagged best-guess address if no provider is configured or none finds a match.
    on_provider_attempt(provider_name), if given, fires once per completed provider request
    (whether or not it found an email) so the caller can track local monthly usage in its own DB.
    """
    domain = domain_hint or (re.sub(r'\s+', '', str(company_name or '').lower()) + ".com")
    parts = str(full_name or "").strip().split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""

    hunter_key = os.environ.get("HUNTER_API_KEY")
    if hunter_key:
        try:
            res = requests.get(
                "https://api.hunter.io/v2/email-finder",
                params={"domain": domain, "first_name": first, "last_name": last, "api_key": hunter_key},
                timeout=10
            )
            if on_provider_attempt:
                on_provider_attempt("hunter")
            email = res.json().get("data", {}).get("email")
            if email:
                return email
        except Exception as e:
            logging.error(f"Hunter.io email-finder failed ({domain}): {e}")

    prospeo_key = os.environ.get("PROSPEO_API_KEY")
    if prospeo_key:
        try:
            res = requests.post(
                "https://api.prospeo.io/email-finder",
                json={"first_name": first, "last_name": last, "company": domain},
                headers={"X-KEY": prospeo_key},
                timeout=10
            )
            if on_provider_attempt:
                on_provider_attempt("prospeo")
            email = (res.json().get("response") or {}).get("email")
            if email:
                return email
        except Exception as e:
            logging.error(f"Prospeo email-finder failed ({domain}): {e}")

    getprospect_key = os.environ.get("GETPROSPECT_API_KEY")
    if getprospect_key:
        try:
            res = requests.get(
                "https://api.getprospect.com/public/v1/email/find",
                params={"apikey": getprospect_key, "domain": domain, "first_name": first, "last_name": last},
                timeout=10
            )
            if on_provider_attempt:
                on_provider_attempt("getprospect")
            email = res.json().get("email")
            if email:
                return email
        except Exception as e:
            logging.error(f"GetProspect email-finder failed ({domain}): {e}")

    if first and last:
        return f"{first.lower()}.{last.lower()}@{domain} [⚠️ Unverified]"
    return f"operations@{domain} [⚠️ Fallback]"


def derive_job_source(job_id):
    """Classify a job's origin from its job_id prefix for source-level outcome attribution.
    Returns one of: greenhouse, lever, ashby, manual_ingest, jsearch (default, no known prefix).
    """
    job_id = str(job_id or "")
    if job_id.startswith("gh_"):
        return "greenhouse"
    if job_id.startswith("lever_"):
        return "lever"
    if job_id.startswith("ashby_"):
        return "ashby"
    if job_id.startswith("ingest_"):
        return "manual_ingest"
    return "jsearch"


def is_unverified_email(email_str):
    """True if an email string carries an [\u26a0\ufe0f Unverified]/[\u26a0\ufe0f Fallback ...] tag from
    resolve_email_waterfall() or resolve_target_email(), meaning it's a best-guess, not a confirmed hit.
    """
    return "[\u26a0\ufe0f" in str(email_str or "")

