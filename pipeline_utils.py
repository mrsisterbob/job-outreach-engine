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
from datetime import datetime, timedelta, timezone

import requests


def build_apollo_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', "", str(company_name or "")).strip()
    encoded = urllib.parse.quote(f"{clean_company} Operations")
    return f"https://app.apollo.io/#/people?qKeywords={encoded}"


def build_linkedin_url(company_name):
    clean_company = re.sub(r'[^a-zA-Z0-9\s]', "", str(company_name or "")).strip()
    encoded = urllib.parse.quote(f'{clean_company} ("VP" OR "Director" OR "Manager") ("Operations" OR "Compliance")')
    return f"https://www.linkedin.com/search/results/people/?keywords={encoded}"


def build_linkedin_company_posts_url(company_name):
    """Best-effort LinkedIn company posts feed. The slug is guessed from the display name, so it
    404s for companies whose handle differs (Ford -> ford-motor-company) - fine for a manual click.
    """
    slug = re.sub(r'\s+', '-', _strip_legal_suffixes(company_name).lower())
    return f"https://www.linkedin.com/company/{urllib.parse.quote(slug)}/posts/?feedView=all"


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

# Fifth knob, counted from *today* rather than from followup_anchor(): when a verified inbound
# reply lands (main.check_inbound_gmail_replies), Next Followup Date is pushed to today + this
# many days. A live thread deserves a tighter loop than the anchor-based cadence above, so this
# is independent of the four constraints listed there - it only has to stay positive.
REPLY_FOLLOWUP_DAYS = 4   # Any verified inbound reply -> Next Followup Date = today + 4d

# Ceiling, not a cadence knob: a bury is two irreversible CRM writes (append_note + move to Died),
# and eligibility is purely date-driven - so a CRM left stale for a month makes the entire Applied
# backlog bury-eligible on the same pass, with no chance to intervene. Cap what one run may bury;
# the overflow stays unlogged and eligible, so re-running drains the rest a batch at a time.
MAX_AUTO_BURIES_PER_RUN = 10

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
    # Ban the vague ask, not the timeboxed one. The forensic analysis of the correct mailbox
    # (kjmiller406@gmail.com, 62 professional emails, May 2025 - Sep 2026) shows Kevin's real
    # habit is a specific, mostly round timebox - "10 minutes" x3, "15 minute call" x1, and zero
    # odd-minute asks. An earlier pass banned round numbers and pushed odd counts ("13 minutes",
    # "17 minutes"); that was calibrated on the wrong corpus (kevin.miller@hope.edu, a pre-job
    # college mailbox) and is reverted here. "quick chat" has zero uses in the professional
    # corpus, so that is what stays banned.
    (r"\bquick chat\b", "'quick chat' - vague ask with zero uses in the corpus; name a specific length like '10 minutes' or 'a brief call'"),
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


# ==============================================================================
# ATS AUTO-EXPANSION NAME GUARD (pure, no I/O)
#
# The Carmen Warm CRM holds personal networking contacts, not companies: the Company
# column really does contain "mom", "cousin", "Nathan at speaker event" and pasted
# LinkedIn profile URLs. auto_expand_ats_slug() probes Greenhouse, Lever and Ashby in
# sequence at up to 8s each, so every one of those costs 24 seconds and three log lines
# for a match that cannot exist.
#
# The two errors are not symmetric, so this guard is deliberately eager: a false skip
# loses one auto-discovered board, which Kevin can add by hand; a false probe costs 24
# seconds of pipeline time on every run, forever.
# ==============================================================================

# Rejected only as the WHOLE name, so the real company "Guy Carpenter" is still probed.
_NON_COMPANY_PERSON_WORDS = frozenset({
    "mom", "dad", "grandma", "grandpa", "cousin", "uncle", "aunt", "guy", "friend", "buddy",
})

# A pasted profile/company URL slugs into something meaningless ("httpswwwlinkedincomin...").
_NON_COMPANY_URL_MARKERS = ("http", "linkedin.com", "www.")

# Note text Kevin typed into the Company cell rather than a name. The connectives are
# space-padded so they match "Nathan at speaker event" but not "Atwell" or "Whom".
_NON_COMPANY_NOTE_MARKERS = ("?", "/", " at ", " from ", " who ")

# Below this, the slug is too short to be a real board (and covers blank/punctuation-only).
ATS_MIN_SLUG_LENGTH = 3


