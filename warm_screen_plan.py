"""Pure planning rules for /w: what gets scored, what gets listed, who was applied to.

No I/O, no imports from main.py.

Constants Kevin chose: MIN_PRE_BOOST_SCORE = 70, LISTED_PER_COMPANY = 2, CARD_CAP = 5.
Constants that are the planner's picks and are NOT measured: CANDIDATES_PER_COMPANY = 3,
RESCREEN_DAYS = 14, APPLIED_SKIP_DAYS = 14 (DIGEST_CAP = 5 mirrors /t's digest).

The score gate is the PRE-boost score (final score minus the warm boost), because at a warm
company every role clearing 70 gets the +30 and lands near 100. Cards and digest are split by
rank, not by a score band.
"""
import html
from datetime import date, datetime, timedelta

MIN_PRE_BOOST_SCORE = 70  # roles below this pre-warm-boost score are rejected
CANDIDATES_PER_COMPANY = 3  # roles per company that get a Gemini call (unmeasured pick)
LISTED_PER_COMPANY = 2  # most roles one company may show in a run
CARD_CAP = 5  # best roles per run that become cards
DIGEST_CAP = 5  # next-best roles per run that go in the digest
RESCREEN_DAYS = 14  # a role screened or shown within this many days is skipped (unmeasured)
APPLIED_SKIP_DAYS = 14  # a company applied to within this many days is skipped (unmeasured)


def _parse_day(value):
    """Return a date from a YYYY-MM-DD string, or None if missing or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def is_recently_memoed(memo_day, today, rescreen_days=RESCREEN_DAYS):
    """True when a role's memo date is within rescreen_days of today (missing or malformed = False).

    Boundary: a memo exactly rescreen_days old is NOT recent (it is screened again); a memo
    rescreen_days - 1 days old is. A future memo date counts as recent.
    """
    seen = _parse_day(memo_day)
    return seen is not None and seen + timedelta(days=rescreen_days) > today


def pick_to_screen(roles_by_company, memo, today, *, per_company=CANDIDATES_PER_COMPANY,
                   rescreen_days=RESCREEN_DAYS):
    """Flat list of roles worth a Gemini call: per company, drop recently memoed roles first, then
    take the best per_company by rank, so roles ranked below a decided top three still get screened."""
    out = []
    for company in sorted(roles_by_company):
        ranked = sorted(roles_by_company[company],
                        key=lambda r: (r["rank"], str(r.get("job_hash", ""))))
        fresh = [r for r in ranked
                 if not is_recently_memoed((memo or {}).get(r.get("job_hash")), today, rescreen_days)]
        out.extend(fresh[:per_company])
    return out


def choose_outputs(scored):
    """Split scored roles into cards, digest, held and rejected without mutating the input."""
    rejected = [s for s in scored if s["pre_boost"] < MIN_PRE_BOOST_SCORE]
    eligible = [s for s in scored if s["pre_boost"] >= MIN_PRE_BOOST_SCORE]

    by_company = {}
    for s in eligible:
        by_company.setdefault(s["company_key"], []).append(s)

    held = []
    survivors = []
    for company in by_company:
        ordered = sorted(by_company[company],
                         key=lambda s: (-s["final"], -s["pre_boost"], str(s["job_hash"])))
        survivors.extend(ordered[:LISTED_PER_COMPANY])
        held.extend(ordered[LISTED_PER_COMPANY:])

    survivors.sort(key=lambda s: (-s["final"], -s["pre_boost"],
                                  str(s["company_key"]), str(s["job_hash"])))
    cards = survivors[:CARD_CAP]
    digest = survivors[CARD_CAP:CARD_CAP + DIGEST_CAP]
    held.extend(survivors[CARD_CAP + DIGEST_CAP:])
    return {"cards": cards, "digest": digest, "held": held, "rejected": rejected}


def applied_recently(applied_dates, company_key, today, days=APPLIED_SKIP_DAYS):
    """True when the company's latest application is within days of today (future counts)."""
    applied = _parse_day((applied_dates or {}).get(company_key))
    if applied is None:
        return False
    return applied + timedelta(days=days) >= today


def digest_lines(items):
    """Digest lines in /t's shape: score right-aligned, company and title truncated."""
    return [f"{i['final']:>3}  {str(i['company'])[:28]} - {str(i['title'])[:40]}"
            for i in items[:DIGEST_CAP]]


UNSCORED_NAMES_SHOWN = 5  # unscored companies named in the closing message before "and N more"


def rotation_report(picked_companies, finished_companies, unscored_companies, carded_companies,
                    held_back_boards, screen_seconds, budget_seconds):
    """Closing-message lines that make the screen order visible (a measurement, no behavior).

    Inputs are lists of company display names (held_back_boards is a count). Names are
    html.escape'd whole, never sliced mid-tag. Only lines with something to say are returned."""
    picked = sorted(set(picked_companies or []))
    finished = set(finished_companies or [])
    unscored = sorted(set(unscored_companies or []), key=lambda n: (str(n).lower(), str(n)))
    carded = list(carded_companies or [])
    lines = []
    if picked:
        lines.append(f"Screen: {len(picked)} companies picked, {len(finished)} finished in "
                     f"{screen_seconds:.0f} s of {budget_seconds:.0f} s.")
    if unscored:
        shown = ", ".join(html.escape(str(n)) for n in unscored[:UNSCORED_NAMES_SHOWN])
        more = len(unscored) - UNSCORED_NAMES_SHOWN
        tail = f" and {more} more" if more > 0 else ""
        lines.append(f"Budget ended: {len(unscored)} unscored, first alphabetically: {shown}{tail}")
    lines.append(f"Cards: {len(carded)} from {len(set(carded))} companies.")
    if held_back_boards:
        lines.append(f"Boards held back to retry first next run: {held_back_boards}.")
    return lines
