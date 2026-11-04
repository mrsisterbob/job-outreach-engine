"""Hot promotion: decide when a two-way exchange earns a place in Carmen Hot.

Pure logic, no I/O, no imports from main.py. Kevin's rule (2026-10-28, reverses the old
"Carmen Hot is manual /promote only"): a real person goes to Carmen Hot once the Gmail thread
holds BOTH one inbound reply from them AND one message Kevin sent. A first send alone never
promotes. The poller (TASK-228, TASK-229) gathers the facts and calls these functions.
"""
import html
import re
from collections import namedtuple
from datetime import date, datetime, timedelta

from pipeline_utils import (
    INBOUND_REPLY_NOTE_MARKER,
    inbound_sender_screen_reason,
    is_automated_sender,
    is_role_mailbox,
)

HOT_FOLLOWUP_DAYS = 7
LOOKUP_FAILED = "lookup_failed"

# Kevin named Cold and Warm. The three job tabs are included for parity with today's behaviour:
# route_inbound_reply_to_crm (main.py) already moves a job-tab contact to Carmen Cold on a
# GENERAL reply. Carmen Hot, Killed and Died are never moved.
PROMOTABLE_SOURCE_TABS = (
    "Carmen Cold", "Carmen Warm", "Tetiana Cold", "Tetiana Warm", "Clavicular",
)

PromotionDecision = namedtuple("PromotionDecision", "action reason")

_ADDR_IN_BRACKETS = re.compile(r"<\s*([^<>@\s]+@[^<>@\s]+)\s*>")
_BARE_ADDR = re.compile(r"[^<>@\s,]+@[^<>@\s,]+")
_AUTO_PRECEDENCE = frozenset({"bulk", "junk", "auto_reply"})


def sender_address(raw):
    """Lower-cased address from 'Name <a@b.com>' or a bare address; '' when there is none."""
    text = str(raw or "")
    match = _ADDR_IN_BRACKETS.search(text)
    if match:
        return match.group(1).strip().lower()
    bare = _BARE_ADDR.search(text)
    return bare.group(0).strip().lower() if bare else ""


def _strip_plus_tag(addr):
    local, _, domain = addr.partition("@")
    return f"{local.split('+', 1)[0]}@{domain}"


def sender_disqualifier(raw_sender, own_addresses, ats_domains=(), auto_reply=False):
    """Why this sender cannot earn Carmen Hot, or '' when they can. First hit wins."""
    addr = sender_address(raw_sender)
    if not addr:
        return "no_address"
    if isinstance(own_addresses, str):
        own_addresses = (own_addresses,)
    if isinstance(ats_domains, str):
        ats_domains = (ats_domains,)
    own = {_strip_plus_tag(sender_address(a)) for a in (own_addresses or ()) if sender_address(a)}
    if _strip_plus_tag(addr) in own:
        return "own_address"
    if auto_reply:
        return "auto_reply"
    if is_role_mailbox(_strip_plus_tag(addr)):
        return "role_mailbox"
    domain = addr.rsplit("@", 1)[-1].strip(".")
    if any(domain == d or domain.endswith("." + d) for d in (ats_domains or ())):
        return "ats_domain"
    if is_automated_sender(addr):
        return "automated_sender"
    if inbound_sender_screen_reason(addr, (), False):
        return "relay_domain"
    return ""


def auto_reply_flag(headers):
    """True when lower-cased header name -> value marks a machine auto-reply (out of office)."""
    h = headers or {}
    auto_submitted = h.get("auto-submitted")
    if auto_submitted is not None and str(auto_submitted).strip().lower() != "no":
        return True
    if str(h.get("precedence", "")).strip().lower() in _AUTO_PRECEDENCE:
        return True
    return "x-autoreply" in h or "x-autorespond" in h


def decide_promotion(status_label, kevin_sent_in_thread, live_tab, disqualifier=""):
    """Decide create / move / none. live_tab is the LIVE CRM lookup: a tab name, None when the
    CRM answered and the person is nowhere, or LOOKUP_FAILED."""
    if disqualifier:
        return PromotionDecision("none", disqualifier)
    if status_label != "GENERAL":
        return PromotionDecision("none", "status_event")
    if not kevin_sent_in_thread:
        return PromotionDecision("none", "no_kevin_message")
    if live_tab == LOOKUP_FAILED:
        return PromotionDecision("none", "lookup_failed")
    if live_tab is None:
        return PromotionDecision("create", "new_person")
    if live_tab == "Carmen Hot":
        return PromotionDecision("none", "already_hot")
    if live_tab in PROMOTABLE_SOURCE_TABS:
        return PromotionDecision("move", "two_way_exchange")
    if live_tab in ("Killed", "Died"):
        return PromotionDecision("none", "archived")
    return PromotionDecision("none", "unknown_tab")