def ats_slug_guess(company_name):
    """The lowercase alphanumeric board slug auto_expand_ats_slug() probes for a company."""
    return re.sub(r"[^a-z0-9]", "", str(company_name or "").lower())


def is_probable_company_name(name):
    """False when a Carmen Warm 'company' is obviously a person or a note, not a company.

    Callers should treat False as "do not spend HTTP requests on this", never as a hard
    assertion about the string. See the block comment above for why this errs toward skipping.
    """
    text = str(name or "").strip()

    if len(ats_slug_guess(text)) < ATS_MIN_SLUG_LENGTH:
        return False

    lowered = text.lower()
    if any(marker in lowered for marker in _NON_COMPANY_URL_MARKERS):
        return False
    if lowered in _NON_COMPANY_PERSON_WORDS:
        return False
    # Real company names are capitalized; "cousin" and "(fuck)" are not. This is the rule
    # that catches lowercase junk no keyword list could enumerate.
    if not any(char.isupper() for char in text):
        return False
    if any(marker in lowered for marker in _NON_COMPANY_NOTE_MARKERS):
        return False

    return True


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



# ==============================================================================
# SENT-MAIL CONTACT CAPTURE (pure, no I/O)
#
# Every unique person Kevin emails at a company that already exists as a job in the
# CRM becomes a Carmen Cold row automatically. His Sent folder is the outreach log:
# the To: header carries the name, address and (via the domain) the company, so a
# contact costs zero typing. LinkedIn DMs are deliberately not a capture path - no
# server-readable record of one exists that does not require scraping.
# ==============================================================================

# Role/shared mailboxes are the job pipeline's own targets (resolve_target_email() invents
# these), already tracked as job rows. Carmen Cold is people, so they never capture here.
_ROLE_MAILBOX_LOCALPARTS = frozenset({
    "operations", "bizops", "wealthops", "compliance", "careers", "jobs", "recruiting",
    "recruitment", "talent", "hr", "people", "info", "hello", "contact", "support",
    "admin", "help", "sales", "team", "noreply", "no-reply", "donotreply",
})

# Mail providers and ATS/job-board senders: the domain says nothing about an employer,
# so company matching would be meaningless even when the local part is a real person.
_NON_COMPANY_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "aol.com", "icloud.com", "me.com", "msn.com", "proton.me", "protonmail.com", "gmx.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkday.com", "icims.com", "taleo.net",
    "smartrecruiters.com", "jobvite.com", "workable.com", "bamboohr.com", "indeed.com",
    "ziprecruiter.com", "linkedin.com", "glassdoor.com", "monster.com",
})


def parse_email_recipient(to_header):
    """Split one RFC-5322 To: value into (display_name, email). Returns (None, None) if no
    address is present. Only the FIRST address is read - a multi-recipient blast is not the
    one-to-one outreach this capture path is for, and the caller drops those.
    """
    raw = str(to_header or "").strip()
    match = re.search(r'([^<>,]*)<\s*([^<>@\s,]+@[^<>@\s,]+)\s*>', raw)
    if match:
        name = match.group(1).strip().strip('"').strip()
        return (name or None), match.group(2).strip().lower()
    bare = re.fullmatch(r'\s*([^<>@\s,]+@[^<>@\s,]+)\s*', raw)
    if bare:
        return None, bare.group(1).strip().lower()
    return None, None


def is_role_mailbox(email):
    """True for shared/role addresses (operations@, careers@, noreply@) - not a person."""
    local = str(email or "").split("@")[0].strip().lower()
    local = re.sub(r'[._-]?\d+$', '', local)
    return local in _ROLE_MAILBOX_LOCALPARTS


def company_domain_of(email):
    """The employer-bearing domain of an address, or '' for consumer mail and ATS senders.
    Strips one level of mail subdomain ('mail.crain.com' -> 'crain.com') so a corporate
    relay still matches the company it belongs to.
    """
    domain = str(email or "").split("@")[-1].strip().lower().strip(".")
    if not domain or "." not in domain:
        return ""
    if domain in _NON_COMPANY_EMAIL_DOMAINS:
        return ""
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in ("mail", "email", "careers", "jobs", "smtp", "mx"):
        domain = ".".join(parts[1:])
    return "" if domain in _NON_COMPANY_EMAIL_DOMAINS else domain


