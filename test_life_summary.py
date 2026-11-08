import re
from datetime import datetime, timedelta, timezone

import pytest

import life_summary as ls
from finance_model import build_finance_view

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
LINK = "https://x.example/finance?t=abc"


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def money(as_of=None, flow=None, off=None):
    return {"as_of": as_of or iso(5), "period": "2026-10",
            "flow": flow if flow is not None else [
                {"category": "Rent", "bucket": "Needs", "amount": 1800},
                {"category": "Fun", "bucket": "Wants", "amount": 900},
                {"category": "Save", "bucket": "Savings", "amount": 712.5},
                {"category": "Misc", "bucket": "Other", "amount": 1}],
            "off_budget": off if off is not None else [
                {"name": "Emergency fund", "balance": 5200, "target": 10000}]}


def timesec(as_of=None, planned=None, actual=None):
    return {"as_of": as_of or iso(5), "week_of": "2026-11-02",
            "planned": planned if planned is not None else [
                {"category": "Sleep", "hours": 56}, {"category": "Work", "hours": 112}],
            "actual": actual if actual is not None else [
                {"category": "Sleep", "hours": 49.5}, {"category": "Work", "hours": 101}]}


def view(m=None, t=None):
    snap = {"received_at": NOW.isoformat()}
    if m is not None:
        snap["money"] = m
    if t is not None:
        snap["time"] = t
    return build_finance_view([snap], NOW)


def test_full_view():
    r = ls.render_life_reply(view(money(), timesec()), NOW, LINK)
    for s in ["2026-10", "$3,413.50 spent", "Needs $1,800.00", "Wants $900.00", "Savings $712.50",
              "Emergency fund $5,200.00 (52%)", "2026-11-02", "planned 168 h", "actual 150.5 h",
              LINK, "Expires in 15 minutes."]:
        assert s in r
    assert "Other $1.00" not in r


def test_only_one_section():
    r = ls.render_life_reply(view(t=timesec()), NOW, LINK)
    assert "Money: no data yet." in r and "planned 168 h" in r
    r = ls.render_life_reply(view(m=money()), NOW, LINK)
    assert "Time: no data yet." in r and "Needs $1,800.00" in r


@pytest.mark.parametrize("v", [build_finance_view([], NOW), None, {}, "x",
                               {"money": "x", "time": 5}, {"empty": False, "money": [], "time": 1}])
def test_empty_and_malformed(v):
    r = ls.render_life_reply(v, NOW, LINK)
    assert r == "<b>Life snapshot</b>\n" + ls.NO_DATA_TEXT
    assert "http" not in r and "$" not in r


def test_staleness_ages():
    r = ls.render_life_reply(view(money(as_of=iso(73)), timesec(as_of=iso(5))), NOW, LINK)
    money_age = [l for l in r.splitlines() if l.startswith("As of")]
    assert "STALE" in money_age[0] and "3 d ago" in money_age[0]
    assert "STALE" not in money_age[1] and "5 h ago" in money_age[1]
    r = ls.render_life_reply(view(m=money(as_of="garbage")), NOW, LINK)
    assert "Date unknown" in r


def test_repushed_old_numbers_stale():
    r = ls.render_life_reply(view(m=money(as_of=iso(240))), NOW, LINK)
    assert "10 d ago" in r and "STALE" in r


def test_no_actual():
    r = ls.render_life_reply(view(t=timesec(actual=[])), NOW, LINK)
    assert "Actual hours not uploaded yet" in r and "0 h" not in r.replace("168 h", "")


def test_gap_wording_and_biggest_delta():
    r = ls.render_life_reply(view(t=timesec()), NOW, LINK)
    assert "Unlogged: 17.5 h" in r
    assert "Biggest gap: Work -11 h vs plan" in r
    over = timesec(planned=[{"category": "A", "hours": 100}, {"category": "B", "hours": 60}],
                   actual=[{"category": "A", "hours": 110}, {"category": "B", "hours": 60}])
    over["actual"].append({"category": "C", "hours": 2})
    r = ls.render_life_reply(view(t=over), NOW, LINK)
    assert "Over 168 by 4 h" in r and "Biggest gap: A +10 h vs plan" in r
    tie = timesec(planned=[{"category": "A", "hours": 10}, {"category": "B", "hours": 10}],
                  actual=[{"category": "A", "hours": 8}, {"category": "B", "hours": 12}])
    assert "Biggest gap: A -2 h" in ls.render_life_reply(view(t=tie), NOW, LINK)


