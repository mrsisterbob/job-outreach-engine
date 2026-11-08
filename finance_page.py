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

_S_HEAD = """
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
"""

_S_DRAW = """  function maxOf(rows) {
    var m = 0;
    rows.forEach(function (r) { m = Math.max(m, r.planned || 0, r.actual || 0); });
    return m;
  }
  function drawHours(r, i, max) {
    var host = document.querySelector('[data-hours="' + i + '"]');
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);
    if (!(max > 0)) return;
    var svg = mk(28, r.category + ': planned and actual hours');
    if (typeof r.planned === 'number') rect(svg, 0, 0, r.planned / max * 100, 12, 'plan');
    if (typeof r.actual === 'number') rect(svg, 0, 15, r.actual / max * 100, 12, 'act');
    host.appendChild(svg);
  }
"""

_S_TAIL = """  if (time) {
    var max = maxOf(time.rows || []);
    (time.rows || []).forEach(function (r, i) { drawHours(r, i, max); });
  }
"""

_S_DRAW_MANUAL = """  function maxOf(rows) {
    var m = 0;
    rows.forEach(function (r) {
      m = Math.max(m, r.planned || 0, typeof r.combined === 'number' ? r.combined : (r.actual || 0));
    });
    return m;
  }
  var defsDone = false;
  function hatchDefs() {
    if (defsDone) return;
    defsDone = true;
    var svg = document.createElementNS(NS, 'svg'), defs = document.createElementNS(NS, 'defs');
    var p = document.createElementNS(NS, 'pattern'), l = document.createElementNS(NS, 'path');
    svg.setAttribute('width', 0); svg.setAttribute('height', 0); svg.setAttribute('aria-hidden', 'true');
    p.setAttribute('id', 'hatch'); p.setAttribute('patternUnits', 'userSpaceOnUse');
    p.setAttribute('width', 6); p.setAttribute('height', 6); p.setAttribute('patternTransform', 'rotate(45)');
    l.setAttribute('d', 'M0 0V6'); l.setAttribute('class', 'hatchp');
    p.appendChild(l); defs.appendChild(p); svg.appendChild(defs); document.body.appendChild(svg);
  }
  function drawHours(r, i, max) {
    var host = document.querySelector('[data-hours="' + i + '"]');
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);
    if (!(max > 0)) return;
    hatchDefs();
    var s = barSegments(typeof r.combined === 'number' ? r.combined : r.actual, r.manual, max);
    var svg = mk(28, r.category + ': planned, PC and by-hand hours');
    if (typeof r.planned === 'number') rect(svg, 0, 0, r.planned / max * 100, 12, 'plan');
    if (s) {
      rect(svg, 0, 15, s.pc, 12, 'act');
      if (s.manual > 0) rect(svg, s.pc, 15, s.manual, 12, 'man');
    }
    host.appendChild(svg);
  }
"""

_SEGS = """
function barSegments(combined, manual, max) {
  if (typeof combined !== 'number' || !(max > 0)) return null;
  var m = Math.min(Math.max(typeof manual === 'number' ? manual : 0, 0), combined);
  return {pc: (combined - m) / max * 100, manual: m / max * 100, total: combined / max * 100};
}
"""

_MANUAL_CSS = """
.hatchp{stroke:var(--b);stroke-width:2}.man{fill:url(#hatch);stroke:var(--b);stroke-width:1.5}
table.man6{min-width:34rem}table.man6 td.n,table.man6 th.n{white-space:nowrap}
"""

_SCRIPT_A = _S_HEAD + _S_DRAW + _S_TAIL
_SCRIPT = _SCRIPT_A + "})();\n"

_SUMQ = """
function snapQ(v) { var n = Number(v); return isFinite(n) ? Math.min(672, Math.max(0, Math.round(n * 4))) : 0; }
function sumQuarters(values) {
  var s = 0;
  for (var i = 0; i < values.length; i++) s += snapQ(values[i]);
  return s;
}
"""

