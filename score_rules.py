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


def seniority_ceiling(title):
    """SENIOR_CEILING when the title matches SENIORITY_TITLE_RE, else None."""
    if title and SENIORITY_TITLE_RE.search(title.lower()):
        return SENIOR_CEILING
    return None
