"""Decides which unsolicited email is HELD quietly instead of pinging Kevin, and words the standup line
that counts what was held. Pure: no I/O, no clock, no import from main.

THE RULE: a mail from a sender with no CRM identity is held unless it is a REAL reply or a hiring
signal. Not held (pings as today): Tier 1 (calendar invite, interview or offer), a REJECTION, and any
direct reply. A direct reply is the pre-filter's own test (gate 9, "Require direct reply"): an
In-Reply-To header, a References header, or a subject starting with `re:` (case-insensitive, trimmed).
A cold recruiter answering Kevin's outreach ("Re: Kevin Miller") therefore still pings; an unsolicited
mail with none of those is held. This helper is separate from the pre-filter on purpose: that flag is
off by default and drops mail without leaving a trace; a held mail is logged where Kevin can see it.
"""
from __future__ import annotations

import html

DEFAULT_MODE = "hold"
HOLD_REASON = "unsolicited, not a reply"
_NAME_CAP = 40
_SUBJECT_CAP = 40
_ALERT_MODES = ("alert", "alerts", "permissive")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def is_direct_reply(in_reply_to, references, subject):
    """True with an In-Reply-To header, a References header, or a subject starting `re:` (colon required, so
    `Reminder: ...` and a bare `Re` do not count). None and non-string inputs are empty."""
    if _text(in_reply_to) or _text(references):
        return True
    return _text(subject).lower().startswith("re:")


def parse_mode(raw):
    """`hold` or `alert` from an env string. alert / alerts / permissive -> alert; blank, None or a typo -> hold."""
    return "alert" if _text(raw).lower() in _ALERT_MODES else DEFAULT_MODE


def hold_reason(*, is_tier1, status_label, in_reply_to, references, subject, mode=DEFAULT_MODE):
    """"" (do not hold, ping as today) or "unsolicited, not a reply". mode "alert" never holds; any other value,
    including None, is "hold". A REJECTION is never held: rejection routing runs on the alert path."""
    if mode == "alert":
        return ""
    if is_tier1:
        return ""
    if _text(status_label).upper() == "REJECTION":
        return ""
    if is_direct_reply(in_reply_to, references, subject):
        return ""
    return HOLD_REASON


def _clean(value, cap):
    text = " ".join(str(value or "").split())   # a newline or tab becomes a space
    if len(text) > cap:
        text = text[:cap - 1].rstrip() + "…"
    return html.escape(text, quote=False)


def format_held_line(rows, limit=3):
    """The standup line. rows: [(sender_name, sender_email, subject)] newest first, or None (the read failed).
    "" for no rows (a quiet day adds no line); None -> "held-mail count unavailable" (an unreadable tray is never a
    zero). Names and subjects are cut at 40 characters, HTML-escaped, newlines flattened. Never prints a body."""
    if rows is None:
        return "📪 held-mail count unavailable this morning"
    rows = list(rows)
    if not rows:
        return ""
    shown = []
    for name, email, subject in rows[:max(0, limit)]:
        who = _clean(name, _NAME_CAP) or _clean(email, _NAME_CAP)
        subj = _clean(subject, _SUBJECT_CAP) or "(no subject)"
        shown.append(f'{who} - "{subj}"')
    n = len(rows)
    head = f"📪 {n} unsolicited email{'' if n == 1 else 's'} held, not pinged"
    extra = n - len(shown)
    parts = shown + ([f"… (+{extra} more)"] if extra > 0 else [])
    return head + (": " + " · ".join(parts) if parts else "")