def _known_ms(value):
    """A non-negative integer epoch-ms from an int or digit string, else None (unknown)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    return None


def kevin_sent_before(messages, message_id, internal_date_ms):
    """True when the Gmail thread holds a message Kevin SENT strictly BEFORE the inbound one.

    messages is the `messages` list of a threads.get response (id, labelIds, maybe internalDate
    as an epoch-ms string); message_id is the inbound message's Gmail id (may be ""); and
    internal_date_ms is that message's own internalDate. The inbound message never counts as
    Kevin's, even when it carries SENT. Order per SENT message: both times known -> sent time
    strictly less than inbound time; otherwise list position, only when the inbound message is
    in the list by id (Gmail returns thread messages oldest first: UNVERIFIED in this repo);
    otherwise it does not count.

    internalDate being present on format=minimal thread messages is UNVERIFIED in this repo
    (recalled from Gmail's resource shape; nothing in main.py or the fakes reads it from
    threads.get), which is why the position rule exists. Unknown order errs toward False (no
    promotion), the same direction as is_thread_kevin_started. Never raises; returns a bool.
    """
    if not isinstance(messages, list):
        return False
    entries = [m for m in messages if isinstance(m, dict)]
    inbound_idx = None
    inbound_ms = _known_ms(internal_date_ms)
    if message_id:
        for i, m in enumerate(entries):
            if m.get("id") == message_id:
                inbound_idx = i
                own_ms = _known_ms(m.get("internalDate"))
                if own_ms is not None:
                    inbound_ms = own_ms
                break
    for i, m in enumerate(entries):
        labels = m.get("labelIds")
        if not isinstance(labels, list) or "SENT" not in labels:
            continue
        if message_id and m.get("id") == message_id:
            continue
        sent_ms = _known_ms(m.get("internalDate"))
        if sent_ms is not None and inbound_ms is not None:
            if sent_ms < inbound_ms:
                return True
            continue
        if inbound_idx is not None and i < inbound_idx:
            return True
    return False


def exchange_date_from_internal_ms(internal_date_ms, fallback):
    """Local date of a Gmail internalDate (epoch ms); fallback when missing or unparseable."""
    try:
        return datetime.fromtimestamp(int(internal_date_ms) / 1000).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return fallback


def hot_followup_date(exchange_date):
    """YYYY-MM-DD, exchange date + HOT_FOLLOWUP_DAYS."""
    return (exchange_date + timedelta(days=HOT_FOLLOWUP_DAYS)).strftime("%Y-%m-%d")


def _clean_subject(subject):
    return re.sub(r"\s+", " ", str(subject or "")).strip()[:120]


def _origin(action, source_tab):
    if action == "move":
        return f"moved from {source_tab}"
    return "new contact, not previously in the CRM"


def promotion_note(exchange_date, action, source_tab, subject):
    """One dated notes line; carmen_reply_anchor reads the marker back as the reply date."""
    return (
        f"[{exchange_date.strftime('%Y-%m-%d')}] {INBOUND_REPLY_NOTE_MARKER}: they replied and "
        f"Kevin sent a message in the same thread; {_origin(action, source_tab)}; "
        f"follow-up {hot_followup_date(exchange_date)}; subject: {_clean_subject(subject)}"
    )


def promotion_alert_html(name, email, company, action, source_tab, exchange_date, subject):
    """Telegram HTML naming who was promoted to Carmen Hot and why."""
    esc = lambda v: html.escape(str(v or ""))  # noqa: E731
    who = f"<b>{esc(name)}</b>"
    if company:
        who += f" ({esc(company)})"
    lines = [
        f"\U0001F525 {who} is now in Carmen Hot",
        f"Address: {esc(email)}",
        "Why: they replied and you had sent a message in the thread "
        f"({esc(exchange_date.strftime('%Y-%m-%d'))})",
    ]
    if action == "move":
        lines.append(f"Moved from: {esc(source_tab)}")
    else:
        lines.append("Added as a new contact")
    lines.append(f"Follow-up: {esc(hot_followup_date(exchange_date))}")
    subj = _clean_subject(subject)
    if subj:
        lines.append(f"Subject: {esc(subj)}")
    return "\n".join(lines)
