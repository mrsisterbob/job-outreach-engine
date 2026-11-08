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


# ---- TASK-280: plan editor ----
import hours_plan as hp

EDIT = {"plan_url": "/finance/plan", "exp": 1788123457, "sig": "SIGVALUEzz9Qk"}


def plan_view(tmp_path, monkeypatch, cats=("Sleep", "Work", "Gym"), hours=(56, 100, 12)):
    monkeypatch.setenv("PERSONAL_PLAN_DIR", str(tmp_path))
    hp.save_plan([{"category": c, "hours": h} for c, h in zip(cats, hours)], list(cats), None, now=NOW)
    snap = {"received_at": iso(5), "time": {"as_of": iso(2), "actual": [{"category": c, "hours": 10} for c in cats]}}
    return fm.build_finance_view([snap], NOW, plan=hp.load_plan())


def epage(view, edit=EDIT):
    return fp.render_finance_page(view, NOW, NONCE, edit)


def script_of(p):
    return re.findall(r"<script nonce=[^>]*>(.*?)</script>", p, re.S)[0]


def test_plan_edit_embeds_credential_and_container(tmp_path, monkeypatch):
    p = epage(plan_view(tmp_path, monkeypatch))
    d = json.loads(embedded(p))
    assert d["edit"] == EDIT
    assert d["time"]["editable_categories"] == ["Sleep", "Work", "Gym"]
    assert 'id="plan-editor"' in p and "data-save" in p and ">Save<" in p and ">Reset<" in p
    assert "Plan saved" in p and "(version 1)" in p


def test_plan_edit_none_equals_read_only(tmp_path, monkeypatch):
    v = plan_view(tmp_path, monkeypatch)
    p = epage(v, None)
    assert p == fp.render_finance_page(v, NOW, NONCE)
    for bad in ('type="range"', "Save", "plan_url", "plan-editor"):
        assert bad not in p


def test_plan_edit_bad_inputs_render_read_only(tmp_path, monkeypatch):
    v = plan_view(tmp_path, monkeypatch)
    plain = fp.render_finance_page(v, NOW, NONCE)
    no_time = view_of(money=money())
    empty_cats = json.loads(json.dumps(v))
    empty_cats["time"]["editable_categories"] = []
    for view, edit in ((no_time, EDIT), (empty_cats, EDIT), (v, {"plan_url": "/x"}), (v, "x"), (v, []),
                       (v, dict(EDIT, exp="soon")), (v, dict(EDIT, exp=True)), (v, dict(EDIT, sig="")),
                       (v, dict(EDIT, plan_url=None))):
        p = epage(view, edit)
        assert "plan-editor" not in p and 'type="range"' not in p and "plan_url" not in p
    assert epage(v, {"plan_url": "/x"}) == plain


def test_plan_edit_credential_appears_once_only_in_blob(tmp_path, monkeypatch):
    p = epage(plan_view(tmp_path, monkeypatch))
    assert p.count(EDIT["sig"]) == 1 and p.count(str(EDIT["exp"])) == 1
    assert EDIT["sig"] in embedded(p) and str(EDIT["exp"]) in embedded(p)
    assert p.count("financelink") == 1 and "financelink" in script_of(p)
    assert "<form" not in p and 'type="hidden"' not in p and "href=" not in p


def test_plan_edit_escaping(tmp_path, monkeypatch):
    evil = "</script><b>" + chr(0x2028) + "x"
    cats = (evil, "Work")
    v = plan_view(tmp_path, monkeypatch, cats=cats, hours=(60, 108))
    p = epage(v)
    assert evil not in p and "&lt;/script&gt;&lt;b&gt;" in p
    assert p.count("</script>") == 2  # only our two real closing tags
    raw = embedded(p)
    assert chr(0x2028) not in raw and r"\u2028" in raw
    assert json.loads(raw)["time"]["editable_categories"][0] == evil


