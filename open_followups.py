"""Every follow-up Kevin still owes, merged from the saved daily queue snapshots. Pure: no I/O.

Each 07:30 sequencer run snoozes every contact it lists to the next rung, so a contact not acted
on that morning drops out of /followups, /queue and /overdue until the ladder kills it. The
snapshots saved by save_followup_queue_snapshot() still hold those listings; this module merges
them, against a fresh read of the CRM and the Sent folder, into one list of what is still owed.

The caller (main.py) does the reads and passes the results in, including `today`.
"""
from datetime import datetime

from pipeline_utils import carmen_reply_anchor, is_followup_unscheduled


def _parse_date(value):
    """'YYYY-MM-DD' (first 10 chars) -> date, or None when blank / sentinel / unparseable."""
    text = str(value or "").strip()[:10]
    if is_followup_unscheduled(text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _norm_email(value):
    return str(value or "").strip().lower()


def collect_open_followups(snapshots, live_rows, last_sent, today):
    """
    snapshots: {run_date "YYYY-MM-DD": result dict as saved by save_followup_queue_snapshot}
    live_rows: {sheet_uuid: rec} from a FRESH read of the sequencer's scan tabs
    last_sent: {email_lower: datetime.date of the most recent Sent message to that address},
               or None when the Sent folder could not be checked
    today:     datetime.date
    returns:   list of dicts, oldest-due first
    """
    sent_checked = last_sent is not None
    sent = {}
    for address, sent_on in (last_sent or {}).items():
        key = _norm_email(address)
        if sent_on is not None and (key not in sent or sent_on > sent[key]):
            sent[key] = sent_on
    live_rows = live_rows or {}

    # uuid -> [(run_date, entry)], only listings still unsatisfied by a Sent message.
    open_listings = {}
    for run_date_text, result in (snapshots or {}).items():
        run_date = _parse_date(run_date_text)
        if run_date is None or not isinstance(result, dict):
            continue
        # followups_ready only: applications_quiet is watch-only, nothing is owed on it.
        for entry in result.get("followups_ready") or []:
            uuid = entry.get("sheet_uuid")
            # The sequencer never snoozes a row without a uuid, so it reappears on its own.
            if not uuid:
                continue
            # Killed (moved to the ghost tab) or promoted to Carmen Hot: off the scanned tabs.
            if uuid not in live_rows:
                continue
            sent_on = sent.get(_norm_email(entry.get("email")))
            # Same-day counts: listed at 07:30, sent at 10:00.
            if sent_on is not None and sent_on >= run_date:
                continue
            open_listings.setdefault(uuid, []).append((run_date, entry))

    items = []
    for uuid, listings in open_listings.items():
        listings.sort(key=lambda pair: pair[0])
        first_date, first_entry = listings[0]
        latest_date, latest = listings[-1]

        # Read the note from the LIVE row: the reply lands after the snapshot was taken.
        replied_on = carmen_reply_anchor((live_rows.get(uuid) or {}).get("note"))
        if replied_on is not None and replied_on >= first_date:
            continue

        due = _parse_date(first_entry.get("next_followup")) or first_date

        # The snapshot's progress is as of its own run (and a list after json.loads). Shift it to
        # today. Not clamped: "day 23 of 21" means overdue for its kill, which is worth seeing.
        progress = latest.get("progress")
        if progress:
            day, total = progress[0], progress[1]
            progress = (int(day) + (today - latest_date).days, int(total))
        else:
            progress = None

        items.append({
            "sheet_uuid": uuid,
            "name": latest.get("name") or "",
            "company": latest.get("company") or "",
            "company_raw": latest.get("company_raw") or "",
            "email": latest.get("email") or "",
            "role": latest.get("role") or "",
            "short_id": latest.get("short_id"),
            "attempt": latest.get("attempt"),
            "draft_text": latest.get("draft_text") or "",
            "run_date": latest_date.strftime("%Y-%m-%d"),
            "owed_since": first_date.strftime("%Y-%m-%d"),
            "days_owed": (today - first_date).days,
            "due": due.strftime("%Y-%m-%d"),
            "progress": progress,
            "listings": len(listings),
            "sent_checked": sent_checked,
        })

    items.sort(key=lambda item: (item["due"], item["name"], item["sheet_uuid"]))
    return items
