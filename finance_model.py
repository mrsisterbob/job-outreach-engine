"""Pure view model for the /finance page: stored personal snapshots in, JSON-safe dict out.

No I/O, no env, no clock: `now` is passed in (aware UTC datetime). Snapshots come newest first
from personal_snapshots.load_recent, each a dict as stored (carries the server's `received_at`).

SNAPSHOT CONTRACT the PC pushes (this docstring is the only description of it):

    {
     "money": {
       "as_of": "2026-11-05",            # ISO date or datetime, supplied by the PC
       "period": "2026-10",              # label only, shown as given
       "flow": [ {"category": "Groceries", "bucket": "Needs", "amount": 412.50}, ... ],
       "off_budget": [ {"name": "Emergency fund", "balance": 5200.0, "target": 10000.0}, ... ]
     },                                   # target optional
     "time": {
       "as_of": "2026-11-05", "week_of": "2026-11-02",
       "planned": [ {"category": "Sleep", "hours": 56}, ... ],
       "actual":  [ {"category": "Sleep", "hours": 49.5}, ... ]
     }
    }

Category names are VALUES in lists, never dict keys, on purpose: the store rejects any key
containing merchant, payee, vendor, transaction, txn, counterparty, memo, line_item. Everything
is aggregate; a row is never one purchase.

Rules: each section comes from the newest snapshot where that key is a dict (a newer snapshot
carrying only `time` never hides an older `money`). Staleness is per section from its own
`as_of`, never from `received_at`. Bad rows are counted in `unreadable` and skipped, never
zeroed. Nothing is rescaled to fit 168 hours. Only the documented fields reach the view.
"""
import math
from datetime import datetime, timedelta, timezone

MONEY_STALE_AFTER_HOURS = 72    # DESIGN CHOICE, not a measurement
TIME_STALE_AFTER_HOURS = 192    # DESIGN CHOICE, not a measurement (8 days)

MAX_FLOW_ROWS = 200
MAX_OFF_BUDGET_ROWS = 50
MAX_TIME_CATEGORIES = 60
MAX_NAME_LEN = 60
WEEK_HOURS = 168
_FUTURE_SLACK = timedelta(days=1)
_AS_OF_ECHO_CAP = 40
MAX_HOURS_ROWS = 200   # per planned/actual list; extras counted unreadable


