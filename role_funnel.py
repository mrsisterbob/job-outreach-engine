"""Per-role conversion funnel for /funnel. Pure: no I/O.

Code.gs funnel_stats buckets rows by their CURRENT JOBS-tab Status and divides one bucket by
another. That breaks three ways: a role that moved on is no longer in the earlier bucket (so
Matched->Applied ran to 254%), interviews recorded only in SQLite application_outcomes or on a
row moved into Carmen are never seen, and every Died row is skipped even when Kevin applied.

Here each role (one sheet_uuid) is counted once, at the highest stage ANY evidence says it
reached, and reaching a stage counts it in every earlier one. The counts are therefore nested
(offers <= interviewed <= replied <= applied <= matched) and no rate can exceed 100%.

The caller (main.py) does the CRM and SQLite reads and passes the results in.
"""
import re

from pipeline_utils import carmen_reply_anchor

MATCHED, APPLIED, REPLIED, INTERVIEWED, OFFER = range(5)

_COUNT_KEYS = ("matched", "applied", "replied", "interviewed", "offers")
PERSONAS = ("Tetiana", "Clavicular", "Other")
DIED_TAB = "Died"

# canonicalizeStatus() (Code.gs) port: same regexes, same order, first match wins.
_CANON_RULES = (
    (r"match|sourc|lead|prospect|candidate|pending|queue|identif|backlog|to review", "Matched"),
    (r"appl|sent", "Applied"),
    (r"repl|respond", "Replied"),
    (r"screen", "Screening"),
    (r"interview", "Interviewing"),
    (r"offer", "Offer"),
    (r"reject|declin|pass|no thanks", "Rejected"),
)

# Kevin's own decision NOT to apply, typed into the sheet by hand (nothing in main.py writes
# these): Pass / Decline / No thanks. Code.gs canonicalizeStatus() buckets them as "Rejected", and
# _CANON_RULES must keep mirroring it, so the distinction is made here, in _Role.add_row, on the
# raw status. This is deliberately Python-only: the Code.gs buckets (public stats) are unchanged.
# \bpass keeps "bypass" out. An employer's "Rejected" is not this and stays (APPLIED, True).
_OWN_DECISION_RE = re.compile(r"declin|\bpass(?:ed)?\b|no thanks", re.IGNORECASE)
_EMPLOYER_REJECTION_RE = re.compile(r"reject", re.IGNORECASE)

# Canonical JOBS Status -> (stage, rejected flag).
_JOBS_STAGE = {
    "Matched": (MATCHED, False),
    "Applied": (APPLIED, False),
    "Replied": (REPLIED, False),
    "Screening": (INTERVIEWED, False),
    "Interviewing": (INTERVIEWED, False),
    "Offer": (OFFER, False),
    "Rejected": (APPLIED, True),
}

# PEOPLE statuses that mean a job row moved into Carmen reached an interview. Matched exactly -
# never through canonicalize_status, where "Networking Call" or "Cold Lead" would regex-match.
_PEOPLE_INTERVIEW_STATUSES = {"phone screen", "interview"}

# application_outcomes.status -> (stage, rejected flag). dead_link says nothing past matched.
_EVENT_STAGE = {
    "applied": (APPLIED, False),
    "withdrawn": (APPLIED, False),
    "reply": (REPLIED, False),
    "interview": (INTERVIEWED, False),
    "offer": (OFFER, False),
    "rejection": (APPLIED, True),
    "dead_link": (MATCHED, False),
}


def canonicalize_status(raw):
    """Python port of Code.gs canonicalizeStatus(): the canonical JOBS Status for any free-text /
    legacy value, or None when nothing matches. Blank -> "Matched"."""
    s = ("" if raw is None else str(raw)).strip().lower()
    if s == "":
        return "Matched"
    for pattern, canonical in _CANON_RULES:
        if re.search(pattern, s):
            return canonical
    return None


def persona_for_tab(tab):
    """The persona a JOBS tab belongs to: Tetiana, Clavicular, or Other."""
    if tab in ("Tetiana Cold", "Tetiana Warm"):
        return "Tetiana"
    if tab == "Clavicular":
        return "Clavicular"
    return "Other"


def _empty_counts():
    counts = {k: 0 for k in _COUNT_KEYS}
    counts["rejected"] = 0
    return counts


def _rate(num, denom):
    return round(100.0 * num / denom, 1) if denom else None


