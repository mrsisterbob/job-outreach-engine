"""Pure helpers for the /w board-lookup queue: which warm entries are not employers, and which
never-looked-up companies to take first. No I/O.

The word lists below are a planner's guess, not measured against the live sheet. Edit them here.
"""
import re

from warm_big_companies import WARM_BOARD_TARGET_MIN_CONTACTS

SCHOOL_WORDS = ("school", "schools", "university", "college", "academy", "seminary",
                "preschool", "montessori")
CHURCH_WORDS = ("church", "churches", "ministry", "ministries", "parish", "chapel",
                "diocese", "synagogue", "mosque")
LAW_WORDS = ("attorney", "attorneys", "llp")
# "law" only counts together with one of these words.
LAW_COMPANION_WORDS = ("office", "offices", "group", "firm")
LAW_PHRASES = ("law offices", "counsel at law")
CLEANING_WORDS = ("cleaning", "cleaners", "janitorial", "maid", "maids")
CLEANING_PHRASES = ("carpet care",)
# A school-word name that also carries one of these is a real employer with a job board.
EMPLOYER_OVERRIDE_WORDS = ("health", "hospital", "medical", "bank", "insurance")
EMPLOYER_OVERRIDE_PHRASES = ("credit union",)

KINDS = ("school", "church", "law firm", "cleaning")


def _words(name):
    return re.findall(r"[a-z0-9]+", str(name or "").lower())


def _has_phrase(words, phrase):
    p = phrase.split()
    return any(words[i:i + len(p)] == p for i in range(len(words) - len(p) + 1))


def non_employer_kind(name):
    """'school', 'church', 'law firm', 'cleaning', or '' when the name may be an employer."""
    words = _words(name)
    if not words:
        return ""
    ws = set(words)
    if ws & set(EMPLOYER_OVERRIDE_WORDS) or any(_has_phrase(words, p) for p in EMPLOYER_OVERRIDE_PHRASES):
        return ""
    if ws & set(SCHOOL_WORDS):
        return "school"
    if ws & set(CHURCH_WORDS):
        return "church"
    if (ws & set(LAW_WORDS) or "law" in ws and ws & set(LAW_COMPANION_WORDS)
            or any(_has_phrase(words, p) for p in LAW_PHRASES)):
        return "law firm"
    if ws & set(CLEANING_WORDS) or any(_has_phrase(words, p) for p in CLEANING_PHRASES):
        return "cleaning"
    return ""


def lookup_order(rows, min_contacts=WARM_BOARD_TARGET_MIN_CONTACTS):
    """rows: iterable of (key, count, priority_score, attempted_at). Returns keys: every company
    with min_contacts+ warm contacts first, then the rest. Inside each group biggest count first
    whether or not an earlier lookup errored; ties go never-attempted first, then oldest attempt,
    then higher priority, then key. Attempt history never moves a company across the size gate."""
    rows = list(rows)
    rows.sort(key=lambda r: (0 if (r[1] or 0) >= min_contacts else 1, -(r[1] or 0),
                             r[3] or "", -(r[2] or 0), r[0]))
    return [r[0] for r in rows]