def _num(v):
    """int/float only; not bool, not NaN/inf. Returns float or None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _name(v):
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or len(s) > MAX_NAME_LEN:
        return None
    return s


def _parse_as_of(v):
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _freshness(section, now, stale_after):
    as_of_raw = section.get("as_of")
    dt = _parse_as_of(as_of_raw)
    age = None
    if dt is not None:
        try:
            if dt - now <= _FUTURE_SLACK:
                age = max(0.0, (now - dt).total_seconds() / 3600.0)
        except OverflowError:
            age = None
    if age is None:
        # unparseable: echo only a capped string, never the raw value
        echo = as_of_raw.strip()[:_AS_OF_ECHO_CAP] if isinstance(as_of_raw, str) else None
        return {"as_of": echo or None,
                "as_of_known": False, "age_hours": None, "stale": True}
    return {"as_of": as_of_raw.strip()[:_AS_OF_ECHO_CAP], "as_of_known": True,
            "age_hours": round(age, 1), "stale": age > stale_after}


def _label(v):
    return v.strip()[:MAX_NAME_LEN] if isinstance(v, str) and v.strip() else None


def _rows(section, key):
    v = section.get(key)
    return v if isinstance(v, list) else []


def _money(section, now, received_at):
    unreadable = 0
    flow = {}   # (bucket, category) -> amount
    rows = _rows(section, "flow")
    unreadable += max(0, len(rows) - MAX_FLOW_ROWS)
    for r in rows[:MAX_FLOW_ROWS]:
        if not isinstance(r, dict):
            unreadable += 1
            continue
        amt, cat, bkt = _num(r.get("amount")), _name(r.get("category")), _name(r.get("bucket"))
        if amt is None or amt < 0 or cat is None or bkt is None:
            unreadable += 1
            continue
        flow[(bkt, cat)] = flow.get((bkt, cat), 0.0) + amt
    buckets = {}
    for (bkt, cat), amt in flow.items():
        buckets.setdefault(bkt, {})[cat] = amt
    out_buckets = []
    for bkt, cats in buckets.items():
        clist = sorted(({"category": c, "amount": round(a, 2)} for c, a in cats.items()),
                       key=lambda x: (-x["amount"], x["category"]))
        out_buckets.append({"bucket": bkt, "total": round(sum(cats.values()), 2), "categories": clist})
    out_buckets.sort(key=lambda b: (-b["total"], b["bucket"]))
    flow_total = round(sum(flow.values()), 2) if flow else None   # None: no readable rows

    ob_rows = _rows(section, "off_budget")
    unreadable += max(0, len(ob_rows) - MAX_OFF_BUDGET_ROWS)
    off = []
    for r in ob_rows[:MAX_OFF_BUDGET_ROWS]:
        if not isinstance(r, dict):
            unreadable += 1
            continue
        name, bal = _name(r.get("name")), _num(r.get("balance"))
        if name is None or bal is None:
            unreadable += 1
            continue
        tgt = _num(r.get("target"))
        pct = None
        if tgt is not None and tgt > 0:
            try:
                q = bal / tgt * 100
                pct = round(q) if math.isfinite(q) else None
            except (OverflowError, ZeroDivisionError):
                pct = None
        off.append({"name": name, "balance": round(bal, 2),
                    "target": round(tgt, 2) if tgt is not None else None, "pct": pct})
    view = _freshness(section, now, MONEY_STALE_AFTER_HOURS)
    view.update({"received_at": received_at, "period": _label(section.get("period")),
                 "buckets": out_buckets, "flow_total": flow_total, "off_budget": off,
                 "unreadable": unreadable})
    return view


def _hours(rows):
    """-> (dict category -> hours, unreadable)"""
    bad = max(0, len(rows) - MAX_HOURS_ROWS)
    out = {}
    for r in rows[:MAX_HOURS_ROWS]:
        if not isinstance(r, dict):
            bad += 1
            continue
        cat, h = _name(r.get("category")), _num(r.get("hours"))
        if cat is None or h is None or h < 0:
            bad += 1
            continue
        out[cat] = out.get(cat, 0.0) + h
    return out, bad


def _time(section, now, received_at):
    unreadable = 0
    sides = []
    for key in ("planned", "actual"):
        h, bad = _hours(_rows(section, key))
        unreadable += bad
        sides.append(h)
    planned, actual = sides
    all_cats = list(dict.fromkeys(list(planned) + list(actual)))
    unreadable += max(0, len(all_cats) - MAX_TIME_CATEGORIES)
    cats = all_cats[:MAX_TIME_CATEGORIES]
    # totals are summed over ALL readable categories, before the display cap
    ptot = round(sum(planned.values()), 1) if planned else None
    atot = round(sum(actual.values()), 1) if actual else None
    rows = []
    for c in cats:
        p = round(planned[c], 1) if c in planned else None
        a = round(actual[c], 1) if c in actual else None
        d = round(actual[c] - planned[c], 1) if c in planned and c in actual else None
        rows.append({"category": c, "planned": p, "actual": a, "delta": d})
    view = _freshness(section, now, TIME_STALE_AFTER_HOURS)
    view.update({"received_at": received_at, "week_of": _label(section.get("week_of")),
                 "rows": rows, "planned_total": ptot, "actual_total": atot,
                 "planned_gap": round(WEEK_HOURS - ptot, 1) if ptot is not None else None,
                 "actual_gap": round(WEEK_HOURS - atot, 1) if atot is not None else None, "unreadable": unreadable})
    return view


def build_finance_view(snapshots, now):
    """Snapshots (newest first) + aware UTC `now` -> the /finance view model."""
    money = time_ = None
    for snap in snapshots if isinstance(snapshots, list) else []:
        if not isinstance(snap, dict):
            continue
        ra = snap.get("received_at")
        ra = ra if isinstance(ra, str) else None
        if money is None and isinstance(snap.get("money"), dict):
            money = _money(snap["money"], now, ra)
        if time_ is None and isinstance(snap.get("time"), dict):
            time_ = _time(snap["time"], now, ra)
        if money is not None and time_ is not None:
            break
    return {"empty": money is None and time_ is None, "money": money, "time": time_}