def test_plan_edit_structure(tmp_path, monkeypatch):
    p = epage(plan_view(tmp_path, monkeypatch))
    for tag in re.findall(r"<(?:script|style)\b[^>]*>", p):
        assert f'nonce="{NONCE}"' in tag
    for bad in ("http", "<link", "<img", "src=", "innerHTML", "eval", "new Function", "document.write"):
        assert bad not in p, bad
    assert len(script_of(p).strip().splitlines()) < 160
    assert "function sumQuarters(values)" in script_of(p)


def _node_run(code):
    f = Path(__file__).with_name("_tmp_plan_edit.js")
    try:
        f.write_text(code, encoding="utf-8")
        return subprocess.run(["node", str(f)], capture_output=True, text=True)
    finally:
        f.unlink(missing_ok=True)


def test_plan_edit_sum_quarters_in_node(tmp_path, monkeypatch):
    if not shutil.which("node"):
        import pytest
        pytest.skip("node not installed")
    s = script_of(epage(plan_view(tmp_path, monkeypatch)))
    harness = (s + "\n"
               "var out = [sumQuarters([168]), sumQuarters([167.75]), sumQuarters([168.25]),\n"
               "  sumQuarters(new Array(30).fill(5.6)), sumQuarters(['', 'x', null, -3, 0.1, 0.13])];\n"
               "console.log(JSON.stringify(out));\n")
    harness = "var document = {getElementById: function () { return null; }};\n" + harness
    r = _node_run(harness)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [672, 671, 673, 660, 1]


def test_plan_edit_markup_counts(tmp_path, monkeypatch):
    p = epage(plan_view(tmp_path, monkeypatch))
    nums = re.findall(r'<input[^>]*type="number"[^>]*>', p)
    rngs = re.findall(r'<input[^>]*type="range"[^>]*>', p)
    assert len(nums) == 3 and len(rngs) == 3
    for tag in nums + rngs:
        assert 'step="0.25"' in tag and 'max="168"' in tag and 'min="0"' in tag
    assert 'aria-live="polite"' in p


