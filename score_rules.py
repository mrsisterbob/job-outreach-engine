"""Pure scoring rules (no I/O). One progressive posting-age ladder.

Every number here is a one-line edit. Ladder (posted_hours -> points):
  <=48h +10 | <=120h +7 | <=240h +2 | <336h 0 | <720h -6 | >=720h -25
A posting at or past AGE_DEAD_HOURS is treated as dead: process_single_candidate caps its final
score at AGE_DEAD_CEILING, one under the Tier-2 floor of 65, however strong its keywords.

Seniority: a senior/lead/manager title keeps its -18 in the keyword layer, but the keyword/salary
stack can earn more than that back, so process_single_candidate also caps the final score at
SENIOR_CEILING (top of the Tier-2 digest band): shown in the digest, never carded as Tier 1.
Entry-level title markers weigh ENTRY_TITLE_BONUS, a bare job-family word PLAIN_FAMILY_TITLE_BONUS.
"""
import re

# (upper bound in hours, inclusive, points) - checked in order.
AGE_FRESH_STEPS = ((48, 10), (120, 7), (240, 2))
AGE_STALE_HOURS = 336        # 14 days: from here the posting starts losing points
AGE_STALE_PENALTY = -6
AGE_DEAD_HOURS = 720         # 30 days
AGE_DEAD_PENALTY = -25
AGE_DEAD_CEILING = 64        # one under the Tier-2 floor (65)


def posted_age_adjustment(posted_hours):
    """Score points for a posting's age in hours.

    A negative age (future-dated feed) counts as fresh. None scores 0, but note that the caller's
    parse_posted_hours fails open to 48 on a missing/unparseable date, so a dateless posting lands
    in the 48h bucket and earns the full +10 (deliberate: ATS feeds omit the date).
    """
    if posted_hours is None:
        return 0
    if posted_hours >= AGE_DEAD_HOURS:
        return AGE_DEAD_PENALTY
    if posted_hours >= AGE_STALE_HOURS:
        return AGE_STALE_PENALTY
    for limit, points in AGE_FRESH_STEPS:
        if posted_hours <= limit:
            return points
    return 0


def is_dead_posting(posted_hours):
    """True when the posting is old enough to be capped below Tier 2."""
    return posted_hours is not None and posted_hours >= AGE_DEAD_HOURS


# ---- Title seniority ----
# Moved unchanged from calculate_hybrid_score_modifier. It matches words, so "Staff Accountant" and
# "Lead Generation Specialist" match too (accepted cost, same as the -18 penalty always had).
SENIORITY_TITLE_RE = re.compile(r'\b(senior|sr\.?|lead|principal|staff|head of|director|vp|manager|mgr)\b')
TIER1_FLOOR = 80
SENIOR_CEILING = 74             # top of the Tier-2 digest band, one band under the Tier-1 floor
ENTRY_TITLE_BONUS = 10          # junior / associate / entry-level / coordinator / specialist (was 6)
PLAIN_FAMILY_TITLE_BONUS = 6    # bare analyst / operations / administrator / assistant (was 4)


# A trailing level numeral reads as seniority: "Analyst II" is a step up from "Analyst I". Roman forms
# are uppercase only ("Analyst Iv" and the word "ii" in prose do not match). The numeral must follow
# a letter word and end the title or sit right before a separator, so "Tier 2 Support Analyst",
# "Level 3 Reporting Lead" and "Analyst (2025 Cohort)" are not read as levels.
LEVEL2_CEILING = 74             # level II / 2: still in the Tier-2 digest band, never carded
LEVEL3_PLUS_CEILING = 64        # level III and above: one under the digest floor (65), out of sight
_LEVEL_VALUES = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}
_TITLE_LEVEL_RE = re.compile(r'(?<=[A-Za-z])\s+(III|II|IV|V|I|[1-5])\s*(?:$|[-–—,(|:])')


def title_level(title):
    """Level read off a trailing numeral (I/1 -> 1, II/2 -> 2, ... V/5 -> 5), None when absent."""
    found = _TITLE_LEVEL_RE.search(str(title or "").strip())
    if not found:
        return None
    tok = found.group(1)
    return _LEVEL_VALUES[tok] if tok in _LEVEL_VALUES else int(tok)


def seniority_ceiling(title):
    """Lowest ceiling that applies to the title, or None: SENIOR_CEILING for a seniority word,
    LEVEL2_CEILING for level 2, LEVEL3_PLUS_CEILING for level 3 or higher."""
    caps = []
    if title and SENIORITY_TITLE_RE.search(title.lower()):
        caps.append(SENIOR_CEILING)
    level = title_level(title)
    if level is not None and level >= 3:
        caps.append(LEVEL3_PLUS_CEILING)
    elif level == 2:
        caps.append(LEVEL2_CEILING)
    return min(caps) if caps else None


# ---- Years of experience DEMANDED ----
# Only a year figure attached to experience is a demand. "Serving families for 40 years", "grown for
# 12 years" and "a 2-year associate degree" are not, and the old inline regex read all of them.
# A figure counts when "year(s)" is followed within ~4 words (same sentence) by "experience"/"exp",
# or when it directly follows minimum (of) / at least / require(s|d) ("Minimum of 3 years in a
# similar role"). A range ("3-5", "3 to 5") reads its LOWER bound.
_YEARS_FIGURE_RE = re.compile(
    r"(?<![\d.])(\d+)\s*(?:\+|\s+plus\b)?\s*(?:(?:-|\u2013|to)\s*\d+\s*\+?\s*)?-?\s*(?:years?|yrs?)\b")
_YEARS_EXPERIENCE_AFTER_RE = re.compile(
    r"\s*(?:['\u2019]s?)?(?:\s+[\w'\u2019-]+){0,4}?\s+(?:experience|exp)\b")
_YEARS_DEMAND_LEAD_RE = re.compile(r"(?:minimum(?:\s+of)?|at\s+least|requires?|required)\s+(?:an?\s+)?$")


def years_of_experience_demanded(description):
    """Largest lower-bound years figure a (lowercase) posting ties to experience, or None."""
    text = str(description or "")
    best = None
    for found in _YEARS_FIGURE_RE.finditer(text):
        if (_YEARS_EXPERIENCE_AFTER_RE.match(text, found.end())
                or _YEARS_DEMAND_LEAD_RE.search(text[:found.start()])):
            years = int(found.group(1))
            if best is None or years > best:
                best = years
    return best


# ---- Years REQUIRED: the hard gate (TASK-89) ----
# A posting that demands this many years of experience (largest lower bound) is rejected before it
# is scored. 3 stays a score penalty only (the -4 branch above), so "3+ years" and "2-3 years" pass.
YEARS_REQUIRED_REJECT_AT = 4
# A sentence carrying one of these is a wish or a degree-or-experience alternative, not a requirement.
_YEARS_SOFT_RE = re.compile(r"preferred|a plus|nice to have|bonus|ideally|or equivalent")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|[\r\n]+")


def years_required_reject(description):
    """Largest years-of-experience demand in the REQUIRED sentences of a (lowercase) posting when it
    is >= YEARS_REQUIRED_REJECT_AT, else None (also None when no years text can be read)."""
    best = None
    for sentence in _SENTENCE_SPLIT_RE.split(str(description or "").lower()):
        if not sentence or _YEARS_SOFT_RE.search(sentence):
            continue
        years = years_of_experience_demanded(sentence)
        if years is not None and (best is None or years > best):
            best = years
    return best if best is not None and best >= YEARS_REQUIRED_REJECT_AT else None