class _Role:
    __slots__ = ("stage", "rejected", "tabs", "jobs_tabs")

    def __init__(self):
        self.stage = MATCHED
        self.rejected = False
        self.tabs = []        # every tab this role has a CRM row on
        self.jobs_tabs = []   # JOBS-schema tabs only, in input order

    def lift(self, stage, rejected=False):
        self.stage = max(self.stage, stage)
        self.rejected = self.rejected or rejected

    def add_row(self, row):
        tab = str(row.get("tab") or "")
        schema = str(row.get("schema") or "").upper()
        self.tabs.append(tab)
        if schema == "JOBS":
            self.jobs_tabs.append(tab)
            raw = str(row.get("status") or "")
            canonical = canonicalize_status(raw) or "Matched"
            if (canonical == "Rejected" and _OWN_DECISION_RE.search(raw)
                    and not _EMPLOYER_REJECTION_RE.search(raw)):
                # Kevin passed on or declined it: matched, never applied, never rejected. Any
                # applied/interview/offer event in application_outcomes still lifts it.
                self.lift(MATCHED, False)
            else:
                self.lift(*_JOBS_STAGE[canonical])
        elif str(row.get("status") or "").strip().lower() in _PEOPLE_INTERVIEW_STATUSES:
            self.lift(INTERVIEWED)
        if carmen_reply_anchor(row.get("note")) is not None:
            self.lift(REPLIED)

    def persona(self):
        live = [t for t in self.jobs_tabs if t != DIED_TAB]
        return persona_for_tab(live[0]) if live else "Other"

    def excluded(self):
        """Kevin's call: a role that never got past matched and whose only CRM presence is the
        Died archive is not part of the funnel. A jobs-cache uuid or a dead_link event does not
        rescue it - neither can move a role past matched, and nearly every Died row is cached."""
        return self.stage == MATCHED and bool(self.tabs) and all(t == DIED_TAB for t in self.tabs)


def compute_role_funnel(crm_rows, outcome_events, tracked_uuids):
    """
    crm_rows:       list of dicts, one per CRM row on EVERY tab:
                    {"sheet_uuid": str, "tab": str, "schema": "JOBS"|"PEOPLE", "status": str, "note": str}
    outcome_events: iterable of (sheet_uuid, status) from application_outcomes
                    (status in applied, reply, interview, rejection, offer, withdrawn, dead_link)
    tracked_uuids:  set of sheet_uuids present in the SQLite jobs cache (carded roles)
    returns: {"matched", "applied", "replied", "interviewed", "offers", "rejected": int,
              "rates": {"matched_to_applied", "applied_to_replied",
                        "replied_to_interviewed", "interviewed_to_offer": float 1dp or None},
              "by_persona": {"Tetiana"|"Clavicular"|"Other": {same six counts}}}

    A sheet_uuid is a role if it has a JOBS-schema row, is in tracked_uuids, or has an outcome
    event with a known status. A JOBS row Kevin passed on or declined (Pass / Decline / No
    thanks) is Matched, not Applied and not Rejected; an applied/interview/offer event still
    lifts it, so a role he applied to and later declined shows as applied only when SQLite says
    so. PEOPLE rows only add evidence to an existing role; a PEOPLE row whose uuid is none of
    those is a networking contact and never counts. A JOBS row with a blank uuid is a role of its
    own that cannot be joined to any other evidence.
    """
    crm_rows = list(crm_rows or [])
    # Only an event with a known status is evidence. An unknown one (a typo, a future event type)
    # neither conjures a role nor lifts one, so it is dropped here, once, for both loops below.
    events = [(u, s) for u, s in (
        (str(u or "").strip(), str(s or "").strip().lower()) for u, s in (outcome_events or []))
        if u and s in _EVENT_STAGE]

    roles = {}
    for uuid in tracked_uuids or ():
        uuid = str(uuid or "").strip()
        if uuid:
            roles.setdefault(uuid, _Role())
    for uuid, _status in events:
        roles.setdefault(uuid, _Role())
    for row in crm_rows:
        uuid = str(row.get("sheet_uuid") or "").strip()
        if uuid and str(row.get("schema") or "").upper() == "JOBS":
            roles.setdefault(uuid, _Role())

    unkeyed = []
    for row in crm_rows:
        uuid = str(row.get("sheet_uuid") or "").strip()
        if uuid:
            if uuid in roles:
                roles[uuid].add_row(row)
        elif str(row.get("schema") or "").upper() == "JOBS":
            role = _Role()
            role.add_row(row)
            unkeyed.append(role)

    for uuid, status in events:
        roles[uuid].lift(*_EVENT_STAGE[status])

    overall = _empty_counts()
    by_persona = {p: _empty_counts() for p in PERSONAS}
    for role in list(roles.values()) + unkeyed:
        if role.excluded():
            continue
        persona_counts = by_persona[role.persona()]
        for stage, key in enumerate(_COUNT_KEYS):
            if role.stage >= stage:
                overall[key] += 1
                persona_counts[key] += 1
        if role.rejected:
            overall["rejected"] += 1
            persona_counts["rejected"] += 1

    result = dict(overall)
    result["rates"] = {
        "matched_to_applied": _rate(overall["applied"], overall["matched"]),
        "applied_to_replied": _rate(overall["replied"], overall["applied"]),
        "replied_to_interviewed": _rate(overall["interviewed"], overall["replied"]),
        "interviewed_to_offer": _rate(overall["offers"], overall["interviewed"]),
    }
    result["by_persona"] = by_persona
    return result
