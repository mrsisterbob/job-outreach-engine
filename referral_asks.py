"""Referral-ask drafts for /w: pure text logic, no I/O, no SQLite, no `main` import.

/w finds new roles at companies where Kevin has a warm contact. This module turns the roles found
at ONE company into ONE ready-to-send ask for the best contact there. Nothing here sends, drafts in
Gmail or attaches a file; main.py owns the table, the reads and the writes, and hands this module
already-computed strings (cleaned company, greeting name, resume bullet entries).

Hard rules, each with a test in test_referral_asks.py:
  - Every draft passes pipeline_utils.negative_language_hits() before it can be stored. A role whose
    title or link trips the gate on its own is left OUT; a draft with a hit is never returned.
  - The "why Kevin fits" sentence is a bullet taken VERBATIM from the resume bullet bank for the
    track the TITLE implies (track_registry.track_for_title). No title rule, no sentence:
    blank beats false. An employer is named only when the bullet's own metadata names one.
  - A requisition id is shown only when the board gave a non-empty value with no spaces (a Greenhouse
    board can return the placeholder "See Opening ID").
"""
import re
from datetime import date

import pipeline_utils as pu
import track_registry

# Verb-first bullets only: a first word is lower-cased for the endorsement when it is a plain
# capitalised past-tense verb. Anything else (an acronym, "SQL", "iPhone", a noun such as
# "Salesforce") makes the endorsement be dropped rather than guessed.
_IRREGULAR_VERBS = frozenset({
    "built", "ran", "set", "found", "gave", "wrote", "led", "took", "made", "kept", "held", "cut",
    "drove", "sent", "won", "taught", "grew", "began", "chose", "spoke",
})
_PLAIN_WORD_RE = re.compile(r"[A-Z][a-z]+(?:-[a-z]+)*")


def clean_inline(text) -> str:
    """One line: whitespace collapsed, straight double quotes made single (the endorsement is quoted)."""
    return re.sub(r"\s+", " ", str(text or "")).replace('"', "'").strip()


_PLACEHOLDER_REQ_IDS = frozenset({
    "n/a", "na", "null", "none", "nil", "tbd", "tba", "unknown", "undefined", "-", "--", "0",
})


def valid_requisition_id(raw) -> str:
    """The requisition id to show, or "". Empty, containing whitespace ("See Opening ID") or a
    common placeholder ("N/A", "null", "TBD", "-", "0") is not one."""
    value = str(raw or "").strip()
    if not value or re.search(r"\s", value) or value.lower() in _PLACEHOLDER_REQ_IDS:
        return ""
    return value


def build_role(title, link, requisition_id, job_hash) -> dict:
    """One role entry as stored in roles_json."""
    return {
        "title": clean_inline(title),
        "link": str(link or "").strip(),
        "req": valid_requisition_id(requisition_id),
        "job_hash": str(job_hash or ""),
    }


# The most roles ONE ask holds. main.WARM_RADAR_ROLES_PER_COMPANY is set from this, so the number a
# /w message lists for a company and the number one pending ask can hold are the same by construction.
REFERRAL_ASK_MAX_ROLES = 5
# A role title is cut to this many characters on the ask LINE only (roles_json keeps the full title).
ROLE_TITLE_MAX_CHARS = 120
# A finished ask must stay under one Telegram message (main.TELEGRAM_CHUNK_CHARS is 3900); /refer adds
# a header around it, so the draft itself stops a little short of that.
REFERRAL_ASK_MAX_CHARS = 3500


def role_usable(role) -> bool:
    """True when a role can stand on an ask line: it has a link (every draft carries the job link) and
    neither its title nor its finished line has negative wording. compose_ask leaves out any other role."""
    if not str(role.get("link") or "").strip():
        return False
    return not (pu.negative_language_hits(role.get("title")) or pu.negative_language_hits(_role_line(role)))


