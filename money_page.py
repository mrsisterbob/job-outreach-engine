"""Pure rendering for the /money page. No I/O, no Flask, no clock (callers pass `now`)."""
import html
from datetime import datetime, timezone

MAX_DEPTH = 6
MAX_ROWS = 200
STALE_HOURS = 36


def _parse(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_text(received_at, now):
    """(text, stale). Unparseable -> ('unknown', False)."""
    dt = _parse(received_at)
    if dt is None or now is None:
        return "unknown", False
    try:
        minutes = int((now - dt).total_seconds() // 60)
    except Exception:
        return "unknown", False
    if minutes < -5:
        return "unknown (clock ahead)", True
    minutes = max(minutes, 0)
    return f"received {minutes} minutes ago", minutes > STALE_HOURS * 60


def _esc(v):
    return html.escape(str(v))


def _rows(node, depth, out, budget):
    """Append (indent, key, value-or-None) rows to out; budget is a one-item list."""
    if isinstance(node, dict):
        items = [(k, v) for k, v in node.items()]
    else:
        items = [(f"#{i + 1}", v) for i, v in enumerate(node)]
    for k, v in items:
        if budget[0] <= 0:
            budget[1] = True
            return
        if isinstance(v, (dict, list)) and v:
            scalars = isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v)
            if scalars:
                budget[0] -= 1
                out.append((depth, k, ", ".join(str(x) for x in v)))
            elif depth + 1 > MAX_DEPTH:
                budget[0] -= 1
                out.append((depth, k, "(too deep)"))
            else:
                budget[0] -= 1
                out.append((depth, k, None))
                _rows(v, depth + 1, out, budget)
        else:
            budget[0] -= 1
            out.append((depth, k, "" if v in ({}, []) else ("-" if v is None else v)))


def render_money_page(snapshot, now) -> str:
    """Body HTML for the latest snapshot. Every key and value is escaped."""
    if not isinstance(snapshot, dict):
        return "<h2>Money</h2><p>No snapshot received yet.</p>"
    age, stale = age_text(snapshot.get("received_at"), now)
    parts = ["<h2>Money</h2>", f"<p class='meta'>{_esc(age)}"]
    if stale:
        parts.append(f" - STALE: older than {STALE_HOURS} hours")
    parts.append("</p><table>")
    out, budget = [], [MAX_ROWS, False]
    _rows(snapshot, 0, out, budget)
    for depth, k, v in out:
        pad = f"style='padding-left:{depth * 18}px'"
        if v is None:
            parts.append(f"<tr><td {pad}><b>{_esc(k)}</b></td><td></td></tr>")
        else:
            parts.append(f"<tr><td {pad}>{_esc(k)}</td><td>{_esc(v)}</td></tr>")
    parts.append("</table>")
    if budget[1]:
        parts.append("<p class='meta'>truncated: more rows not shown.</p>")
    return "".join(parts)
