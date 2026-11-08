"""Pure renderer for the short /life Telegram reply: finance view model in, Telegram-HTML out.

No I/O, no clock (`now` is an argument), no imports beyond the stdlib. The view is the output of
finance_model.build_finance_view, passed in; this module never imports it.

Decided: when the whole view is empty or malformed the reply is only the header plus NO_DATA_TEXT
and carries NO link line. A signed link to an empty page is noise and leaks that the page exists.

Ages come from each section's own `age_hours` (never recomputed from received_at). Every value that
came from a snapshot is html.escape'd and truncated for display so the reply stays under 900 chars.
Never raises: anything malformed renders the no-data text.
"""
import html
import math

NO_DATA_TEXT = "No data yet - nothing has been uploaded from the PC."
_HEADER = "<b>Life snapshot</b>"
_NAME_SHOW = 28
_MAX_BUCKETS = 3
_MAX_OFF = 3


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _esc(v, cap=_NAME_SHOW):
    s = v if isinstance(v, str) else ""
    s = s.strip()
    if len(s) > cap:
        s = s[:cap - 1] + "..."
    e = html.escape(s, quote=False)
    while len(e) > cap + 12 and s:      # entities (&amp;) can expand a name; keep the reply short
        s = s[:-1]
        e = html.escape(s, quote=False)
    return e


def _money(v):
    if not _is_num(v):
        return "unknown"
    r = round(float(v), 2)
    s = f"${abs(r):,.2f}"
    return "-" + s if r < 0 else s


def _hours(v):
    if not _is_num(v):
        return "unknown"
    r = round(float(v), 1)
    s = f"{r:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return s + " h"


def _signed_hours(v):
    h = _hours(v)
    return ("+" + h) if v > 0 else h


def _age_line(sec):
    """None for a plan-only time section (model says: no stamp, never stale)."""
    if sec.get("as_of_known") is True and _is_num(sec.get("age_hours")):
        age = max(0.0, float(sec["age_hours"]))      # negative age = clock skew
        ago = f"{int(age // 24)} d ago" if age >= 48 else f"{int(age)} h ago"
        line = f"As of {_esc(sec.get('as_of'), 40) or 'unknown'} ({ago})"
        return line + (" STALE" if sec.get("stale") is True else "")
    if (sec.get("as_of_known") is False and sec.get("stale") is False
            and sec.get("as_of") is None and sec.get("age_hours") is None):
        return None
    return "Date unknown - treat as stale"      # says stale once; no extra STALE mark


def _skipped(sec):
    n = sec.get("unreadable")
    if _is_num(n) and n > 0:
        return f"{int(n)} rows skipped"
    return None


def _money_lines(m):
    period = _esc(m.get("period"))
    head = f"Money ({period}): " if period else "Money: "
    total = m.get("flow_total")
    head += f"{_money(total)} spent" if _is_num(total) else "total unknown"
    lines = [head]
    buckets = [b for b in (m.get("buckets") if isinstance(m.get("buckets"), list) else [])
               if isinstance(b, dict)]
    if buckets:
        parts = [f"{_esc(b.get('bucket'))} {_money(b.get('total'))}" for b in buckets[:_MAX_BUCKETS]]
        lines.append("Top: " + ", ".join(parts))
    off = [r for r in (m.get("off_budget") if isinstance(m.get("off_budget"), list) else [])
           if isinstance(r, dict)]
    if off:
        parts = []
        for r in off[:_MAX_OFF]:
            p = f"{_esc(r.get('name'))} {_money(r.get('balance'))}"
            if _is_num(r.get("pct")):
                p += f" ({max(0, min(100, int(round(r['pct']))))}%)"
            parts.append(p)
        line = "Off-budget: " + ", ".join(parts)
        if len(off) > _MAX_OFF:
            line += f" +{len(off) - _MAX_OFF} more"
        lines.append(line)
    age = _age_line(m)
    if age:
        lines.append(age)
    sk = _skipped(m)
    if sk:
        lines.append(sk)
    return lines


def _time_lines(t):
    week = _esc(t.get("week_of"))
    head = f"Time (week of {week}): " if week else "Time: "
    head += f"planned {_hours(t.get('planned_total'))}"
    avail = t.get("actual_available") is True
    mtot = t.get("manual_total")
    by_hand = _is_num(mtot) and mtot > 0       # hand-logged hours present (TASK-287)
    use_comb = by_hand and _is_num(t.get("combined_total")) and avail
    if avail:
        head += f", actual {_hours(t.get('combined_total') if use_comb else t.get('actual_total'))}"
        if by_hand:
            head += f" ({_hours(mtot)} by hand)"
    if t.get("plan_source") == "saved":
        head += " (your saved plan)"
    lines = [head]
    if not avail:
        lines.append(f"PC hours not uploaded yet; {_hours(mtot)} logged by hand" if by_hand
                     else "Actual hours not uploaded yet")
    else:
        gap = t.get("combined_gap") if use_comb else t.get("actual_gap")
        if _is_num(gap):
            if gap < 0:
                lines.append(f"Over 168 by {_hours(-gap)}")
            else:
                lines.append(f"Unlogged: {_hours(gap)}")
    hid = t.get("manual_hidden_hours")
    if by_hand and _is_num(hid) and hid > 0:
        lines.append(f"{_hours(hid)} logged this week will show once the PC pushes the new week.")
    rows = [r for r in (t.get("rows") if isinstance(t.get("rows"), list) else [])
            if isinstance(r, dict) and r.get("manual_only") is not True
            and _is_num(r.get("delta")) and r["delta"] != 0]
    if rows:
        best = max(rows, key=lambda r: abs(r["delta"]))
        lines.append(f"Biggest gap: {_esc(best.get('category'))} "
                     f"{_signed_hours(best['delta'])} vs plan")
    age = _age_line(t)
    if age:
        lines.append(age)
    sk = _skipped(t)
    if sk:
        lines.append(sk)
    return lines


def render_life_reply(view, now, link):
    """view: build_finance_view output or None. link: full URL or None. Returns Telegram-HTML."""
    empty = _HEADER + "\n" + NO_DATA_TEXT
    try:
        if not isinstance(view, dict) or view.get("empty") is True:
            return empty
        money, time_ = view.get("money"), view.get("time")
        money = money if isinstance(money, dict) else None
        time_ = time_ if isinstance(time_, dict) else None
        if money is None and time_ is None:
            return empty
        lines = [_HEADER]
        lines += _money_lines(money) if money is not None else ["Money: no data yet."]
        lines += _time_lines(time_) if time_ is not None else ["Time: no data yet."]
        if isinstance(link, str) and link.strip():
            lines += ["Open the page: " + html.escape(link.strip(), quote=False), "Expires in 15 minutes."]
        else:
            lines.append("Link not configured (PAGE_LINK_SECRET).")
        return "\n".join(lines)
    except Exception:
        return empty
