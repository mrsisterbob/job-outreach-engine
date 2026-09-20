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
    # Narrowed 2026-09-20 at Kevin's explicit direction. "my background is in" is now the shipped
    # cold_ops[1] opener; he chose it over the linter's objection after being shown the conflict.
    # "my experience centers/lies" and "my background centers/lies" stay banned.
    (r"my experience (?:centers|is in|lies)|my background (?:centers|lies)", "abstract capability claim - use past-tense proof"),
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

# Raised from 75 to 100 deliberately. The deferential register used for senior contacts -
# naming the application, crediting the recipient's vantage point, then asking - does not fit in
# 75 words, and the hand-written email this bank is modeled on runs 79. The cap exists to stop a
# cold email sprawling, not to force every note into the same clipped shape.
OUTREACH_EMAIL_WORD_CAP = 100
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


MIN_SIMHASH_TOKENS = 8


def compute_description_simhash(text: str) -> str:
    """Normalized SimHash token for a job description, or "" when the text cannot identify a job.

    Returning "" (rather than the MD5 of an empty string) is the whole point of this signature.
    The old version hashed every description-less posting to d41d8cd9... - the empty-string MD5 -
    so the FIRST such job saved that token to seen_content_hashes and every later one collided
    with it and was dropped, permanently and silently, across all future runs. Greenhouse returned
    no description at all until the content=true fix, and Workday's list endpoint still does before
    its detail fetch, so a single poisoned hash could bury an unbounded number of unrelated jobs.

    A too-short description is the same hazard in slower motion: two 3-word blurbs collide far more
    easily than two real postings, so anything under MIN_SIMHASH_TOKENS tokens is treated as
    unidentifiable too. Callers MUST treat "" as "no content signature - do not dedup on this".
    """
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', str(text or "")[:400].lower())
    tokens = clean.split()
    if len(tokens) < MIN_SIMHASH_TOKENS:
        return ""
    # Normalize 3-grams to catch reworded titles with identical bodies
    shingles = [" ".join(tokens[i:i+3]) for i in range(max(1, len(tokens)-2))]
    return hashlib.md5("".join(sorted(shingles)).encode()).hexdigest()


# Names the pipeline invents when it has no real contact. None of them is a person, so the
# person-finder providers can only miss on them.
_PLACEHOLDER_CONTACT_NAMES = frozenset({
    "operations lead", "operations", "hiring manager", "recruiter", "talent",
    "hiring team", "team", "contact", "unknown",
})


def _hunter_domain_search(domain, on_provider_attempt=None):
    """Hunter's domain-search: who is publicly listed at this company, when no NAME is known.

    The right endpoint for the no-name case. email-finder answers "what is THIS PERSON's address"
    and needs a real first/last; handed a placeholder like "Operations Lead" it searches for a
    human by that name, finds none, and reports no match - which is what made /eh look broken
    while quietly spending a credit per provider. Prefers a generic/role mailbox when Hunter
    flags one, else the highest-confidence personal address it lists.
    """
    hunter_key = os.environ.get("HUNTER_API_KEY")
    if not (hunter_key and domain):
        return None
    try:
        res = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": hunter_key, "limit": 10},
            timeout=10,
        )
        if on_provider_attempt:
            on_provider_attempt("hunter_domain")
        emails = (res.json().get("data") or {}).get("emails") or []
        if not emails:
            return None
        generic = [e for e in emails if e.get("type") == "generic" and e.get("value")]
        if generic:
            return generic[0]["value"]
        ranked = sorted(
            (e for e in emails if e.get("value")),
            key=lambda e: e.get("confidence") or 0,
            reverse=True,
        )
        return ranked[0]["value"] if ranked else None
    except Exception as e:
        logging.error(f"Hunter.io domain-search failed ({domain}): {e}")
    return None


def resolve_email_waterfall(full_name, company_name, domain_hint=None, on_provider_attempt=None):
    """Cascading email discovery: Hunter.io -> Prospeo -> GetProspect -> deterministic guess.
    Tries each configured provider in order and returns the first hit immediately (early-exit, no
    downstream providers are called once a match is found); falls back to a flagged best-guess
    address if no provider is configured or none finds a match.
    on_provider_attempt(provider_name), if given, fires once per completed provider request
    (whether or not it found an email) so the caller can track local monthly usage in its own DB.

    With no REAL name, the person-finder providers are skipped entirely in favour of Hunter's
    domain-search - see _hunter_domain_search. All three finders take a first/last name, so a
    placeholder ("Operations Lead", "Hiring Manager") guarantees three misses and a fallback
    guess, which is exactly the failure that made /eh appear dead.
    """
    domain = domain_hint or (re.sub(r'\s+', '', str(company_name or '').lower()) + ".com")
    parts = str(full_name or "").strip().split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""

    # A placeholder is not a person: go straight to domain-search rather than burning a credit
    # per provider looking for someone who does not exist.
    if not last or str(full_name or "").strip().lower() in _PLACEHOLDER_CONTACT_NAMES:
        found = _hunter_domain_search(domain, on_provider_attempt=on_provider_attempt)
        if found:
            return found

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

