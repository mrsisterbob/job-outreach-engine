"""Pure scoring rules (no I/O). One progressive posting-age ladder.

Every number here is a one-line edit. Ladder (posted_hours -> points):
  <=48h +10 | <=120h +7 | <=240h +2 | <336h 0 | <720h -6 | >=720h -25
A posting at or past AGE_DEAD_HOURS is treated as dead: process_single_candidate caps its final
score at AGE_DEAD_CEILING, one under the Tier-2 floor of 65, however strong its keywords.
"""

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
