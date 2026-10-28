"""One DERIVED record per company: what covers it, and the share of warm contacts covered.

Pure: no I/O, no clock, no main.py. Reads plain dicts of the four stores (company_identities,
hirebase_slug_registry, company_feed_source, company_resolution) and invents no new data.
"""
from datetime import datetime, timezone

COMPANIES_HEADER = (
    "key", "display", "contacts", "board_system", "board_token", "covered_by",
    "feed", "last_result", "last_checked_at", "next_eligible_at", "covered",
)


def _s(value):
    """Any value as stripped text; None and non-text junk read as blank."""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _ts(value):
    """Aware datetime for an ISO text, or None when blank or unparseable."""
    text = _s(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _newest(values):
    best, best_dt = "", None
    for v in values:
        dt = _ts(v)
        if dt is not None and (best_dt is None or dt > best_dt):
            best, best_dt = _s(v), dt
    return best


def _earliest(values):
    best, best_dt = "", None
    for v in values:
        dt = _ts(v)
        if dt is not None and (best_dt is None or dt < best_dt):
            best, best_dt = _s(v), dt
    return best


def _int(value):
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _identity(identities, key):
    try:
        ident = identities.get(key)
        return _s(ident.get("ats_slug")), _s(ident.get("workday_board"))
    except Exception:
        return "", ""


def _registry(registry, key):
    try:
        e = registry.get(key)
        return _s(e.slug), _s(e.status), _s(e.checked_at)
    except Exception:
        return "", "", ""


def _feed(feed_source, key):
    """(job_board, rows, checked_at) or None when nothing usable is recorded."""
    try:
        row = feed_source.get(key)
        domain, board, rows, checked = row
        return _s(board), _int(rows), _s(checked), True
    except Exception:
        return "", 0, "", False


def _resolution_rows(resolution, key):
    out = []
    try:
        items = list(resolution.items())
    except Exception:
        return out
    for k, val in items:
        try:
            if k[0] != key:
                continue
            outcome, attempted, nxt = val
            out.append((_s(k[1]), _s(outcome), _s(attempted), _s(nxt)))
        except Exception:
            continue
    return out


def build_source_records(companies, identities, registry, feed_source, resolution, now):
    """One dict per company, sorted contacts descending then key ascending.

    board_system is "workday" when a workday_board is set, "ats" when only ats_slug is set. The
    stored slug carries NO system name (Greenhouse/Lever/Ashby/SmartRecruiters is recorded
    nowhere), so none is invented. Workday triples are assumed already validated by the caller.
    covered_by: board, then hirebase (status covered + slug), then feed (rows > 0). A malformed
    entry reads as nothing recorded for that source; this never raises. `now` is reserved.
    """
    records = []
    try:
        items = list(companies)
    except Exception:
        return records
    for item in items:
        try:
            key, display, contacts = item
        except Exception:
            continue
        key = _s(key)
        slug, workday = _identity(identities, key)
        reg_slug, reg_status, reg_checked = _registry(registry, key)
        feed_name, feed_rows, feed_checked, feed_seen = _feed(feed_source, key)
        res = _resolution_rows(resolution, key)

        board_token = workday or slug
        board_system = "workday" if workday else ("ats" if slug else "")
        feed_label = feed_name or "apify"
        if board_token:
            covered_by = "board"
        elif reg_status == "covered" and reg_slug:
            covered_by = "hirebase"
        elif feed_rows > 0:
            covered_by = "feed"
        else:
            covered_by = ""

        if covered_by == "board":
            last_result = "board"
        elif covered_by == "hirebase":
            last_result = "hirebase:covered"
        elif covered_by == "feed":
            last_result = feed_label + ":rows"
        else:
            newest = _newest(r[2] for r in res)
            latest = [r for r in res if newest and r[2] == newest]
            if latest:
                last_result = (latest[0][0] or "apify") + ":" + (latest[0][1] or "unknown")
            elif reg_status == "no_jobs":
                last_result = "hirebase:no_jobs"
            elif feed_seen:
                last_result = feed_label + ":empty"
            else:
                last_result = "none"

        records.append({
            "key": key,
            "display": _s(display),
            "contacts": _int(contacts),
            "board_system": board_system,
            "board_token": board_token,
            "covered_by": covered_by,
            "feed": feed_label if covered_by == "feed" else "",
            "last_result": last_result,
            "last_checked_at": _newest([reg_checked, feed_checked] + [r[2] for r in res]),
            "next_eligible_at": "" if covered_by else _earliest(r[3] for r in res),
            "covered": covered_by != "",
        })
    records.sort(key=lambda r: (-r["contacts"], r["key"]))
    return records


def covered_contact_share(records):
    """(covered_contacts, total_contacts): sums contacts, never counts records."""
    covered = total = 0
    for r in records or []:
        try:
            n = _int(r["contacts"])
            total += n
            if r["covered"]:
                covered += n
        except Exception:
            continue
    return covered, total


def record_row(record):
    """Sheet row in COMPANIES_HEADER order: all text, covered as yes/no, never None."""
    out = []
    for col in COMPANIES_HEADER:
        try:
            v = record.get(col)
        except Exception:
            v = None
        if col == "covered":
            out.append("yes" if v else "no")
        else:
            out.append(_s(v))
    return out