# Local parts that mean "a machine sent this", used ONLY to deny the Tier 1 interview bypass.
# Deliberately NOT _ROLE_MAILBOX_LOCALPARTS: that set contains careers@, recruiting@, talent@ and
# hr@, which are exactly the addresses a real interview invitation arrives from, and denying those
# would re-create the silent loss the bypass exists to prevent.
#
# Every entry here is a sender that announces automated bulk/transactional mail. The ones that
# actually reached Kevin's phone as "Interview Signal Detected": welcome@notify.chime.com,
# no-reply@usa.experian.com, azure@promomail.microsoft.com, support@turbotax.intuit.com.
_AUTOMATED_SENDER_LOCALPARTS = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply", "notify", "notifications",
    "welcome", "alerts", "alert", "updates", "news", "newsletter", "marketing", "promo",
    "promomail", "mailer", "mailer-daemon", "bounce", "bounces", "automated", "auto",
    "system", "notification", "account", "accounts", "billing", "receipts", "invoice",
    "security", "service", "services", "member", "members", "offers", "deals",
})

# Subdomains that mark a bulk/transactional mail stream even when the local part looks human:
# "azure@promomail.microsoft.com" is Microsoft, but promomail. is the marketing relay.
_AUTOMATED_MAIL_SUBDOMAINS = frozenset({
    "notify", "notifications", "promomail", "promo", "mailer", "email", "mail",
    "send", "sendgrid", "mailgun", "bounce", "bounces", "marketing", "news", "alerts",
})


def is_automated_sender(email):
    """True when an address announces machine-generated bulk/transactional mail.

    Used to deny the Tier 1 interview bypass, NOT to drop mail: a message from one of these
    still goes through the ordinary pre-filter and can still alert. A human recruiter scheduling
    an interview does not write from welcome@notify.chime.com, so honouring this costs no real
    interview while closing the class of false Tier 1 that keyword tightening alone cannot.

    careers@/recruiting@/talent@/hr@ are deliberately NOT automated - see the set above.
    """
    raw = str(email or "")
    # A display-name form ("Chime <welcome@notify.chime.com>") must be reduced to the address.
    match = re.search(r'<\s*([^<>@\s]+@[^<>@\s]+)\s*>', raw)
    addr = (match.group(1) if match else raw).strip().lower()
    if "@" not in addr:
        return False
    local, _, domain = addr.partition("@")
    local = re.sub(r'[._-]?\d+$', '', local.strip())
    if local in _AUTOMATED_SENDER_LOCALPARTS:
        return True
    # Leading segment of a multi-part local part: "welcome-team@", "no.reply@".
    lead = re.split(r'[._-]', local)[0] if local else ""
    if lead in ("noreply", "donotreply", "notify", "welcome", "alerts", "promo"):
        return True
    labels = domain.strip(".").split(".")
    return len(labels) >= 3 and labels[0] in _AUTOMATED_MAIL_SUBDOMAINS

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


# Lead words too generic to identify a company on their own. Without this, "First Financial"
# brand-matches firstsolar.com and "United Wholesale" matches unitedairlines.com. A company whose
# name STARTS with one of these still matches through the whole-string tests above.
_GENERIC_BRAND_TOKENS = frozenset({
    "first", "united", "national", "american", "general", "global", "premier", "advanced",
    "allied", "associated", "consolidated", "federal", "international", "standard", "superior",
    "universal", "western", "eastern", "northern", "southern", "central", "pacific", "atlantic",
    "capital", "financial", "insurance", "services", "solutions", "systems", "partners", "group",
})