def domain_matches_company(email, company_name):
    """True if an address's domain plausibly belongs to `company_name`.

    Compares the domain's registrable label against the normalized company name with all
    non-alphanumerics removed, so "Signal Advisors" matches signaladvisors.com and
    "40 Acres" matches 40acres.com. Deliberately strict: a substring test would let
    'aa.com' match 'AAA-The Auto Club'.
    """
    domain = company_domain_of(email)
    if not domain:
        return False
    # Mirrors main.normalize_company_for_match() rather than importing it: pipeline_utils is the
    # pure layer and must not depend on main.
    normalized = str(company_name or "").strip().lower()
    normalized = re.sub(r'\b(inc|llc|ltd|corp|corporation|co|holdings|plc|group)\b\.?', '', normalized)
    label = re.sub(r'[^a-z0-9]', '', domain.split(".")[0])
    company = re.sub(r'[^a-z0-9]', '', normalized)
    if not label or not company:
        return False
    return label == company or (len(label) >= 5 and label in company) or (len(company) >= 5 and company in label)


def match_email_to_crm_company(email, crm_companies):
    """Return the CRM company name an address belongs to, or None.

    `crm_companies` is every company that has ever appeared as a job (any tab). This is the
    gate on the whole capture path: a person is only worth a Carmen Cold row when Kevin
    emailed them BECAUSE of a job he is tracking.
    """
    if not email or is_role_mailbox(email) or not company_domain_of(email):
        return None
    for company in crm_companies:
        if company and domain_matches_company(email, company):
            return company
    return None


def name_from_email_local_part(email):
    """Fall back to a display name derived from the address ('eina.assali@x' -> 'Eina Assali')
    when the To: header carried no display name.
    """
    local = str(email or "").split("@")[0].strip()
    local = re.sub(r'\d+$', '', local)
    words = [w for w in re.split(r'[._\-+]+', local) if w]
    return " ".join(w.capitalize() for w in words) if words else ""


def build_sent_contact(to_header, crm_companies):
    """One Sent message -> a Carmen Cold contact dict, or None when it should not capture.

    Returns {name, email, company} only for a one-to-one message to a real person at a
    company already tracked as a job. Everything else (role mailboxes, consumer domains,
    unknown companies) returns None and is skipped silently.
    """
    name, email = parse_email_recipient(to_header)
    if not email:
        return None
    company = match_email_to_crm_company(email, crm_companies)
    if not company:
        return None
    return {
        "name": name or name_from_email_local_part(email),
        "email": email,
        "company": company,
    }


def is_guessed_contact_email(email):
    """True when a JOBS row's Contact Email is a pipeline guess rather than a real person.

    resolve_target_email() invents `operations@`/`bizops@`/`wealthops@`/`compliance@` addresses
    from the company name and tags the worst ones "[⚠️ Fallback Email]". Those are placeholders,
    so they are the only values the sent-mail back-fill is allowed to overwrite - a real address
    (typed via /e, or already back-filled) must never be clobbered by a later message.
    """
    raw = str(email or "").strip()
    if not raw:
        return True
    if "Fallback Email" in raw:
        return True
    # Strip the bracketed confidence tag the same way the send path does before inspecting.
    bare = re.sub(r'\s*\[.*?\]\s*', '', raw).strip()
    return is_role_mailbox(bare)


def resolve_sent_email_backfill(to_header, job_rows):
    """One Sent message -> (sheet_uuid, real_email) for a JOBS row whose Contact Email is still
    a guess, or None.

    `job_rows` is an iterable of dicts as returned by get_followups: {sheet_uuid, company, email}.
    A row only qualifies when the recipient is a real person (not a role mailbox), the address's
    domain matches that row's company, and the row's current Contact Email is a placeholder.
    Rows already carrying a real address are left alone, which keeps the back-fill idempotent -
    rescanning the same Sent window twice is a no-op.
    """
    _, email = parse_email_recipient(to_header)
    if not email or is_role_mailbox(email) or not company_domain_of(email):
        return None
    for row in job_rows or []:
        uuid_value = str((row or {}).get("sheet_uuid") or "").strip()
        if not uuid_value:
            continue
        if not domain_matches_company(email, (row or {}).get("company")):
            continue
        if not is_guessed_contact_email((row or {}).get("email")):
            continue
        return uuid_value, email
    return None