def merge_roles_counted(existing, new, taken_hashes=(), max_roles=REFERRAL_ASK_MAX_ROLES):
    """(merged, added, full): `existing` plus every role in `new` whose job_hash is not already in the
    ask, not in `taken_hashes` (roles in ANY ask, whatever its status) and not repeated inside `new`,
    until the ask holds `max_roles` USABLE roles. The cap is applied after the link and wording checks
    (role_usable): a role compose_ask will leave out takes no slot, so it never crowds out a good role
    behind it. Such a role is still returned in `merged`/`added` so compose_ask drops and reports it.
    `full` counts the usable, otherwise-new roles that did not fit; they are not added anywhere here,
    and an ask already past the cap (older data) just takes no more."""
    merged = [dict(r) for r in (existing or [])]
    seen = {r.get("job_hash") for r in merged} | set(taken_hashes or ())
    held = sum(1 for r in merged if role_usable(r))
    added, full = [], 0
    for role in new or []:
        h = role.get("job_hash")
        if not h or h in seen:
            continue
        seen.add(h)
        if role_usable(role):
            if max_roles is not None and held >= max_roles:
                full += 1
                continue
            held += 1
        merged.append(dict(role))
        added.append(dict(role))
    return merged, added, full


def merge_roles(existing, new, taken_hashes=(), max_roles=REFERRAL_ASK_MAX_ROLES):
    """(merged, added): merge_roles_counted without the count of roles that did not fit."""
    merged, added, _full = merge_roles_counted(existing, new, taken_hashes, max_roles)
    return merged, added


def derive_track(title, company="") -> str:
    """The track letter the TITLE alone implies, or "" when no title rule fires (or the employer is
    itself a financial-services business, which the registry refuses to override)."""
    return track_registry.track_for_title(title, employer=company) or ""


def _bullet_text(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("text"), str):
        return entry["text"]
    return ""


def bullet_employer(entry, experience) -> str:
    """The employer a bullet's OWN metadata names, else "". A bare string carries no metadata, and a
    dict only names one through an in-range integer source_job."""
    if not isinstance(entry, dict):
        return ""
    idx = entry.get("source_job")
    if isinstance(idx, bool) or not isinstance(idx, int):
        return ""
    if not isinstance(experience, list) or not 0 <= idx < len(experience):
        return ""
    job = experience[idx]
    return clean_inline(job.get("company")) if isinstance(job, dict) else ""


def lowercase_first_word(text):
    """`text` with its first letter lower-cased, or None when the first word is not a plain
    capitalised past-tense verb (so an acronym or a proper noun is never mangled)."""
    stripped = str(text or "").strip()
    if not stripped:
        return None
    word = stripped.split()[0].rstrip(",")
    if not _PLAIN_WORD_RE.fullmatch(word):
        return None
    base = word.split("-")[0].lower()
    if not (word.lower().endswith("ed") or base in _IRREGULAR_VERBS):
        return None
    return stripped[0].lower() + stripped[1:]


def pick_fit(title, company, entries_for_track, experience, sender_name="Kevin Miller"):
    """{"sentence", "endorsement", "bullet"} for one role, or None.

    `entries_for_track(track_letter)` returns that track's confirmed bullet entries (the main.py
    glue passes resume_engine.filter_ats_bullet_entries). The first entry whose finished text has
    no negative wording wins. `endorsement` is "" when its first word cannot be lower-cased safely.
    """
    track = derive_track(title, company)
    if not track:
        return None
    try:
        entries = entries_for_track(track) or []
    except Exception:
        return None
    clean_title = clean_inline(title)
    sender = clean_inline(sender_name) or "Kevin Miller"
    for entry in entries:
        bullet = clean_inline(_bullet_text(entry))
        if not bullet:
            continue
        employer = bullet_employer(entry, experience)
        sentence = (f"From my time at {employer}, here is one example: {bullet}" if employer
                    else f"Here is one example from my background: {bullet}")
        lowered = lowercase_first_word(bullet)
        endorsement = ""
        if lowered:
            core = lowered.rstrip(".")
            lead = f"At {employer}, " if employer else ""
            endorsement = f"{lead}{sender} {core} and is a strong fit for the {clean_title} role."
        if pu.negative_language_hits(sentence + " " + endorsement):
            continue
        return {"sentence": sentence, "endorsement": endorsement, "bullet": bullet}
    return None