# Words no one carries into an acronym: "National Center FOR Manufacturing Sciences" is NCMS,
# not NCFMS. Only used to build the acronym candidate in domain_matches_company().
_ACRONYM_STOPWORDS = frozenset({
    "for", "of", "and", "the", "in", "on", "at", "to", "a", "an",
})


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
    if label == company or (len(label) >= 5 and label in company) or (len(company) >= 5 and company in label):
        return True
    # Brand-token match. A company's legal name and its mail domain often share only the brand:
    # "Intact Services USA LLC" sends from intactinsurance.com, where neither whole string
    # contains the other, so the tests above all miss and a real contact is dropped. Compare the
    # first significant word of the company name against the domain label instead.
    #
    # Deliberately narrow, because this is the loosest test here: the token must be >= 5 chars
    # (so "auto", "first", "main" style words cannot carry a match on their own), it must be the
    # FIRST word (the brand, not a descriptor deeper in the name), and it must be a prefix of the
    # domain label rather than appearing anywhere inside it - "intact" matches intactinsurance.com
    # but not contactcenter.com.
    words = [w for w in re.split(r'[^a-z0-9]+', normalized) if w]
    if words:
        brand = words[0]
        if len(brand) >= 5 and brand not in _GENERIC_BRAND_TOKENS and label.startswith(brand):
            return True

    # SHORT BRAND. The tests above all have a >= 5 char floor, which drops a real class of
    # match: "Ford Motor Company" sends from ford.com, where the brand is only 4 characters.
    # The floor exists to stop a short generic token carrying a match on its own ("aa.com" vs
    # "AAA-The Auto Club"), so this relaxes it only where the token cannot be generic: the
    # domain label must EQUAL the company's first word exactly, and that word must be a real
    # brand rather than a descriptor. An exact label==word match is much stricter than the
    # prefix/substring tests, so "aa" still fails against "aaatheautoclub" (first word "aaa").
    if words:
        brand = words[0]
        if 3 <= len(brand) < 5 and brand not in _GENERIC_BRAND_TOKENS and label == brand:
            return True

    # ACRONYM. A long institutional name almost always mails from its initials: "National
    # Center for Manufacturing Sciences" sends from ncms.org. Built from the significant words
    # only - stopwords like "for"/"of"/"and" are not carried into an acronym by anyone - and
    # requires the company to be genuinely long-form (>= 3 significant words), so a two-word
    # company cannot acronym its way into a 2-letter collision.
    significant = [w for w in words if w not in _ACRONYM_STOPWORDS]
    if len(significant) >= 3:
        acronym = "".join(w[0] for w in significant)
        if len(acronym) >= 3 and label == acronym:
            return True
    return False


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
# CARMEN COLD 4/11/21 FOLLOW-UP LADDER (pure, no I/O)
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

# Two ladders, picked per row by whether the contact has EVER replied (carmen_reply_anchor).
#
# COLD - a stranger who has never written back. Day 0 is the original email, so (4, 11) is three
# total contacts, ending at day 18 with the grace week. The fourth contact the old single ladder
# sent (day 21, to someone who had ignored three emails) is the one rung with no case for it: a
# cold contact silent for eleven days has decided, and the third unanswered touch is where spam
# complaints concentrate - which this sender cannot afford on a SPF SOFTFAIL domain.
#
# ENGAGED - has replied at least once, so this is a live conversation, not a push against silence.
# Keeps the original 4/11/21. An ask that needs a call before much moves needs the long runway.
#
# A cold row is PROMOTED automatically the moment a reply lands: carmen_reply_anchor() starts
# returning a date, the row switches to the engaged ladder, and the anchor resets to the reply
# date. Nothing to set by hand.
CARMEN_LADDER_DAYS_COLD = (4, 11)
CARMEN_LADDER_DAYS_ENGAGED = (4, 11, 21)

# Back-compat alias. Callers that predate the split (and the migration guard below) still read
# the engaged ladder, which is the old single ladder unchanged.
CARMEN_LADDER_DAYS = CARMEN_LADDER_DAYS_ENGAGED

# After the last nudge the ladder writes one more date, anchor + ladder[-1] + this, so a silent
# contact gets a week to answer before triage. That written date is what makes "exhausted"
# reachable at all: without it the final rung left the row's gap at exactly the last offset, which
# reads as the final rung again, and the last nudge re-fired every morning forever.
CARMEN_KILL_GRACE_DAYS = 7


def carmen_ladder_for(replied):
    """The ladder tuple a row walks: engaged when the contact has ever replied, else cold.

    `replied` is truthy for a reply date (carmen_reply_anchor), falsy for None.
    """
    return CARMEN_LADDER_DAYS_ENGAGED if replied else CARMEN_LADDER_DAYS_COLD


def carmen_terminal_gap(ladder):
    """The gap the final rung writes for `ladder`: last offset + the grace week.

    Per-ladder, not a module constant: a cold row given the engaged ladder's terminal gap would
    sit 7 days past its own last rung before triage, and the rung math would read it as engaged.
    """
    return ladder[-1] + CARMEN_KILL_GRACE_DAYS


