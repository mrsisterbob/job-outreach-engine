"""One view of what every paid /w source has spent. Pure: no I/O, no clock reads (`now` is passed in),
no import from main.

Each source is reported the same way: unit, this run, this month, cap, what is left, and where the
figure came from. A figure that cannot be read is None and is printed as `unknown`, never as 0: an
unreadable ledger must not look like an untouched budget.
"""
from collections import namedtuple
from datetime import datetime, timezone

SpendRow = namedtuple("SpendRow", "source unit this_run this_month cap left basis note")

HIREBASE = "Hirebase"
HIREBASE_LOOKUPS = "Hirebase lookups"
APIFY = "Apify Feed"
RESOLVER = "ATS resolver"

_CONSOLE_GAP_USD = 0.005  # DESIGN CHOICE, not a measurement: half a cent


def _ts(ts, now):
    """Aware-UTC datetime; malformed or future timestamps count as `now` (never as an old month)."""
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return now
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return now if dt > now else dt


def _utc(now):
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _ledger_month_usd(rows, cap_usd, now):
    """Sum of this UTC month's rows, errored ones included (a failed call may still have billed). An unparseable
    amount counts as the whole cap: unknown spend never grants budget. None when `rows` is None."""
    if rows is None:
        return None
    total = 0.0
    for ts, usd, _status, _keys in rows:
        dt = _ts(ts, now)
        if (dt.year, dt.month) != (now.year, now.month):
            continue
        try:
            total += max(0.0, float(usd))
        except (TypeError, ValueError):
            total += float(cap_usd)
    return total


def _dollar_row(source, spec, run_amount, now):
    cap = float(spec.get("cap_usd") or 0.0)
    ledger = _ledger_month_usd(spec.get("ledger_rows"), cap, now)
    console = spec.get("console_month_usd")
    note = ""
    if ledger is None and console is None:
        month, basis, note = None, "unknown", "ledger unreadable"
    elif ledger is None:
        month, basis = float(console), "console"
    elif console is not None and float(console) > ledger:
        month, basis = float(console), "console"
        if float(console) - ledger > _CONSOLE_GAP_USD:
            note = f"console is higher by ${float(console) - ledger:.2f} (runs outside /w)"
    else:
        month, basis = ledger, "ledger"
    left = None if month is None else max(0.0, cap - month)
    if cap <= 0:
        left, note = 0.0, "off"
    return SpendRow(source, "usd", run_amount, month, cap, left, basis, note)


def _count_row(source, unit, spec, run_amount, held_back=0):
    cap = int(spec.get("cap") or 0)
    used = spec.get("month_used") if unit == "jobs" else spec.get("month_units")
    left = None if used is None else max(0, cap - int(held_back) - int(used))
    note = f"held back {int(held_back)}" if held_back else ""
    return SpendRow(source, unit, run_amount, None if used is None else int(used), cap, left,
                    "unknown" if used is None else "ledger", note)


def build_rows(hirebase, hirebase_lookups, apify, resolver, run, now):
    """One SpendRow per source that is on. A source argument of None (off, or not built) produces no row.
    hirebase {"month_used", "cap", "held_back"}; hirebase_lookups {"month_units", "cap"};
    apify / resolver {"ledger_rows", "console_month_usd", "cap_usd"}; run {source name: amount this run}."""
    now, run = _utc(now), run or {}
    rows = []
    if hirebase is not None:
        rows.append(_count_row(HIREBASE, "jobs", hirebase, run.get(HIREBASE, 0), hirebase.get("held_back") or 0))
    if hirebase_lookups is not None:
        rows.append(_count_row(HIREBASE_LOOKUPS, "units", hirebase_lookups, run.get(HIREBASE_LOOKUPS, 0)))
    if apify is not None:
        rows.append(_dollar_row(APIFY, apify, run.get(APIFY, 0.0), now))
    if resolver is not None:
        rows.append(_dollar_row(RESOLVER, resolver, run.get(RESOLVER, 0.0), now))
    return rows


def _usd_text(v):
    return f"${v:.3f}" if 0 < v < 0.01 else f"${v:.2f}"


def _num(row, v):
    if v is None:
        return "unknown"
    return _usd_text(v) if row.unit == "usd" else str(int(v))


def render_lines(rows):
    """One line per row, e.g. `Apify Feed: $0.60 this run, $1.35 of $10.00 this month ($8.65 left, console)`."""
    lines = []
    for r in rows:
        if r.note == "off":
            lines.append(f"{r.source}: off (cap is 0)")
            continue
        unit = " " + r.unit if r.unit != "usd" else ""
        month = "unknown this month" if r.this_month is None else f"{_num(r, r.this_month)} of {_num(r, r.cap)} this month"
        extras = []
        if r.left is not None:
            extras.append(f"{_num(r, r.left)} left")
        if r.unit != "usd" and r.note:
            extras.append(r.note)
        if r.unit == "usd" and r.basis == "console":
            extras.append("console")
        if r.this_month is None and r.note:
            extras.append(r.note)
        line = f"{r.source}: {_num(r, r.this_run)}{unit} this run, {month}"
        if r.this_month is None:
            line += f" (cap {_num(r, r.cap)})" + (f" ({', '.join(extras)})" if extras else "")
        elif extras:
            line += f" ({', '.join(extras)})"
        if r.left == 0:
            line += ", CAP REACHED"
        lines.append(line)
    return lines


def at_cap(rows):
    """Source names whose `left` is exactly 0. A None left is NOT at cap and NOT ok: see unknown_sources."""
    return {r.source for r in rows if r.left == 0}


def unknown_sources(rows):
    """Source names whose month figure could not be read."""
    return {r.source for r in rows if r.left is None}