def test_plan_edit_snapshot_source_line(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_PLAN_DIR", str(tmp_path))
    v = view_of(time=timesec())
    p = epage(v)
    assert "Plan taken from the last upload - save to keep your own" in p
    assert "Plan saved" not in p


def test_plan_edit_rejects_non_same_origin_url(tmp_path, monkeypatch):
    v = plan_view(tmp_path, monkeypatch)
    for bad in ("https://evil.example/finance/plan", "//evil.example/p", "finance/plan", "/\evil.example"):
        p = epage(v, dict(EDIT, plan_url=bad))
        assert "plan-editor" not in p and "evil.example" not in p and EDIT["sig"] not in p


def test_plan_edit_input_order_follows_editable_categories(tmp_path, monkeypatch):
    cats = ("Zeta", "Alpha", "Mid", "Beta")
    p = epage(plan_view(tmp_path, monkeypatch, cats=cats, hours=(40, 40, 44, 44)))
    labels = re.findall(r'<label for="pn\d+">(.*?)</label>', p)
    assert labels == list(cats)
    rows = re.findall(r'<div class="prow">(.*?)</div>', p)
    assert len(rows) == 4
    for i, (c, r) in enumerate(zip(cats, rows)):
        assert f'id="pn{i}"' in r and f'aria-label="{c} hours"' in r and f'aria-label="{c} slider"' in r
        assert r.index('type="number"') < r.index('type="range"')
    assert json.loads(embedded(p))["time"]["editable_categories"] == list(cats)


def test_plan_edit_script_guards_in_flight_and_bad_version(tmp_path, monkeypatch):
    s = script_of(epage(plan_view(tmp_path, monkeypatch)))
    assert "nums[i].disabled = busy" in s and "rngs[i].disabled = busy" in s and "resetBtn.disabled = busy" in s
    assert "typeof j.version === 'number'" in s and "Saved, but reload the page" in s
    assert s.index("if (!host) return;") < s.index("if (!(max > 0)) return;")
    assert s.index("removeChild(host.firstChild)") < s.index("if (!(max > 0)) return;")


# ---- hand-logged hours (TASK-286) ----
import manual_hours as mh


def manual_view(tmp_path, monkeypatch, texts=("Sleep 1h", "side work 2h"), time_=None, now=NOW):
    monkeypatch.setenv("PERSONAL_LOG_DIR", str(tmp_path))
    for t in texts:
        mh.log_entry(t, ["Sleep", "Work"], now=now)
    snap = {"received_at": iso(5)}
    if time_ is not False:
        snap["time"] = time_ if time_ is not None else timesec()
    return fm.build_finance_view([snap], NOW, manual=mh.list_all())


def test_manual_log_columns_and_footnote(tmp_path, monkeypatch):
    p = page(manual_view(tmp_path, monkeypatch))
    for s in ("By hand", "Total", "side work", "50.5", ">2.0<",
              "By hand = typed with /life log. It is added on top of the PC&#x27;s hours, "
              "so time the PC also tracked counts twice.",
              "PC 171 h + by hand 3 h = 174 h, over 168 by 6 h"):
        assert s in p, s


def test_manual_log_absent_changes_nothing(tmp_path, monkeypatch):
    v = manual_view(tmp_path, monkeypatch, texts=())
    assert v["time"]["manual_total"] is None
    stripped = {**v, "time": {k: x for k, x in v["time"].items()
                              if k not in ("manual_total", "combined_total", "combined_gap", "manual_hidden_hours",
                                           "manual_week", "manual_entries", "manual_latest_at")}}
    stripped["time"]["rows"] = [{k: x for k, x in r.items() if k not in ("manual", "combined", "manual_only")}
                                for r in v["time"]["rows"]]
    a, b = page(v), page(stripped)
    assert "By hand" not in a and "hatch" not in a and "barSegments" not in a
    assert re.sub(r'<script type="application/json".*?</script>', "", a, flags=re.S) == \
        re.sub(r'<script type="application/json".*?</script>', "", b, flags=re.S)


def test_manual_log_hostile_category_escaped(tmp_path, monkeypatch):
    v = manual_view(tmp_path, monkeypatch, texts=())
    v["time"]["rows"].append({"category": "</script><b>x</b>", "planned": None, "actual": None, "delta": None,
                              "manual": 2.0, "combined": 2.0, "manual_only": True})
    v["time"]["manual_total"] = 2.0
    p = page(v)
    assert "&lt;/script&gt;&lt;b&gt;x&lt;/b&gt;" in p
    assert "<b>x</b>" not in p
    raw = embedded(p)
    assert "<" not in raw and ">" not in raw
    assert json.loads(raw)["time"]["rows"][-1]["category"] == "</script><b>x</b>"
    assert p.count("&lt;/script&gt;&lt;b&gt;x&lt;/b&gt;") >= 3  # row label, legend, table


def test_manual_log_manual_only_has_no_editor_row(tmp_path, monkeypatch):
    v = manual_view(tmp_path, monkeypatch)
    assert v["time"]["editable_categories"] == ["Sleep", "Work"]
    p = epage(v)
    assert p.count('type="number"') == 2 and p.count('type="range"') == 2
    assert 'aria-label="side work' not in p
    assert "Edit planned hours" in p


def test_manual_log_structure(tmp_path, monkeypatch):
    for p in (page(manual_view(tmp_path, monkeypatch)), epage(manual_view(tmp_path, monkeypatch))):
        for tag in re.findall(r"<(?:script|style)\b[^>]*>", p):
            assert f'nonce="{NONCE}"' in tag
        for bad in ("http", "<link", "<img", "src=", "innerHTML", "eval", "new Function", "document.write"):
            assert bad not in p, bad
        s = script_of(p)
        assert "'pattern'" in s and "barSegments" in s and "url(#hatch)" in p and "svg.lastChild" not in s
        assert s.count("createElementNS(NS, 'pattern')") == 1
        assert len(s.strip().splitlines()) < 180
        assert len(re.findall(r"<script nonce=", p)) == 1


def test_manual_log_pc_not_uploaded_and_hidden(tmp_path, monkeypatch):
    v = manual_view(tmp_path, monkeypatch, time_=False)
    p = page(v)
    assert "PC hours not uploaded yet. By hand: 3 h" in p
    assert " 0 h" not in p and ">0 h" not in p
    monkeypatch.setenv("PERSONAL_LOG_DIR", str(tmp_path / "other"))
    mh.log_entry("Sleep 1h", ["Sleep"], now=NOW)
    old = dict(timesec(), week_of="2026-10-26")
    v2 = fm.build_finance_view([{"received_at": iso(5), "time": old}], NOW, manual=mh.list_all())
    assert v2["time"]["manual_hidden_hours"] > 0
    assert "h logged this week is not shown yet - the PC has not pushed the new week." in page(v2)


def test_manual_log_bar_segments_in_node(tmp_path, monkeypatch):
    if not shutil.which("node"):
        import pytest
        pytest.skip("node not installed")
    s = script_of(page(manual_view(tmp_path, monkeypatch)))
    harness = ("var document = {getElementById: function () { return null; }};\n" + s + "\n"
               "var a = barSegments(10, 4, 20), b = barSegments(3, 5, 10), c = barSegments('x', 1, 10);\n"
               "console.log(JSON.stringify([a.pc + a.manual === a.total, a.total, a.manual,"
               " b.pc + b.manual === b.total, b.manual, c, barSegments(5, 1, 0)]));\n")
    r = _node_run(harness)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [True, 50, 20, True, 30, None, None]


def test_manual_log_delta_dash_without_pc(tmp_path, monkeypatch):
    p = page(manual_view(tmp_path, monkeypatch))
    cells = re.findall(r"<tr>(.*?)</tr>", p, re.S)
    side = [c for c in cells if "side work" in c][0]
    assert re.findall(r"<td[^>]*>(.*?)</td>", side) == ["side work", "-", "-", "2.0", "2.0", "-"]
    p2 = page(manual_view(tmp_path, monkeypatch, time_=False))
    assert "+" not in "".join(re.findall(r"<td[^>]*>(.*?)</td>", p2))
    assert not re.search(r"<td[^>]*>-\d", p2)


def test_manual_log_layout_legend_and_gap_labels(tmp_path, monkeypatch):
    p = page(manual_view(tmp_path, monkeypatch))
    assert '<table class="man6">' in p and "table.man6{min-width:34rem}" in p and "white-space:nowrap" in p
    for s in ("top = Planned", "bottom solid = PC hours", "bottom hatched = hours typed by hand", "side work 2 h",
              "PC only: 171 h, 3 h over 168", "by hand 2 h"):
        assert s in p, s
    assert "PC - h" not in p


def test_manual_log_wording_unreadable_vs_absent(tmp_path, monkeypatch):
    assert "PC hours not uploaded yet" in page(manual_view(tmp_path, monkeypatch, time_=False))
    bad = {"as_of": iso(2), "week_of": "2026-11-02", "actual": [{"category": "x", "hours": "zz"}]}
    p = page(manual_view(tmp_path, monkeypatch, time_=bad))
    assert "PC hours could not be read" in p and "not uploaded yet" not in p


def test_manual_log_zero_manual_keeps_page(tmp_path, monkeypatch):
    v = manual_view(tmp_path, monkeypatch, texts=())
    v["time"]["manual_total"] = 0.0
    p = page(v)
    assert "By hand" not in p and "man6" not in p and "barSegments" not in p