# The engaged ladder's terminal gap, kept as a module constant because plan_carmen_ladder() uses
# it as the widest gap any ladder can legitimately write - the bound on "this row is mid-ladder"
# when deciding whether a stale anchor is a revival. Must stay the MAX across both ladders.
CARMEN_TERMINAL_GAP_DAYS = carmen_terminal_gap(CARMEN_LADDER_DAYS_ENGAGED)

# A Carmen Cold row traverses the whole ladder (grace included) in under 30 days, so an anchor
# older than this cannot be mid-ladder. It is a revived bench contact (Carmen Warm rows carry Last
# Contact Dates months old) or a stalled row, and today is the correct anchor for both. Without
# this, a contact dragged in from the bench reads as long past the last rung and is killed on the
# first pass without a single nudge.
CARMEN_STALE_ANCHOR_DAYS = 30

# Note text the CRM carries, defined once so the writers in main.py and the parsers below can
# never drift apart. Both are written as "[YYYY-MM-DD] <marker> ..." by main.py.
INBOUND_REPLY_NOTE_MARKER = "Inbound reply received"   # route_inbound_reply_to_crm, GENERAL replies
LADDER_RESTART_NOTE_MARKER = "Ladder restarted"        # the sequencer, when it revives a stale row

# Ceiling on automatic moves to Killed per sequencer pass - same reasoning and the same deferral
# semantics as MAX_AUTO_BURIES_PER_RUN: overflow is reported, not written, and not logged, so it
# stays eligible and drains on a later run.
MAX_AUTO_KILLS_PER_RUN = 10


def carmen_ladder_rung(anchor, next_followup, ladder=CARMEN_LADDER_DAYS_ENGAGED):
    """Which rung a Carmen Cold row currently sits on, from the gap between its anchor and
    its scheduled date. 0 = not yet scheduled, 1..len(ladder) = the nudges, len+1 = ladder done.

    Tolerates drift: the sequencer can only advance a row on a day it actually runs, so a
    date a day or two past its nominal rung still reads as that rung rather than falling off.
    """
    if anchor is None or next_followup is None:
        return 0
    gap = (next_followup - anchor).days
    if gap <= 0:
        return 0
    for rung, offset in enumerate(ladder, start=1):
        if gap <= offset:
            return rung
    return len(ladder) + 1


def carmen_ladder_action(anchor, next_followup, today, ladder=CARMEN_LADDER_DAYS_ENGAGED):
    """Pure: what a Carmen Cold row needs today. Side-effect free.

    Returns (action, next_date):
      ("schedule", d)  - undated row (incl. one just dragged in by hand): start the ladder at +4
      ("nudge_N", d)   - rung N is due: alert Kevin, advance to the next rung. The final rung
                         advances to anchor + carmen_terminal_gap(ladder), the triage date.
      ("exhausted", None) - every nudge sent and the grace week is up: triage the row
      ("none", None)   - scheduled for a future date, nothing to do

    MIGRATION: a cold row that was mid-ladder when the cold/engaged split shipped carries a gap
    the old single ladder wrote. Read against the shorter cold ladder those gaps are rung >
    len(), i.e. "exhausted", so the first pass after deploy would move a contact who is still
    owed a nudge straight to Killed. A gap matching one of the old ladder's PENDING-NUDGE offsets
    is therefore treated as the cold ladder's final rung - one last nudge, then the normal grace
    week - rather than as spent.

    The old TERMINAL gap is deliberately excluded from that rescue: it means the old ladder
    already sent everything and the row is a finished ghost, which must still read "exhausted".
    Rescuing it would resurrect dead rows on every pass and they would never reach Killed.
    """
    if anchor is None:
        return "none", None
    if next_followup is None:
        return "schedule", today + timedelta(days=ladder[0])
    if next_followup > today:
        return "none", None

    rung = carmen_ladder_rung(anchor, next_followup, ladder)
    if rung == 0:
        return "schedule", today + timedelta(days=ladder[0])
    if rung > len(ladder):
        gap = (next_followup - anchor).days
        legacy_pending = [d for d in CARMEN_LADDER_DAYS_ENGAGED if d > ladder[-1]]
        if gap in legacy_pending:
            final = len(ladder)
            return f"nudge_{final}", anchor + timedelta(days=carmen_terminal_gap(ladder))
        return "exhausted", None
    if rung == len(ladder):
        return f"nudge_{rung}", anchor + timedelta(days=carmen_terminal_gap(ladder))
    return f"nudge_{rung}", anchor + timedelta(days=ladder[rung])


