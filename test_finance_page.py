import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import finance_model as fm
import finance_page as fp

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
NONCE = "abc123"


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def money(as_of=None, flow=None, off=None, period="2026-10"):
    return {"as_of": as_of or iso(24), "period": period,
            "flow": flow if flow is not None else [
                {"category": "Groceries", "bucket": "Needs", "amount": 1412.5},
                {"category": "Rent", "bucket": "Needs", "amount": 1000},
                {"category": "Games", "bucket": "Wants", "amount": 60.25}],
            "off_budget": off if off is not None else [
                {"name": "Emergency fund", "balance": 5200, "target": 10000},
                {"name": "Card", "balance": -300.5}]}


def timesec(as_of=None):
    return {"as_of": as_of or iso(48), "week_of": "2026-11-02",
            "planned": [{"category": "Sleep", "hours": 56}, {"category": "Work", "hours": 108}],
            "actual": [{"category": "Sleep", "hours": 49.5}, {"category": "Work", "hours": 121.5}]}


def view_of(**sections):
    snap = {"received_at": iso(5)}
    snap.update(sections)
    return fm.build_finance_view([snap], NOW)


def page(view):
    return fp.render_finance_page(view, NOW, NONCE)


def embedded(html_text):
    m = re.search(r'<script type="application/json" id="fin-data"[^>]*>(.*?)</script>', html_text, re.S)
    return m.group(1)


def test_full_view_numbers():
    p = page(view_of(money=money(), time=timesec()))
    for s in ("Needs", "2,412.50", "Wants", "60.25", "Groceries", "1,412.50", "Emergency fund",
              "5,200.00", "Card", "-300.50", "Sleep", "56.0", "49.5", "-6.5", "Work", "108.0",
              "121.5", "+13.5", "Planned: 164 h, 4 h unplanned",
              "Actual: 171 h, 3 h over 168", "Total flow: 2,472.75", "52% of target", '<progress max="100" value="52">'):
        assert s in p, s
    assert "Latest upload received 5h ago" in p


def test_empty_state():
    p = page(fm.build_finance_view([], NOW))
    assert "Nothing uploaded yet" in p
    for s in ("<table", "fin-data", "Needs"):
        assert s not in p


def test_one_section_missing():
    p = page(view_of(money=money()))
    assert "No time data uploaded yet." in p and "Needs" in p
    p = page(view_of(time=timesec()))
    assert "No money data uploaded yet." in p and "Sleep" in p


def test_staleness_stamps():
    p = page(view_of(money=money(as_of=iso(24 * 5)), time=timesec(as_of=iso(24))))
    assert "Updated 5d ago" in p and "Updated 24h ago" in p
    assert p.count("STALE") == 2  # money + off-budget share one stamp
    assert p.index("STALE") < p.index('id="time"')
    fresh = page(view_of(money=money()))
    assert "STALE" not in fresh
    unk = page(view_of(money=money(as_of="garbage")))
    assert "Date unknown" in unk


def test_escaping():
    evil_cat = "<script>alert(1)</script>"
    evil_bucket = '"><img src=x>'
    evil_close = "</script>"
    v = view_of(money=money(flow=[{"category": evil_cat, "bucket": evil_bucket, "amount": 5},
                                  {"category": evil_close, "bucket": "B", "amount": 1}]))
    p = page(v)
    assert evil_cat not in p and "<img src=x>" not in p
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in p
    assert "&quot;&gt;&lt;img src=x&gt;" in p
    assert "&lt;/script&gt;" in p
    assert p.count("</script>") == 2   # only our two real closing tags
    data = json.loads(embedded(p))
    names = {b["bucket"] for b in data["money"]["buckets"]}
    cats = {c["category"] for b in data["money"]["buckets"] for c in b["categories"]}
    assert evil_bucket in names and {evil_cat, evil_close} <= cats