_EDITOR = """
  var ed = data.edit, host = document.getElementById('plan-editor'), tpl = document.getElementById('plan-tpl');
  if (!(ed && time && host && tpl && tpl.content)) return;
  host.appendChild(tpl.content.cloneNode(true));
  var cats = time.editable_categories || [], rows = time.rows || [];
  var nums = host.querySelectorAll('input[type=number]'), rngs = host.querySelectorAll('input[type=range]');
  var totalEl = host.querySelector('[data-total]'), msgEl = host.querySelector('[data-msg]');
  var saveBtn = host.querySelector('[data-save]'), resetBtn = host.querySelector('[data-reset]');
  var base = time.plan_version, busy = false, stale = false, q = [], loaded;
  cats.forEach(function (c) {
    var v = 0;
    rows.forEach(function (r) { if (r.category === c && typeof r.planned === 'number') v = r.planned; });
    q.push(snapQ(v));
  });
  loaded = q.slice();
  function hv() { return q.map(function (x) { return x / 4; }); }
  function say(t) { msgEl.textContent = t; }
  function draw() {
    var shown = rows.map(function (r) {
      var k = cats.indexOf(r.category);
      return {category: r.category, actual: r.actual, planned: k >= 0 ? q[k] / 4 : r.planned};
    });
    var m = maxOf(shown);
    shown.forEach(function (r, i) { drawHours(r, i, m); });
  }
  function update() {
    var tq = sumQuarters(hv()), d = 672 - tq, t;
    if (d > 0) t = 'Total ' + (tq / 4) + ' h of 168 - ' + (d / 4) + ' h unassigned';
    else if (d < 0) t = 'Total ' + (tq / 4) + ' h of 168 - ' + (-d / 4) + ' h over';
    else t = 'Total 168 h - ready to save';
    totalEl.textContent = t;
    saveBtn.disabled = busy || stale || d !== 0;
    resetBtn.disabled = busy;
    cats.forEach(function (c, i) { nums[i].disabled = busy; rngs[i].disabled = busy; });
    draw();
  }
  function sync() {
    q.forEach(function (x, i) {
      nums[i].value = x / 4; rngs[i].value = x / 4; nums[i].removeAttribute('aria-invalid');
    });
  }
  cats.forEach(function (c, i) {
    nums[i].addEventListener('input', function () {
      var s = nums[i].value.trim(), bad = s === '' || isNaN(Number(s));
      q[i] = snapQ(bad ? 0 : s);
      rngs[i].value = q[i] / 4;
      if (bad) nums[i].setAttribute('aria-invalid', 'true'); else nums[i].removeAttribute('aria-invalid');
      say(bad ? 'An empty or non-numeric box counts as 0.' : '');
      update();
    });
    nums[i].addEventListener('change', function () { nums[i].value = q[i] / 4; });
    nums[i].addEventListener('keydown', function (e) { if (e.key === 'Enter') e.preventDefault(); });
    rngs[i].addEventListener('input', function () {
      q[i] = snapQ(rngs[i].value); nums[i].value = q[i] / 4;
      nums[i].removeAttribute('aria-invalid'); say(''); update();
    });
  });
  function done(t) { busy = false; say(t); update(); }
  var FAIL = 'Could not save. Try again.';
  saveBtn.addEventListener('click', function () {
    if (busy || sumQuarters(hv()) !== 672) return;
    busy = true; say('Saving...'); update();
    var sent = q.slice();
    fetch(ed.plan_url, {method: 'POST', credentials: 'omit', cache: 'no-store',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({exp: ed.exp, sig: ed.sig, base_version: base,
        hours: cats.map(function (c, i) { return {category: c, hours: sent[i] / 4}; })})
    }).then(function (r) {
      var body = r.json().then(function (j) { return j || {}; }, function () { return {}; });
      return body.then(function (j) {
        if (r.status === 200) {
          loaded = sent; q = sent.slice(); sync();
          if (typeof j.version === 'number') { base = j.version; done('Saved.'); }
          else { stale = true; done('Saved, but reload the page to continue editing.'); }
        } else if (r.status === 400) done(j.error || FAIL);
        else if (r.status === 409) done('This plan changed somewhere else. Reload the page to see it.');
        else if (r.status === 404) done('This link has expired. Send /financelink again - your edits are still on screen.');
        else done(FAIL);
      });
    }).catch(function () { done(FAIL); });
  });
  resetBtn.addEventListener('click', function () { q = loaded.slice(); sync(); say(''); update(); });
  window.addEventListener('beforeunload', function (e) {
    if (q.join() !== loaded.join()) { e.preventDefault(); e.returnValue = ''; }
  });
  sync(); update();
"""