def _latest_marker_date(note, marker):
    """Most recent "[YYYY-MM-DD] <marker>" date in a notes cell, or None. Notes accumulate one
    entry per line, so the last valid match wins; a malformed date is skipped, never raised."""
    latest = None
    pattern = r"\[(\d{4}-\d{2}-\d{2})\]\s*" + re.escape(marker)
    for match in re.finditer(pattern, str(note or "")):
        try:
            parsed = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def carmen_reply_anchor(note):
    """The date of the most recent inbound reply recorded in a notes cell, or None."""
    return _latest_marker_date(note, INBOUND_REPLY_NOTE_MARKER)


def carmen_restart_anchor(note):
    """The date the sequencer last restarted this row's ladder, or None."""
    return _latest_marker_date(note, LADDER_RESTART_NOTE_MARKER)


class CarmenPlan(tuple):
    """(action, next_date, revived, anchor). Unpacks as a 4-tuple; use .action / .next_date /
    .revived / .anchor for readability. `revived` means the ladder was restarted from today on
    this pass, and the caller must record LADDER_RESTART_NOTE_MARKER so it sticks.

    .replied / .ladder carry which track the row is on. They are set on the instance rather than
    added as tuple slots, so existing 4-tuple unpacking keeps working unchanged. (A tuple subclass
    cannot declare a non-empty __slots__, so these live in the instance dict.)"""

    def __new__(cls, action, next_date, revived, anchor, replied=False,
                ladder=CARMEN_LADDER_DAYS_COLD):
        plan = super().__new__(cls, (action, next_date, revived, anchor))
        plan.replied = replied
        plan.ladder = ladder
        return plan

    action = property(lambda self: self[0])
    next_date = property(lambda self: self[1])
    revived = property(lambda self: self[2])
    anchor = property(lambda self: self[3])


def plan_carmen_ladder(date_added, next_followup, today, note=""):
    """String-in planner for a raw Carmen Cold row. Returns a CarmenPlan.

    Anchor = the latest of Date Added, the last inbound reply and the last ladder restart, all
    read from the row. A reply therefore restarts 4/11/21 from the day they wrote back.

    Revival: a row with no usable anchor, or whose anchor is over CARMEN_STALE_ANCHOR_DAYS old
    and whose follow-up date is not one the ladder wrote, restarts from today as unscheduled.
    Date Added is never rewritten - the caller records the restart as a dated note instead, which
    is what makes the next pass read a fresh anchor rather than reviving again forever.

    A gap of 1..CARMEN_TERMINAL_GAP_DAYS is always trusted as a ladder position, however old the
    anchor, so a late run still triages a finished ghost instead of reviving it. A hand-set future
    date is respected: the row waits for it rather than being restarted early.
    """
    replied_on = carmen_reply_anchor(note)
    ladder = carmen_ladder_for(replied_on)
    candidates = [d for d in (_parse_sequencer_date(date_added), replied_on,
                              carmen_restart_anchor(note)) if d is not None]
    anchor = max(candidates) if candidates else None
    scheduled = None if is_followup_unscheduled(next_followup) else _parse_sequencer_date(next_followup)

    revived = False
    if anchor is None:
        revived = True
    else:
        gap = (scheduled - anchor).days if scheduled is not None else None
        # A live ladder position needs both: a gap the ladder writes, AND a scheduled date the
        # ladder wrote recently. Old bench dates that happen to sit a few days apart satisfy the
        # first alone, and trusting them would walk a revived contact straight to Killed.
        ladder_shaped = (gap is not None and 0 < gap <= CARMEN_TERMINAL_GAP_DAYS
                         and (today - scheduled).days <= CARMEN_STALE_ANCHOR_DAYS)
        if (today - anchor).days > CARMEN_STALE_ANCHOR_DAYS and not ladder_shaped:
            if scheduled is not None and scheduled > today:
                # A hand-set future date on a row the ladder is not driving. Distinct from the
                # ordinary "waiting between rungs" quiet below, which also returns "none": this
                # one is worth surfacing on the sheet, because it is otherwise indistinguishable
                # from a row the sequencer forgot.
                return CarmenPlan("hold", None, False, anchor, bool(replied_on), ladder)
            revived = True
    if revived:
        anchor, scheduled = today, None

    action, next_date = carmen_ladder_action(anchor, scheduled, today, ladder)
    return CarmenPlan(action, next_date, revived, anchor, bool(replied_on), ladder)


