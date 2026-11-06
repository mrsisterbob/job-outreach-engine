"""Pure HTML renderer for the /finance view model (finance_model.build_finance_view output).

No I/O, no env, no Flask. Tables carry every number in plain HTML; a small inline script draws
the bars in the browser from the embedded JSON. Nothing here recomputes a figure: totals, gaps,
pct and stale flags all come from the view.
"""
import html
import json
import math
from datetime import datetime, timezone

_JSON_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
                 " ": "\\u2028", " ": "\\u2029"}

_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#555;--card:#f4f4f6;--line:#d0d0d6;--a:#2a6fdb;--b:#d9822b;--bad:#b3261e;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--fg:#ececf0;--mut:#a0a0aa;--card:#1d1d21;--line:#38383f;--a:#6aa3ff;--b:#f0a352;--bad:#ff8a80;--warn:#ffcc66}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:40rem;margin:0 auto;padding:16px}
h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.25rem;margin:0 0 4px}h3{font-size:1.05rem;margin:16px 0 4px}
section{background:var(--card);border-radius:10px;padding:16px;margin:16px 0}
.mut{color:var(--mut)}.stamp{font-weight:600;margin:0 0 8px}.stale{color:var(--warn)}
.wrap{overflow-x:auto;max-width:100%}
table{border-collapse:collapse;width:100%;min-width:16rem}
th,td{text-align:left;padding:12px 6px;border-bottom:1px solid var(--line)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.neg{color:var(--bad);font-weight:600}
svg.bar{display:block;width:100%;margin:6px 0}
progress{width:100%;height:16px}
.s0{fill:var(--a)}.s1{fill:var(--b)}.s2{fill:#3a9d6e}.s3{fill:#a05bd0}.s4{fill:#c9577a}.s5{fill:#7a8a99}
.plan{fill:var(--a)}.act{fill:var(--b)}
.empty{text-align:center;padding:32px 16px}
"""

_SCRIPT = """
(function () {
  var NS = 'ht' + 'tp://www.w3.org/2000/svg';
  var data;
  try { data = JSON.parse(document.getElementById('fin-data').textContent); } catch (e) { return; }
  function rect(svg, x, y, w, h, cls) {
    var r = document.createElementNS(NS, 'rect');
    r.setAttribute('x', x + '%'); r.setAttribute('y', y); r.setAttribute('height', h);
    r.setAttribute('width', Math.max(w, 0) + '%'); r.setAttribute('class', cls);
    svg.appendChild(r);
  }
  function mk(h, text) {
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'bar'); svg.setAttribute('height', h);
    svg.setAttribute('role', 'img');
    var t = document.createElementNS(NS, 'title');
    t.textContent = text; svg.appendChild(t);
    return svg;
  }
  var money = data.money;
  if (money) {
    (money.buckets || []).forEach(function (b, i) {
      var host = document.querySelector('[data-bucket="' + i + '"]');
      if (!host || !(b.total > 0)) return;
      var svg = mk(20, b.bucket + ' categories, stacked'), x = 0;
      (b.categories || []).forEach(function (c, j) {
        if (typeof c.amount !== 'number') return;
        var w = c.amount / b.total * 100;
        rect(svg, x, 0, w, 20, 's' + (j % 6));
        x += w;
      });
      host.appendChild(svg);
    });
  }
  var time = data.time;
  if (time) {
    var max = 0;
    (time.rows || []).forEach(function (r) {
      max = Math.max(max, r.planned || 0, r.actual || 0);
    });
    (time.rows || []).forEach(function (r, i) {
      var host = document.querySelector('[data-hours="' + i + '"]');
      if (!host || !(max > 0)) return;
      var svg = mk(28, r.category + ': planned and actual hours');
      if (typeof r.planned === 'number') rect(svg, 0, 0, r.planned / max * 100, 12, 'plan');
      if (typeof r.actual === 'number') rect(svg, 0, 15, r.actual / max * 100, 12, 'act');
      host.appendChild(svg);
    });
  }
})();
"""


def _e(v):
    return html.escape(str(v), quote=True)


def _fmt(v, places=2, signed=False):
    """One number formatter: thousands separators, minus for negatives, '-' for None."""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return "-"
    s = f"{abs(v):,.{places}f}"
    nonzero = float(s.replace(",", "")) != 0
    if v < 0 and nonzero:
        return "-" + s
    return ("+" + s) if signed and nonzero else s


def _dicts(v):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _sec(v):
    return v if isinstance(v, dict) else None


def _short(v):
    """Hours without a forced decimal: 164 not 164.0, 1.5 stays 1.5."""
    s = _fmt(v, 1)
    return s[:-2] if s.endswith(".0") else s


def _money(v):
    return _fmt(v, 2)


def _hrs(v, signed=False):
    return _fmt(v, 1, signed)


def _parse_dt(s):
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ago_hours(hours):
    if hours is None:
        return "unknown"
    if hours < 1:
        return "under 1h ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


def _received_ago(view, now):
    stamps = [_parse_dt(s.get("received_at")) for s in (view.get("money"), view.get("time"))
              if isinstance(s, dict)]
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamps = [d for d in stamps if d is not None]
    if not stamps:
        return "Latest upload received time unknown"
    try:
        hrs = max(0.0, (now - max(stamps)).total_seconds() / 3600.0)
    except (TypeError, OverflowError):
        return "Latest upload received time unknown"
    return "Latest upload received " + _ago_hours(hrs)


def _stamp(sec):
    if not sec.get("as_of_known"):
        txt = "Date unknown - treat as stale"
    else:
        txt = f"Updated {_ago_hours(sec.get('age_hours'))} · as of {sec.get('as_of')}"
    if sec.get("stale"):
        txt += " · STALE"
    cls = "stamp stale" if sec.get("stale") else "stamp"
    return f'<p class="{cls}">{_e(txt)}</p>'


def _cell(tag, text, css):
    attr = f' class="{css}"' if css else ""
    return f"<{tag}{attr}>{_e(text)}</{tag}>"


def _table(head, rows):
    """head: list of (label, css); rows: list of lists of (text, css)."""
    h = "".join(_cell("th", t, c) for t, c in head)
    body = "".join("<tr>" + "".join(_cell("td", t, c) for t, c in r) + "</tr>" for r in rows)
    return f'<div class="wrap"><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def _money_section(m):
    out = ['<section id="money"><h2>Money flow</h2>']
    if m is None:
        out.append('<p class="mut">No money data uploaded yet.</p></section>')
        return "".join(out)
    out.append(_stamp(m))
    if not _dicts(m.get("buckets")):
        out.append('<p class="mut">No readable spending rows.</p>')
    for i, b in enumerate(m.get("buckets") if isinstance(m.get("buckets"), list) else []):
        if not isinstance(b, dict):
            continue
        out.append(f'<h3>{_e(b.get("bucket", "-"))} · {_e(_money(b.get("total")))}</h3>'
                   f'<div data-bucket="{i}"></div>')
        out.append(_table([("Category", ""), ("Amount", "n")],
                          [[(c.get("category", "-"), ""), (_money(c.get("amount")), "n")]
                           for c in _dicts(b.get("categories"))]))
    foot = []
    if m.get("period"):
        foot.append(f"Period: {m['period']}")
    foot.append(f"Total flow: {_money(m.get('flow_total'))}")
    out.append('<p class="mut">' + _e(" · ".join(foot)) + "</p>")
    if (m.get("unreadable") or 0) > 0:
        out.append(f'<p class="mut">{_e(m["unreadable"])} rows could not be read</p>')
    out.append("</section>")

    out.append('<section id="offbudget"><h2>Off-budget balances</h2>')
    out.append(_stamp(m))
    ob = _dicts(m.get("off_budget"))
    if not ob:
        out.append('<p class="mut">No balances in this upload.</p>')
    for r in ob:
        bal_v = r.get("balance")
        neg = isinstance(bal_v, (int, float)) and bal_v < 0
        bal = _money(bal_v) + (" owed" if neg else "")
        tgt = (f' <span class="mut">of {_e(_money(r["target"]))} target</span>'
               if r.get("target") is not None else "")
        out.append(f'<h3>{_e(r.get("name", "-"))}</h3><p class="{"neg" if neg else ""}">{_e(bal)}{tgt}</p>')
        pct = r.get("pct")
        if pct is not None:
            if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
                out.append('<p class="mut">-</p>')
            else:
                pv = _fmt(min(100, max(0, pct)), 0)
                out.append(f'<progress max="100" value="{pv}"></progress>'
                           f'<p class="mut">{_e(_fmt(pct, 0))}% of target</p>')
    out.append("</section>")
    return "".join(out)


def _gap_line(kind, total, gap):
    if total is None or gap is None:
        return f"{kind}: -"
    if gap > 0:
        tail = f"{_short(gap)} h " + ("unplanned" if kind == "Planned" else "under 168")
    elif gap < 0:
        tail = f"{_short(-gap)} h over 168"
    else:
        tail = "exactly 168"
    return f"{kind}: {_short(total)} h, {tail}"


def _time_section(t):
    out = ['<section id="time"><h2>168 hours</h2>']
    if t is None:
        out.append('<p class="mut">No time data uploaded yet.</p></section>')
        return "".join(out)
    out.append(_stamp(t))
    rows = t.get("rows") if isinstance(t.get("rows"), list) else []
    if not rows:
        out.append('<p class="mut">No readable hours rows.</p>')
    else:
        out.append("<h3>Planned vs actual</h3>")
        rows = [r if isinstance(r, dict) else {} for r in rows]
        for i, r in enumerate(rows):
            out.append(f'<div><strong>{_e(r.get("category", "-"))}</strong><div data-hours="{i}"></div></div>')
        out.append(_table([("Category", ""), ("Planned", "n"), ("Actual", "n"), ("Delta", "n")],
                          [[(r.get("category", "-"), ""), (_hrs(r.get("planned")), "n"), (_hrs(r.get("actual")), "n"),
                            (_hrs(r.get("delta"), True), "n")] for r in rows]))
    if t.get("week_of"):
        out.append(f'<p class="mut">{_e("Week of: " + str(t["week_of"]))}</p>')
    out.append(f'<p>{_e(_gap_line("Planned", t.get("planned_total"), t.get("planned_gap")))}</p>')
    out.append(f'<p>{_e(_gap_line("Actual", t.get("actual_total"), t.get("actual_gap")))}</p>')
    if (t.get("unreadable") or 0) > 0:
        out.append(f'<p class="mut">{_e(t["unreadable"])} rows could not be read</p>')
    out.append("</section>")
    return "".join(out)


def _json_for_script(view):
    s = json.dumps(view, ensure_ascii=False, default=str)
    for ch, rep in _JSON_ESCAPES.items():
        s = s.replace(ch, rep)
    return s


def render_finance_page(view, now, nonce):
    n = _e(nonce)
    if not isinstance(view, dict):
        view = {"empty": True}
    view = dict(view)
    view["money"], view["time"] = _sec(view.get("money")), _sec(view.get("time"))
    if view["money"] is None and view["time"] is None:
        view["empty"] = True
    head = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><title>Finance</title>'
            f'<style nonce="{n}">{_CSS}</style></head><body><main><h1>Finance</h1>')
    if view.get("empty"):
        body = ('<section class="empty"><h2>Nothing uploaded yet.</h2>'
                '<p>Snapshots are pushed from the PC, and this page fills in after the first one.</p>'
                '</section>')
        return head + body + "</main></body></html>"
    body = (f'<p class="mut">{_e(_received_ago(view, now))}</p>'
            + _money_section(view.get("money")) + _time_section(view.get("time")))
    tail = (f'<script type="application/json" id="fin-data" nonce="{n}">{_json_for_script(view)}</script>'
            f'<script nonce="{n}">{_SCRIPT}</script></main></body></html>')
    return head + body + tail
