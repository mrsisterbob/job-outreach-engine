"""Referral-ask drafts for /w: pure text logic, no I/O, no SQLite, no `main` import.

/w finds new roles at companies where Kevin has a warm contact. This module turns the roles found
at ONE company into ONE ready-to-send ask for the best contact there. Nothing here sends, drafts in
Gmail or attaches a file; main.py owns the table, the reads and the writes, and hands this module
already-computed strings (cleaned company, greeting name, resume bullet entries).

Hard rules, each with a test in test_referral_asks.py:
  - Every draft passes pipeline_utils.negative_language_hits() before it can be stored. A role whose
    title or link trips the gate on its own is left OUT; a draft with a hit is never returned.
  - The "why Kevin fits" sentence is a bullet taken VERBATIM from the resume bullet bank for the
    track the TITLE implies (track_registry.override_track_for_title). No title rule, no sentence:
    blank beats false. An employer is named only when the bullet's own metadata names one.
  - A requisition id is shown only when the board gave a non-empty value with no spaces (a Greenhouse
    board can return the placeholder "See Opening ID").
"""
import re

import pipeline_utils as pu
import track_registry

# override_track_for_title() hands a track back unchanged unless the current one is finance-framed,
# and no title rule ever selects "a". So starting from "a", a different answer means a rule fired.
_PROBE_TRACK = "a"

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


def merge_roles(existing, new, taken_hashes=()):
    """(merged, added): `existing` plus every role in `new` whose job_hash is not already in the ask,
    not in `taken_hashes` (roles in ANY ask, whatever its status) and not repeated inside `new`."""
    merged = [dict(r) for r in (existing or [])]
    seen = {r.get("job_hash") for r in merged} | set(taken_hashes or ())
    added = []
    for role in new or []:
        h = role.get("job_hash")
        if not h or h in seen:
            continue
        seen.add(h)
        merged.append(dict(role))
        added.append(dict(role))
    return merged, added


def derive_track(title, company="") -> str:
    """The track letter the TITLE alone implies, or "" when no title rule fires (or the employer is
    itself a financial-services business, which the registry refuses to override)."""
    letter = track_registry.override_track_for_title(_PROBE_TRACK, title, employer=company)
    return "" if letter == _PROBE_TRACK else letter


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


def _role_line(role) -> str:
    line = f"- {role['title']}: {role['link']}"
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

    Returns {"body": str or None, "kept": [roles], "left_out": [roles], "fit_title": str}.
    A role is left out when it has no link (every draft must carry the job link) or when its own
    line (title, link, requisition id) or its title alone has a hit. `body` is None when no role remains or the finished text still has a hit (the fit lines
    are dropped first, so a bad bullet costs the sentence rather than the draft). Never a draft
    with a hit.
    """
    kept, left_out = [], []
    for role in roles or []:
        if not str(role.get("link") or "").strip():
            left_out.append(role)
        elif pu.negative_language_hits(role.get("title")) or pu.negative_language_hits(_role_line(role)):
            left_out.append(role)
        else:
            kept.append(role)
    if not kept:
        return {"body": None, "kept": [], "left_out": left_out, "fit_title": ""}

    fit, fit_title = None, ""
    for role in kept:
        candidate = pick_fit(role["title"], company, entries_for_track, experience, sender_name)
        if candidate:
            fit, fit_title = candidate, role["title"]
            break

    for attempt in (fit, None):
        body = render_ask(first_name, company, kept, sender_name, attempt)
        if not pu.negative_language_hits(body):
            return {"body": body, "kept": kept, "left_out": left_out,
                    "fit_title": fit_title if attempt else ""}
    return {"body": None, "kept": [], "left_out": left_out + kept, "fit_title": ""}