# ------------------------------------------------------------------------------
# CARMEN COLD STATUS MARKER (Column E, "Context / Priority")
#
# Column E and NOT Status (Column F): statusRank()/status_rank() match the canonical vocabulary
# as whole strings, so any decorated Status reads as rank -1 - which makes followup_action()
# return "none" and silently stops sequencing the row. Column E is free text that no ranking
# path reads, so sorting on it groups the board without touching the state machine.
#
# Counts are TOTAL CONTACTS, not rungs: day 0 is the original email, so the cold ladder's two
# nudges are "1 of 3" and "2 of 3". Reading the cell tells Kevin how many touches remain without
# doing the arithmetic.
CARMEN_MARKER_SEP = " | "

# Any cell this pattern matches is a marker the sequencer wrote and may overwrite. Anything else
# in Column E is Kevin's own text and is preserved after the separator.
CARMEN_MARKER_RE = re.compile(
    r"^\s*(?:COLD|WARM|NEW|HOLD)\s*·\s*(?:\d+\s+of\s+\d+|spent|unsent|dated)\s*"
    r"(?:\|\s*)?", re.IGNORECASE)


def carmen_status_marker(action, replied, ladder):
    """The Column E marker for a row the sequencer just planned, or None when it has nothing to
    say (a row it is not driving).

      NEW · unsent    - in Carmen Cold, ladder not started yet
      COLD · 2 of 3   - never replied, second of three total contacts sent
      WARM · 3 of 4   - has replied, third of four sent
      COLD · spent    - ladder exhausted, awaiting triage
      HOLD · dated    - hand-set future date the sequencer is respecting, not laddering

    HOLD is the one that surfaces something otherwise invisible: plan_carmen_ladder returns
    "hold" for a hand-dated row it is not driving, which on the sheet would otherwise look
    identical to a row the sequencer forgot about. Plain "none" - the ordinary quiet between
    rungs - returns None here on purpose: it fires every day a row is merely waiting, and
    marking it would overwrite the real rung marker the next morning.
    """
    track = "WARM" if replied else "COLD"
    total = len(ladder) + 1  # + the original day-0 email

    if action == "schedule":
        return "NEW · unsent"
    if action == "exhausted":
        return f"{track} · spent"
    if action == "hold":
        return "HOLD · dated"
    if str(action or "").startswith("nudge_"):
        try:
            rung = int(str(action).split("_", 1)[1])
        except (IndexError, ValueError):
            return None
        return f"{track} · {rung} of {total}"
    return None


def carmen_marker_cell(existing, marker):
    """Column E's new value: `marker`, with any text Kevin typed preserved after it.

    A previous marker is replaced, never stacked. A blank cell gets the marker alone. Returns
    `existing` unchanged when there is no marker to write, so a caller can always assign.
    """
    current = str(existing or "").strip()
    if not marker:
        return current
    tail = CARMEN_MARKER_RE.sub("", current, count=1).strip() if current else ""
    return f"{marker}{CARMEN_MARKER_SEP}{tail}" if tail else marker


def plan_carmen_followup(date_added, next_followup, today, note=""):
    """Two-value form of plan_carmen_ladder(): (action, next_date). Same anchoring and revival
    rules; use plan_carmen_ladder() when the caller needs to know a revival happened.

    An undated row anchors on `today` rather than being skipped - that is the manual-move case:
    a contact dragged into Carmen Cold carries no useful date, so the ladder starts when the
    sequencer first sees them.
    """
    plan = plan_carmen_ladder(date_added, next_followup, today, note)
    return plan.action, plan.next_date


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


# ==============================================================================
# MANUAL JOB INGEST (/job + the desktop bookmarklet) - pure parsing, no I/O
# ==============================================================================

# A LinkedIn job URL carries its numeric posting id either as the last path segment
# (/jobs/view/4461280495/) or as the currentJobId query param on a search-results page - the
# latter is what copying the address bar off a job search actually yields.
_LINKEDIN_JOB_PATH_RE = re.compile(r"/jobs/view/(\d+)")


def extract_linkedin_job_id(url):
    """Pull the numeric posting id out of a LinkedIn job URL, or None.

    Handles both the canonical /jobs/view/<id> permalink and the /jobs/search-results/?currentJobId=<id>
    form you get by copying the address bar while browsing results.
    """
    raw = str(url or "").strip()
    if not raw:
        return None
    path_match = _LINKEDIN_JOB_PATH_RE.search(raw)
    if path_match:
        return path_match.group(1)
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
    except Exception:
        return None
    # Matched case-insensitively so a URL that passed through a lowercasing client still resolves.
    for key, vals in params.items():
        if key.lower() != "currentjobid":
            continue
        if vals and str(vals[0]).isdigit():
            return str(vals[0])
    return None