def _ask_title(title) -> str:
    """The title as shown on an ask line: cut to ROLE_TITLE_MAX_CHARS, with "..." marking the cut."""
    text = str(title or "")
    if len(text) <= ROLE_TITLE_MAX_CHARS:
        return text
    return text[:ROLE_TITLE_MAX_CHARS - 3].rstrip() + "..."


def _role_line(role) -> str:
    line = f"- {_ask_title(role['title'])}: {role['link']}"
    if role.get("req"):
        line += f" (Req ID {role['req']})"
    return line


def render_ask(first_name, company, roles, sender_name, fit=None) -> str:
    """The finished text. `roles` must be non-empty. Wording follows the plan Kevin approved:
    one ask, no negations, "My resume is attached." as the reminder to attach it by hand."""
    n = len(roles)
    first = str(first_name or "").strip()
    greeting = f"Hi {first}," if first else "Hi,"
    if n == 1:
        opening, verb, target = "an opening", "matches", "it"
    else:
        opening, verb = f"{n} openings", "match"
        target = "either one" if n == 2 else "any of them"
    lines = [
        greeting,
        "",
        f"I hope your week is going well. {company} has {opening} that {verb} my background:",
        *[_role_line(r) for r in roles],
        "",
    ]
    if fit and fit.get("sentence"):
        lines += [fit["sentence"], ""]
    ask = f"Would you be willing to refer me for {target}?"
    if fit and fit.get("endorsement"):
        ask += (" If it helps, here is a line you can paste into the referral form: "
                f"\"{fit['endorsement']}\"")
    sign = (str(sender_name or "").split() or ["Kevin"])[0]
    lines += [ask, "", "My resume is attached.", "", "Thank you,", sign]
    return "\n".join(lines)


def compose_ask(first_name, company, roles, sender_name, entries_for_track, experience):
    """Render one contact's ask through the wording gate.

    Returns {"body": str or None, "kept": [roles], "left_out": [roles], "too_long": [roles],
    "fit_title": str}. A role is left out when it has no link (every draft must carry the job link) or
    when its own line (title, link, requisition id) or its title alone has a hit. A role dropped only
    because the finished text would pass REFERRAL_ASK_MAX_CHARS is in `too_long`, never in `left_out`.
    `body` is None when no role remains or the finished text still has a hit (the fit lines
    are dropped first, so a bad bullet costs the sentence rather than the draft). Never a draft
    with a hit. The fit sentence always names a role that is still in `kept`.
    """
    kept, left_out, too_long = [], [], []
    for role in roles or []:
        (kept if role_usable(role) else left_out).append(role)
    if not kept:
        return {"body": None, "kept": [], "left_out": left_out, "too_long": [], "fit_title": ""}

    def _fit_for(remaining):
        for role in remaining:
            candidate = pick_fit(role["title"], company, entries_for_track, experience, sender_name)
            if candidate:
                return candidate, role["title"]
        return None, ""

    fit, fit_title = _fit_for(kept)

    # One ask is one Telegram message: while the finished text is too long (only very long links get
    # it there once the title is cut and the role count is capped) the NEWEST role is left out.
    while True:
        for attempt in (fit, None):
            body = render_ask(first_name, company, kept, sender_name, attempt)
            if not pu.negative_language_hits(body):
                break
        else:
            return {"body": None, "kept": [], "left_out": left_out + kept + too_long, "too_long": [],
                    "fit_title": ""}
        if len(body) <= REFERRAL_ASK_MAX_CHARS or len(kept) <= 1:
            return {"body": body, "kept": kept, "left_out": left_out, "too_long": too_long,
                    "fit_title": fit_title if attempt else ""}
        too_long.append(kept.pop())
        fit, fit_title = _fit_for(kept)   # the sentence may have been about the role just cut


# ---- which warm contact a company's roles go to ----------------------------------------------------