# ==============================================================================
# CARMEN COLD 3/7/14 FOLLOW-UP LADDER (pure, no I/O)
#
# A networking contact gets three nudges at fixed offsets from the day they landed in
# Carmen Cold, then stops. Distinct from followup_action()'s JOBS windows (+4/+9/+16 with
# an auto-bury) because these are people: the ladder ends quietly rather than burying, and
# the cadence is tighter since a cold intro goes stale faster than a job application.
#
# Ladder position is read from the row itself, never from local state: the sequencer
# advances Next Followup Date along CARMEN_LADDER_DAYS, so the gap between the anchor and
# the scheduled date says which rung a row is on. That survives the SQLite wipe on every
# Render deploy, and it means a row dragged into Carmen Cold by hand enters the ladder on
# the next nightly pass with no trigger, no stamp, and nothing to configure.
# ==============================================================================

CARMEN_LADDER_DAYS = (3, 7, 14)


def carmen_ladder_rung(anchor, next_followup):
    """Which rung a Carmen Cold row currently sits on, from the gap between its anchor and
    its scheduled date. 0 = not yet scheduled, 1/2/3 = the 3/7/14-day nudges, 4 = ladder done.

    Tolerates drift: the sequencer can only advance a row on a day it actually runs, so a
    date a day or two past its nominal rung still reads as that rung rather than falling off.
    """
    if anchor is None or next_followup is None:
        return 0
    gap = (next_followup - anchor).days
    if gap <= 0:
        return 0
    for rung, offset in enumerate(CARMEN_LADDER_DAYS, start=1):
        if gap <= offset:
            return rung
    return len(CARMEN_LADDER_DAYS) + 1


def carmen_ladder_action(anchor, next_followup, today):
    """Pure: what a Carmen Cold row needs today. Side-effect free.

    Returns (action, next_date):
      ("schedule", d)  - undated row (incl. one just dragged in by hand): start the ladder at +3
      ("nudge_N", d)   - rung N is due: alert Kevin, advance to the next rung
      ("exhausted", None) - all three nudges sent; the row stops asking for attention
      ("none", None)   - scheduled for a future date, nothing to do
    """
    if anchor is None:
        return "none", None
    if next_followup is None:
        return "schedule", today + timedelta(days=CARMEN_LADDER_DAYS[0])
    if next_followup > today:
        return "none", None

    rung = carmen_ladder_rung(anchor, next_followup)
    if rung == 0:
        return "schedule", today + timedelta(days=CARMEN_LADDER_DAYS[0])
    if rung > len(CARMEN_LADDER_DAYS):
        return "exhausted", None
    if rung == len(CARMEN_LADDER_DAYS):
        return f"nudge_{rung}", None
    return f"nudge_{rung}", anchor + timedelta(days=CARMEN_LADDER_DAYS[rung])


def plan_carmen_followup(date_added, next_followup, today):
    """String-in wrapper over carmen_ladder_action() for raw CRM row values.

    An undated row anchors on `today` rather than being skipped - that is the manual-move case:
    a contact dragged into Carmen Cold carries no useful date, so the ladder starts when the
    sequencer first sees them.
    """
    anchor = _parse_sequencer_date(date_added)
    scheduled = None if is_followup_unscheduled(next_followup) else _parse_sequencer_date(next_followup)
    if anchor is None:
        anchor = today
    return carmen_ladder_action(anchor, scheduled, today)


# Days an untouched "Matched" pipeline row may sit in Tetiana Cold before the sequencer retires
# it to Died. followup_action() returns "none" for "Matched", so without this these rows never
# age out and the tab grows without bound - the real cause of a swarmed Tetiana Cold is
# accumulation over weeks, not the <=5 rows any single pipeline run writes.
MATCHED_EXPIRY_DAYS = 30


def is_expired_matched_row(status, date_added, today, expiry_days=MATCHED_EXPIRY_DAYS):
    """True for a JOBS row still sitting at 'Matched' (never applied to, never replied to)
    `expiry_days` or more after it was added.

    Only "Matched" expires: any other status means Kevin engaged with the row, and engaged rows
    are governed by followup_action()'s own windows. A row with no parseable Date Added never
    expires - better a stale row than silently retiring one whose date simply failed to parse.
    """
    if str(status or "").strip().lower() != "matched":
        return False
    anchor = _parse_sequencer_date(date_added)
    if anchor is None:
        return False
    return (today - anchor).days >= expiry_days