def test_saved_plan_marker():
    v = build_finance_view([{"received_at": NOW.isoformat(), "time": timesec()}], NOW,
                           plan={"hours": [{"category": "Sleep", "hours": 168}], "version": 2})
    assert "(your saved plan)" in ls.render_life_reply(v, NOW, LINK)


def test_escaping():
    m = money(flow=[{"category": "c", "bucket": "<b>x</b> & co", "amount": 5}],
              off=[{"name": "<i>", "balance": 1}])
    r = ls.render_life_reply(view(m=m), NOW, LINK)
    assert "<b>x</b>" not in r and "&lt;b&gt;x&lt;/b&gt; &amp; co" in r
    assert "<i>" not in r and "&lt;i&gt;" in r


def test_length_cap():
    flow = [{"category": f"c{i}", "bucket": f"B{i:02d}" + "&" * 56, "amount": 1000000 + i}
            for i in range(200)]
    off = [{"name": f"{i:02d}" + "&" * 58, "balance": 1e6, "target": 2e6} for i in range(50)]
    t = timesec(planned=[{"category": f"{i:02d}" + "z" * 58, "hours": 1} for i in range(60)],
                actual=[{"category": f"{i:02d}" + "z" * 58, "hours": 2} for i in range(60)])
    t["week_of"] = "w" * 70
    m = money(flow=flow, off=off)
    m["period"] = "p" * 70
    r = ls.render_life_reply(view(m, t), NOW, "https://x.example/f?t=" + "a" * 40)
    assert len(r) < 900 and "+47 more" in r


def test_link_rules():
    r = ls.render_life_reply(view(m=money()), NOW, None)
    assert "Link not configured" in r and "http" not in r
    assert "http" not in ls.render_life_reply(build_finance_view([], NOW), NOW, LINK)


def test_formatting():
    assert ls._money(1234567.891) == "$1,234,567.89"
    assert ls._money(-12) == "-$12.00"
    assert ls._money(None) == "unknown" and ls._hours(None) == "unknown"
    assert ls._hours(168.0) == "168 h" and ls._hours(150.5) == "150.5 h"


@pytest.mark.parametrize("v", [
    {"empty": False, "money": None, "time": {}},
    {"empty": False, "money": {"buckets": None, "off_budget": "x", "flow_total": "9"}, "time": None},
    {"empty": False, "money": {"buckets": [1, None, {"bucket": 3, "total": "x"}],
                               "off_budget": [{"name": None, "pct": "a"}], "age_hours": "z"}},
    {"empty": False, "time": {"rows": [None, {"delta": "q"}], "actual_available": True,
                              "actual_gap": "x", "planned_total": [], "week_of": 5}},
    {"empty": False, "money": {}, "time": {"rows": 3}},
])
def test_fuzz_never_raises(v):
    r = ls.render_life_reply(v, NOW, LINK)
    assert isinstance(r, str) and r.startswith("<b>Life snapshot</b>")
    assert not re.search(r"nan", r.lower())


def test_no_forbidden_imports():
    src = open(ls.__file__, encoding="utf-8").read()
    for name in ["main", "flask", "sqlite3", "personal_snapshots", "finance_model", "hours_plan"]:
        assert not re.search(rf"^\s*(import|from)\s+{name}\b", src, re.M)


def test_plan_only_prints_no_age():
    v = build_finance_view([], NOW, plan={"hours": [{"category": "Sleep", "hours": 56}], "version": 1})
    r = ls.render_life_reply(v, NOW, LINK)
    assert "Actual hours not uploaded yet" in r and "Date unknown" not in r
    assert "STALE" not in r and "As of" not in r and "(your saved plan)" in r


def test_age_boundaries_and_skew():
    def line(age):
        sec = {"as_of": "2026-11-05", "as_of_known": True, "age_hours": age, "stale": False}
        return ls._age_line(sec)
    assert "47 h ago" in line(47.6)
    assert "2 d ago" in line(48.0)
    assert "(0 h ago)" in line(-3.0)