# Campaign/analytics params that identify how Kevin ARRIVED at a posting, never which posting it
# is. Two links to the same job from a LinkedIn ad and an Indeed ad differ only by these, so they
# are stripped before the URL is used as a dedup key or stored as the apply link.
_TRACKING_PARAM_PREFIXES = ("utm_", "sc_", "gh_", "_hs")
_TRACKING_PARAM_NAMES = {
    "source", "src", "ref", "refid", "referrer", "trackingid", "trk", "ebp",
    "gclid", "fbclid", "mc_cid", "mc_eid", "campaign", "medium", "recruiter",
}


def strip_tracking_params(url):
    """Drop campaign/analytics query params, preserving everything that identifies the posting.

    Order of surviving params is preserved. A URL with no query string comes back unchanged.
    """
    raw = str(url or "").strip()
    if not raw or "?" not in raw:
        return raw
    try:
        parts = urllib.parse.urlsplit(raw)
        kept = [
            (k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if not (k.lower() in _TRACKING_PARAM_NAMES or k.lower().startswith(_TRACKING_PARAM_PREFIXES))
        ]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), "")
        )
    except Exception:
        return raw


def canonical_job_url(url):
    """Normalize any job URL for use as a dedup key and stored apply link.

    LinkedIn collapses to its bare /jobs/view/<id> permalink - search-results URLs carry a long
    tail of per-visit tracking (eBP, refId, trackingId) around the same posting id. Every other
    host keeps its path (which is what identifies the posting) minus tracking params and fragment.
    """
    job_id = extract_linkedin_job_id(url) if "linkedin.com" in str(url or "").lower() else None
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}/"
    return strip_tracking_params(url)


def canonical_linkedin_job_url(url):
    """Back-compat alias for canonical_job_url(). Prefer canonical_job_url in new code."""
    return canonical_job_url(url)


def is_linkedin_job_url(url):
    """True if this looks like a LinkedIn URL carrying a job posting id.

    Only the host check is case-folded: the query param is `currentJobId`, and lowercasing the
    whole URL before extraction would make that key unmatchable.
    """
    raw = str(url or "").strip()
    return "linkedin.com" in raw.lower() and extract_linkedin_job_id(raw) is not None


def parse_job_command(text_input):
    """Parse `/job <url>` and `/job Title @ Company <url>` into (title, company, url).

    The bare-URL form returns (None, None, url) and leaves title/company for the scraper to fill.
    The explicit form is the fallback for when LinkedIn blocks the server-side fetch: everything
    before the '@' is the title, everything after it (minus a trailing URL) is the company.

    Returns None when no URL and no '@' form is present - i.e. nothing usable.
    """
    body = str(text_input or "").strip()
    # Strip the leading /job or /j token
    body = re.sub(r"^/(?:job|j)\b\s*", "", body, flags=re.IGNORECASE).strip()
    if not body:
        return None

    # A URL may sit anywhere in the text; pull the first one out and treat the rest as title/company.
    url_match = re.search(r"https?://\S+", body)
    url = url_match.group(0).rstrip(".,);") if url_match else ""
    remainder = (body[:url_match.start()] + " " + body[url_match.end():]).strip() if url_match else body

    if "@" in remainder:
        title_part, _, company_part = remainder.partition("@")
        title = title_part.strip(" -–—")
        company = company_part.strip(" -–—")
        if title and company:
            return (title, company, url)

    if url:
        return (None, None, url)
    return None