_IMPORT_SUFFIX = "(LinkedIn import)"
_DATE_PREFIX_RE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}\]\s*")
_CONNECTED_RE = re.compile(r"Connected (\d{4})-(\d{2})-(\d{2})")


def contact_title_and_date(note, company=""):
    """(title, connected) read from a LinkedIn-import note, "" for what is not there.

    The note reads `<Title> at <Company>. Connected <YYYY-MM-DD> (LinkedIn import)`, optionally behind a
    `[YYYY-MM-DD] ` prefix and optionally followed by free text; a row with no title reads `Works at <Company>`. A title may itself contain
    " at " ("Analyst at Large Bank at Acme"), so the company is cut from the END: by name when
    `company` is given and the sentence ends with it, otherwise at the last " at ". Any other note
    ("Hiring manager", "Active relationship") has no title: nothing is guessed from free text.
    `connected` is the ISO date after "Connected", or "" when absent or not a real date.
    """
    text = _DATE_PREFIX_RE.sub("", str(note or "")).strip()
    # The date is read from "Connected YYYY-MM-DD" wherever it sits in the note (text typed after the
    # import line must not hide it); the title needs the "(LinkedIn import)" marker and is the text before it.
    connected = ""
    match = _CONNECTED_RE.search(text)
    if match:
        try:
            connected = date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            connected = ""
    marker = text.find(_IMPORT_SUFFIX)
    if marker < 0:
        return "", connected
    text = text[:marker]
    head = _CONNECTED_RE.search(text)
    if head:
        text = text[:head.start()]
    sentence = text.strip().rstrip(".").strip()
    company = str(company or "").strip()
    if company and sentence.lower() == f"works at {company}".lower():
        return "", connected
    if company and sentence.lower().endswith(f" at {company}".lower()):
        return sentence[:-len(f" at {company}")].strip(), connected
    if " at " not in sentence:
        return "", connected
    if sentence.startswith("Works at ") and sentence.count(" at ") == 1:
        return "", connected
    return sentence.rsplit(" at ", 1)[0].strip(), connected


def is_placeholder_name(name) -> bool:
    """True for the nameless-row placeholder get_warm_crm_contacts stores ("Contact") or an empty name."""
    return str(name or "").strip().lower() in ("", "contact")


def contact_sort_key(contact):
    """Sort key for the contacts at ONE company, best first: a real name before the placeholder, then
    higher priority, then a row with an email, then the more recent connection (a note with no date
    last). sorted() is stable, so full ties keep sheet order."""
    _title, connected = contact_title_and_date(contact.get("note"), contact.get("raw_company", ""))
    ordinal = date.fromisoformat(connected).toordinal() if connected else 0
    try:
        priority = float(contact.get("priority_score") or 0)
    except (TypeError, ValueError):
        priority = 0.0
    return (1 if is_placeholder_name(contact.get("name")) else 0, -priority,
            0 if str(contact.get("email") or "").strip() else 1, -ordinal)


def pick_contact(contacts, role_titles, company=""):
    """The one contact a company's roles go to, or None when `contacts` is empty.

    Only a contact with a sheet_uuid is eligible while any has one (an ask is keyed by it). Among
    them a real name beats the placeholder, then a higher priority always wins; only contacts tied at
    that top priority are compared by role fit: the one whose imported title implies the same track
    as the company's top role (the first of `role_titles`). With no match, or no title rule firing
    on either side, the best by contact_sort_key (email, then recency, then sheet order) is taken.
    """
    people = [c for c in (contacts or []) if c]
    if not people:
        return None
    with_uuid = [c for c in people if str(c.get("sheet_uuid") or "").strip()]
    ranked = sorted(with_uuid or people, key=contact_sort_key)
    top = ranked[0]
    tied = [c for c in ranked if contact_sort_key(c)[:2] == contact_sort_key(top)[:2]]
    role_track = derive_track(role_titles[0], company) if role_titles else ""
    if role_track:
        for c in tied:
            title, _date = contact_title_and_date(c.get("note"), c.get("raw_company", ""))
            if title and derive_track(title, company) == role_track:
                return c
    return top