def test_empty_section_dict_is_not_no_data_yet():
    r = ls.render_life_reply({"empty": False, "money": {}, "time": {}}, NOW, LINK)
    assert "no data yet" not in r and "unknown" in r
    assert r.count("Date unknown - treat as stale") == 2


def test_unknown_date_says_stale_once():
    r = ls.render_life_reply(view(m=money(as_of="garbage")), NOW, LINK)
    assert "Date unknown - treat as stale" in r and "stale STALE" not in r and "STALE" not in r


def test_pct_rounded_and_clamped():
    m = money(off=[{"name": "A", "balance": 1, "target": 3}, {"name": "B", "balance": 5, "target": 2}])
    r = ls.render_life_reply(view(m=m), NOW, LINK)
    assert "A $1.00 (33%)" in r and "B $5.00 (100%)" in r


# ---- TASK-287: hand-logged hours ----
def _mlog(tmp_path, monkeypatch, *texts):
    import manual_hours as mh
    monkeypatch.setenv("PERSONAL_LOG_DIR", str(tmp_path))
    for t in texts:
        mh.log_entry(t, now=NOW)
    return mh.list_all()


def _mview(manual, t="default", now=NOW):
    snaps = [{"received_at": NOW.isoformat(), "time": timesec() if t == "default" else t}] if t else []
    return build_finance_view(snaps, now, None, manual)


def test_manual_log_counts_in_actual_and_gap(tmp_path, monkeypatch):
    m = _mlog(tmp_path, monkeypatch, "reading 3h")
    r = ls.render_life_reply(_mview(m), NOW, LINK)
    assert "actual 153.5 h (3 h by hand)" in r and "Unlogged: 14.5 h" in r
    assert "PC" not in r


def test_manual_log_absent_reply_unchanged(tmp_path, monkeypatch):
    base = _mview(None)
    stripped = _mview(None)
    for k in ("manual_total", "combined_total", "combined_gap", "manual_hidden_hours"):
        stripped["time"].pop(k, None)
    r1 = ls.render_life_reply(base, NOW, LINK)
    assert r1 == ls.render_life_reply(stripped, NOW, LINK)
    assert "by hand" not in r1 and "actual 150.5 h" in r1 and "Unlogged: 17.5 h" in r1


def test_manual_log_pc_not_uploaded(tmp_path, monkeypatch):
    m = _mlog(tmp_path, monkeypatch, "reading 3h")
    r = ls.render_life_reply(_mview(m, t=None), NOW, LINK)
    assert "PC hours not uploaded yet; 3 h logged by hand" in r
    assert "0 h" not in r.replace("168 h", "") and "Unlogged" not in r


def test_manual_log_stale_pc_stays_stale(tmp_path, monkeypatch):
    m = _mlog(tmp_path, monkeypatch, "reading 3h")
    r = ls.render_life_reply(_mview(m, t=timesec(as_of=iso(400))), NOW, LINK)
    assert "STALE" in r and "16 d ago" in r


def test_manual_log_hostile_category_and_cap(tmp_path, monkeypatch):
    texts = [f"<b>x</b>{i} 0.25h" for i in range(30)]
    try:
        m = _mlog(tmp_path, monkeypatch, *texts)
    except Exception:
        m = _mlog(tmp_path, monkeypatch, *[f"cat{i} 0.25h" for i in range(30)])
    full = _mview(m)
    r = ls.render_life_reply(
        build_finance_view([{"received_at": NOW.isoformat(), "money": money(), "time": timesec()}],
                           NOW, None, m), NOW, LINK)
    assert "<b>x</b>" not in r and len(r) < 900
    assert len(ls.render_life_reply(full, NOW, LINK)) < 900


def test_manual_log_over_168_combined(tmp_path, monkeypatch):
    t = timesec(actual=[{"category": "Sleep", "hours": 160}])
    m = _mlog(tmp_path, monkeypatch, "reading 10h")
    r = ls.render_life_reply(_mview(m, t=t), NOW, LINK)
    assert "Over 168 by 2 h" in r and "actual 170 h (10 h by hand)" in r


def test_manual_log_hidden_hours_line():
    v = _mview(None)
    v["time"].update({"manual_total": 3.0, "combined_total": 153.5, "combined_gap": 14.5,
                      "manual_hidden_hours": 2.0})
    r = ls.render_life_reply(v, NOW, LINK)
    assert "2 h logged this week will show once the PC pushes the new week." in r