def parse_job_page_html(html_text):
    """Best-effort extraction of (title, company, description) from ANY job posting page.

    Order of preference:
      1. schema.org JobPosting JSON-LD - emitted by nearly every ATS and corporate careers site
         (Workday, iCIMS, Greenhouse, Phenom) and by LinkedIn's public guest pages. Far more
         stable than CSS class names, which are minified and rotate.
      2. LinkedIn guest-page markup (topcard__title / topcard__org-name-link).
      3. The <title> tag, including LinkedIn's "Company hiring Title in City" shape.

    Returns (title, company, description), any of which may be "" when the page is an auth wall
    or renders its content only via JavaScript.
    """
    body = str(html_text or "")
    if not body:
        return ("", "", "")

    title = company = description = ""

    # 1. JSON-LD JobPosting - the most reliable source when the guest page renders.
    for blob in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', body, re.DOTALL | re.IGNORECASE):
        try:
            import json as _json
            parsed = _json.loads(blob.strip())
        except Exception:
            continue
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for node in candidates:
            if not isinstance(node, dict):
                continue
            if node.get("@type") != "JobPosting":
                continue
            title = title or str(node.get("title") or "")
            org = node.get("hiringOrganization")
            if isinstance(org, dict):
                company = company or str(org.get("name") or "")
            description = description or strip_html_to_text(node.get("description"))

    # 2. Guest-page markup fallback. The jobs-guest endpoint renders the job title in an <h2>
    # (class top-card-layout__title / topcard__title), NOT an <h1> - matching only <h1> here
    # silently produced a blank title on the one endpoint that actually answers a server fetch.
    if not title:
        m = re.search(
            r'<h[12][^>]*(?:top-card-layout__title|topcard__title)[^>]*>(.*?)</h[12]>',
            body, re.DOTALL | re.IGNORECASE,
        )
        if not m:
            m = re.search(r'<h1[^>]*>(.*?)</h1>', body, re.DOTALL | re.IGNORECASE)
        if m:
            title = strip_html_to_text(m.group(1))
    if not company:
        m = re.search(r'<a[^>]*(?:topcard__org-name-link|top-card-layout__second-subline)[^>]*>(.*?)</a>', body, re.DOTALL | re.IGNORECASE)
        if m:
            company = strip_html_to_text(m.group(1))
    if not description:
        m = re.search(r'<div[^>]*(?:show-more-less-html__markup|description__text)[^>]*>(.*?)</div>\s*</div>', body, re.DOTALL | re.IGNORECASE)
        if m:
            description = strip_html_to_text(m.group(1))

    # 3. <title> tag. Two shapes are worth recognizing:
    #      LinkedIn:      "Company hiring Job Title in City, State | LinkedIn"
    #      Careers pages: "Job Title | Multiple Locations | Company"  (pipe-delimited)
    if not (title and company):
        m = re.search(r'<title[^>]*>(.*?)</title>', body, re.DOTALL | re.IGNORECASE)
        if m:
            head = strip_html_to_text(m.group(1))
            head = re.sub(r'\s*\|\s*LinkedIn\s*$', '', head, flags=re.IGNORECASE)
            hiring = re.search(r'^(.*?)\s+hiring\s+(.*?)(?:\s+in\s+.*)?$', head, flags=re.IGNORECASE)
            if hiring:
                company = company or hiring.group(1).strip()
                title = title or hiring.group(2).strip()
            else:
                # Pipe-delimited: first segment is the role, last is the employer. Anything
                # between them is a location and is discarded. Requires >= 2 segments, and is
                # only a guess - JSON-LD above is authoritative whenever the page provides it.
                segments = [s.strip() for s in head.split("|") if s.strip()]
                if len(segments) >= 2:
                    title = title or segments[0]
                    company = company or segments[-1]

    return (title.strip(), company.strip(), description.strip())


def strip_html_to_text(raw):
    """Flatten an HTML fragment to readable plain text - tags dropped, entities decoded,
    <br>/<p>/<li> turned into newlines so a scraped job description keeps its bullet structure.
    """
    import html as _html
    body = str(raw or "")
    if not body:
        return ""
    body = re.sub(r'<\s*(br|/p|/div|/li|/ul|/ol)\s*/?\s*>', '\n', body, flags=re.IGNORECASE)
    body = re.sub(r'<\s*li[^>]*>', '\n- ', body, flags=re.IGNORECASE)
    body = re.sub(r'<[^>]+>', ' ', body)
    body = _html.unescape(body)
    body = re.sub(r'[ \t ]+', ' ', body)
    body = re.sub(r'\n\s*\n\s*\n+', '\n\n', body)
    return body.strip()


def build_ingest_job_dict(title, company, description, url, now=None):
    """Assemble a manually-ingested posting into the same job dict shape the JSearch/ATS feeds
    produce, so process_single_candidate() cannot tell it apart.

    The `ingest_` job_id prefix is what derive_job_source() maps to "manual_ingest" for per-source
    outcome attribution, and what keeps these out of the ATS-sourced Clavicular gate (which keys
    on gh_/lever_/ashby_ prefixes). The id is hashed off the canonical URL so re-pasting the same
    posting produces the same id and trips the existing dedup ledger instead of double-carding.
    """
    stamp = now or datetime.now(timezone.utc)
    canonical = canonical_job_url(url) if url else ""
    seed = canonical or url or f"{company}|{title}"
    return {
        "job_id": f"ingest_{hashlib.md5(str(seed).encode('utf-8', 'ignore')).hexdigest()[:12]}",
        "employer_name": (company or "").strip() or "Manual Ingest",
        "job_title": (title or "").strip() or "Manually Ingested Role",
        "job_description": (description or "").strip() or (title or ""),
        "job_apply_link": canonical or str(url or ""),
        "job_city": "",
        "job_state": "",
        "job_is_remote": False,
        "job_posted_at_datetime_utc": stamp.isoformat(),
    }