_EDIT_CSS = """
.prow{display:grid;grid-template-columns:1fr 6rem;gap:4px 12px;align-items:center;margin:12px 0}
.prow label{grid-column:1/3;font-weight:600}
.prow input{font:inherit;font-size:16px;min-height:44px;width:100%}
.prow input[type=number]{grid-column:2;grid-row:2;padding:0 8px}
.prow input[type=range]{grid-column:1;grid-row:2}
.ptotal{font-weight:600}
#plan-editor button{font:inherit;font-size:16px;min-height:44px;min-width:44px;padding:0 16px;margin:4px 8px 4px 0}
input[aria-invalid=true]{outline:2px solid var(--bad)}
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


def _table(head, rows, cls=""):
    """head: list of (label, css); rows: list of lists of (text, css)."""
    h = "".join(_cell("th", t, c) for t, c in head)
    body = "".join("<tr>" + "".join(_cell("td", t, c) for t, c in r) + "</tr>" for r in rows)
    return f'<div class="wrap"><table{(' class="' + cls + '"') if cls else ""}><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


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


_FOOTNOTE = ("By hand = typed with /life log. It is added on top of the PC's hours, "
             "so time the PC also tracked counts twice.")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _has_manual(t):
    """Real hand-logged data only: a zero-hour total with no entries keeps the page as it was."""
    if not isinstance(t, dict) or not _num(t.get("manual_total")):
        return False
    ents = t.get("manual_entries")
    return t["manual_total"] > 0 or (isinstance(ents, list) and len(ents) > 0)


def _row_text(r):
    if not _num(r.get("manual")):
        return f"PC {_short(r.get('actual'))} h"
    if r.get("manual_only") or not _num(r.get("actual")):
        return f"by hand {_short(r.get('manual'))} h"
    return f"PC {_short(r.get('actual'))} h + by hand {_short(r.get('manual'))} h = {_short(r.get('combined'))} h"


def _legend(rows):
    cats = [f"{r.get('category', '-')} {_short(r.get('manual'))} h" for r in rows if _num(r.get("manual"))]
    txt = "Bars: top = Planned, bottom solid = PC hours, bottom hatched = hours typed by hand"
    if cats:
        txt += " (" + ", ".join(cats) + ")"
    return f'<p class="mut">{_e(txt)}</p>'


def _totals_line(t):
    man = _short(t.get("manual_total"))
    if not t.get("actual_available"):
        if t.get("as_of_known") or (t.get("unreadable") or 0) > 0:
            return f"PC hours could not be read. By hand: {man} h"
        return f"PC hours not uploaded yet. By hand: {man} h"
    gap = t.get("combined_gap")
    line = f"PC {_short(t.get('actual_total'))} h + by hand {man} h = {_short(t.get('combined_total'))} h"
    if not _num(gap):
        return line
    if gap > 0:
        return f"{line}, {_short(gap)} h unlogged"
    if gap < 0:
        return f"{line}, over 168 by {_short(-gap)} h"
    return f"{line}, exactly 168"


def _delta_cell(r, pc_ok):
    if not pc_ok or not _num(r.get("actual")) or r.get("manual_only"):
        return "-"
    return _hrs(r.get("delta"), True)


def _time_section(t, editor=""):
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
        man = _has_manual(t)
        if man:
            out.append(_legend(rows))
        for i, r in enumerate(rows):
            beside = f' <span class="mut">{_e(_row_text(r))}</span>' if man else ""
            out.append(f'<div><strong>{_e(r.get("category", "-"))}</strong>{beside}<div data-hours="{i}"></div></div>')
        if man:
            out.append(_table([("Category", ""), ("Planned", "n"), ("Actual", "n"), ("By hand", "n"), ("Total", "n"),
                               ("Delta", "n")],
                              [[(r.get("category", "-"), ""),
                                ("-" if r.get("manual_only") else _hrs(r.get("planned")), "n"),
                                ("-" if r.get("manual_only") else _hrs(r.get("actual")), "n"),
                                (_hrs(r.get("manual")), "n"), (_hrs(r.get("combined")), "n"),
                                (_delta_cell(r, t.get("actual_available")), "n")] for r in rows], "man6"))
            out.append(f'<p class="mut">{_e(_FOOTNOTE)}</p>')
            out.append('<p class="mut">Delta is Total minus Planned, shown only where the PC has hours for the row.</p>')
        else:
            out.append(_table([("Category", ""), ("Planned", "n"), ("Actual", "n"), ("Delta", "n")],
                              [[(r.get("category", "-"), ""), (_hrs(r.get("planned")), "n"), (_hrs(r.get("actual")), "n"),
                                (_hrs(r.get("delta"), True), "n")] for r in rows]))
    if t.get("week_of"):
        out.append(f'<p class="mut">{_e("Week of: " + str(t["week_of"]))}</p>')
    out.append(f'<p>{_e(_gap_line("Planned", t.get("planned_total"), t.get("planned_gap")))}</p>')
    if _has_manual(t):
        out.append(f'<p>{_e(_totals_line(t))}</p>')
        if t.get("actual_available"):
            out.append(f'<p>{_e(_gap_line("PC only", t.get("actual_total"), t.get("actual_gap")))}</p>')
    else:
        out.append(f'<p>{_e(_gap_line("Actual", t.get("actual_total"), t.get("actual_gap")))}</p>')
    if _num(t.get("manual_hidden_hours")) and t["manual_hidden_hours"] > 0:
        out.append(f'<p class="mut">{_e(_short(t["manual_hidden_hours"]) + " h logged this week is not shown yet - the PC has not pushed the new week.")}</p>')
    if (t.get("unreadable") or 0) > 0:
        out.append(f'<p class="mut">{_e(t["unreadable"])} rows could not be read</p>')
    out.append(editor)
    out.append("</section>")
    return "".join(out)


def _valid_edit(edit, view):
    """(edit_fields, time_section_copy) when an editor may render, else (None, None)."""
    t = view.get("time")
    if not isinstance(edit, dict) or not isinstance(t, dict):
        return None, None
    url, exp, sig = edit.get("plan_url"), edit.get("exp"), edit.get("sig")
    if not (isinstance(url, str) and url.startswith("/") and not url.startswith("//")
            and "\\" not in url and isinstance(sig, str) and sig
            and isinstance(exp, int) and not isinstance(exp, bool)):
        return None, None
    cats = t.get("editable_categories")
    cats = [c for c in cats if isinstance(c, str) and c] if isinstance(cats, list) else []
    if not cats:
        return None, None
    return {"plan_url": url, "exp": exp, "sig": sig}, dict(t, editable_categories=cats)


def _plan_source_line(t, now):
    src, ver = t.get("plan_source"), t.get("plan_version")
    if src == "snapshot":
        return "Plan taken from the last upload - save to keep your own"
    if src != "saved":
        return ""
    vtxt = f" (version {ver})" if isinstance(ver, int) and not isinstance(ver, bool) else ""
    dt = _parse_dt(t.get("plan_updated_at"))
    if dt is None:
        return f"Plan saved{vtxt}"
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        ago = _ago_hours(max(0.0, (now - dt).total_seconds() / 3600.0))
    except (TypeError, OverflowError):
        return f"Plan saved{vtxt}"
    return f"Plan saved {ago}{vtxt}"


def _editor_html(t, now):
    rows = []
    for i, c in enumerate(t["editable_categories"]):
        lab = _e(c)
        rows.append(f'<div class="prow"><label for="pn{i}">{lab}</label>'
                    f'<input id="pn{i}" type="number" step="0.25" min="0" max="168" inputmode="decimal" aria-label="{lab} hours">'
                    f'<input type="range" step="0.25" min="0" max="168" aria-label="{lab} slider"></div>')
    src = _plan_source_line(t, now)
    srcp = f'<p class="mut">{_e(src)}</p>' if src else ""
    return ('<div id="plan-editor"></div><template id="plan-tpl"><h3>Edit planned hours</h3>' + srcp
            + "".join(rows)
            + '<p class="ptotal" data-total aria-live="polite"></p><p class="mut" data-msg aria-live="polite"></p>'
              '<button type="button" data-save disabled>Save</button>'
              '<button type="button" data-reset>Reset</button></template>')


def _json_for_script(view):
    s = json.dumps(view, ensure_ascii=False, default=str)
    for ch, rep in _JSON_ESCAPES.items():
        s = s.replace(ch, rep)
    return s


def render_finance_page(view, now, nonce, edit=None):
    n = _e(nonce)
    if not isinstance(view, dict):
        view = {"empty": True}
    view = dict(view)
    view["money"], view["time"] = _sec(view.get("money")), _sec(view.get("time"))
    if view["money"] is None and view["time"] is None:
        view["empty"] = True
    edit_fields, edit_time = _valid_edit(edit, view)
    if edit_fields:
        view["time"], view["edit"] = edit_time, edit_fields
    man = _has_manual(view.get("time"))
    head = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><title>Finance</title>'
            f'<style nonce="{n}">{_CSS}{_EDIT_CSS if edit_fields else ""}{_MANUAL_CSS if man else ""}</style></head><body><main><h1>Finance</h1>')
    if view.get("empty"):
        body = ('<section class="empty"><h2>Nothing uploaded yet.</h2>'
                '<p>Snapshots are pushed from the PC, and this page fills in after the first one.</p>'
                '</section>')
        return head + body + "</main></body></html>"
    body = (f'<p class="mut">{_e(_received_ago(view, now))}</p>'
            + _money_section(view.get("money"))
            + _time_section(view.get("time"), _editor_html(view["time"], now) if edit_fields else ""))
    if man:
        sa = _S_HEAD + _S_DRAW_MANUAL + _S_TAIL
        ed = _EDITOR.replace("actual: r.actual, planned",
                             "actual: r.actual, combined: r.combined, manual: r.manual, planned")
        assert ed != _EDITOR
        script = _SEGS + ((_SUMQ + sa + ed + "})();\n") if edit_fields else sa + "})();\n")
    else:
        script = (_SUMQ + _SCRIPT_A + _EDITOR + "})();\n") if edit_fields else _SCRIPT
    tail = (f'<script type="application/json" id="fin-data" nonce="{n}">{_json_for_script(view)}</script>'
            f'<script nonce="{n}">{script}</script></main></body></html>')
    return head + body + tail