def test_embedded_json_safe():
    v = view_of(money=money(flow=[{"category": "a<b>&c" + chr(0x2028) + "d" + chr(0x2029) + "e", "bucket": "X", "amount": 1}]))
    raw = embedded(page(v))
    assert not any(ch in raw for ch in "<>&" + chr(0x2028) + chr(0x2029))
    assert "\\u2028" in raw
    assert json.loads(raw)["money"]["buckets"][0]["categories"][0]["category"] == "a<b>&c" + chr(0x2028) + "d" + chr(0x2029) + "e"


def test_structure():
    p = page(view_of(money=money(), time=timesec()))
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in p
    assert 'content="noindex,nofollow"' in p
    for tag in re.findall(r"<(?:script|style)\b[^>]*>", p):
        assert f'nonce="{NONCE}"' in tag
    for bad in ("http", "<link", "<img", "src=", "innerHTML", "transaction", "merchant"):
        assert bad not in p, bad
    scripts = re.findall(r"<script nonce=[^>]*>(.*?)</script>", p, re.S)
    assert len(scripts) == 1 and len(scripts[0].strip().splitlines()) < 120
    if shutil.which("node"):
        f = Path(__file__).with_name("_tmp_finance_script.js")
        try:
            f.write_text(scripts[0], encoding="utf-8")
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
        finally:
            f.unlink(missing_ok=True)


def test_negative_and_none():
    p = page(view_of(money=money(), time={"as_of": iso(1), "planned": [{"category": "Sleep", "hours": 8}],
                                          "actual": []}))
    assert "-300.50 owed" in p
    assert "Actual: -" in p
    assert fp._fmt(None) == "-" and fp._fmt(0) == "0.00"
    row = re.search(r"<tr><td>Sleep</td>(.*?)</tr>", p).group(1)
    assert row.count("<td class=\"n\">-</td>") == 2


def test_unreadable_line():
    bad = money(flow=[{"category": "A", "bucket": "B", "amount": 1}, "junk"])
    assert "1 rows could not be read" in page(view_of(money=bad))
    assert "could not be read" not in page(view_of(money=money()))


def test_source_has_no_heavy_imports():
    src = Path(fp.__file__).read_text(encoding="utf-8")
    for name in ("matplotlib", "plotly", "pandas", "PIL", "numpy", "sqlite3", "flask"):
        assert not re.search(rf"^\s*(import|from)\s+{name}\b", src, re.M), name


def test_robust_to_odd_views():
    for v in (None, [], "x", {}, {"money": [1], "time": "x"}, {"empty": False, "money": [], "time": None}):
        assert "Finance" in page(v)
    bad = {"empty": False, "time": {"rows": [{"category": "Sleep", "planned": 8}, "junk"], "as_of_known": True},
           "money": {"buckets": [{"bucket": "Needs", "categories": [{"category": "Rent"}, 5]}, "x"],
                     "off_budget": [{"name": "N", "balance": 1, "pct": float("nan")},
                                    {"name": "M", "balance": 1, "pct": "high"}]}}
    p = page(bad)
    assert "<progress" not in p and "Sleep" in p and "Needs" in p
    row = re.search(r"<tr><td>Sleep</td>(.*?)</tr>", p).group(1)
    assert row.count('<td class="n">-</td>') == 2 and "0.0" not in row
    assert "0.00" not in p


def test_missing_figure_draws_no_bar_in_script():
    p = page(view_of(time=timesec()))
    assert "typeof r.planned === 'number'" in p and "typeof r.actual === 'number'" in p


def test_off_budget_omitted_when_money_none():
    p = page(view_of(time=timesec()))
    assert "Off-budget" not in p and 'id="offbudget"' not in p


def test_bad_received_at_and_naive_now_do_not_raise():
    snap = {"received_at": "not a date", "money": money()}
    v = fm.build_finance_view([snap], NOW)
    assert "received time unknown" in page(v)
    snap = {"received_at": iso(3), "money": money()}
    v = fm.build_finance_view([snap], NOW)
    p = fp.render_finance_page(v, NOW.replace(tzinfo=None), NONCE)
    assert "Latest upload received 3h ago" in p
