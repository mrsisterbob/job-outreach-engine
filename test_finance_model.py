import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import finance_model as fm

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def iso(h):
    return (NOW - timedelta(hours=h)).isoformat()


def money(**kw):
    d = {"as_of": iso(1), "period": "2026-10", "flow": [], "off_budget": []}
    d.update(kw)
    return d


def tsec(**kw):
    d = {"as_of": iso(1), "week_of": "2026-11-02", "planned": [], "actual": []}
    d.update(kw)
    return d


def test_finance_model_full_snapshot():
    snap = {"received_at": "R1", "money": money(
        flow=[{"category": "Groceries", "bucket": "Needs", "amount": 400.25},
              {"category": "Rent", "bucket": "Needs", "amount": 1000},
              {"category": "Games", "bucket": "Wants", "amount": 50.5}],
        off_budget=[{"name": "Emergency", "balance": 5200, "target": 10000},
                    {"name": "Loan", "balance": -300}]),
        "time": tsec(planned=[{"category": "Sleep", "hours": 56}, {"category": "Work", "hours": 40}],
                     actual=[{"category": "Sleep", "hours": 49.5}, {"category": "Gym", "hours": 3}])}
    v = fm.build_finance_view([snap], NOW)
    m, t = v["money"], v["time"]
    assert v["empty"] is False
    assert [b["bucket"] for b in m["buckets"]] == ["Needs", "Wants"]
    assert m["buckets"][0]["total"] == 1400.25
    assert [c["category"] for c in m["buckets"][0]["categories"]] == ["Rent", "Groceries"]
    assert m["flow_total"] == 1450.75
    assert m["off_budget"][0]["pct"] == 52
    assert m["off_budget"][1]["pct"] is None and m["off_budget"][1]["balance"] == -300
    assert m["received_at"] == "R1" and m["period"] == "2026-10"
    rows = {r["category"]: r for r in t["rows"]}
    assert rows["Sleep"] == {"category": "Sleep", "planned": 56.0, "actual": 49.5, "delta": -6.5}
    assert rows["Work"]["actual"] is None and rows["Work"]["delta"] is None
    assert rows["Gym"]["planned"] is None
    assert t["planned_total"] == 96.0 and t["actual_total"] == 52.5
    assert t["planned_gap"] == 72.0 and t["actual_gap"] == 115.5


def test_finance_model_partial_newest_does_not_hide_older():
    newer = {"received_at": "NEW", "time": tsec()}
    older = {"received_at": "OLD", "money": money(), "time": tsec(week_of="old")}
    v = fm.build_finance_view([newer, older], NOW)
    assert v["money"]["received_at"] == "OLD"
    assert v["time"]["received_at"] == "NEW" and v["time"]["week_of"] == "2026-11-02"


def test_finance_model_staleness_boundaries():
    def m(h):
        return fm.build_finance_view([{"money": money(as_of=iso(h))}], NOW)["money"]

    def t(h):
        return fm.build_finance_view([{"time": tsec(as_of=iso(h))}], NOW)["time"]
    assert m(71)["stale"] is False and m(73)["stale"] is True
    assert t(191)["stale"] is False and t(193)["stale"] is True
    assert m(71)["age_hours"] == 71.0 and m(71)["as_of_known"] is True


def test_finance_model_date_only_and_unknown_as_of():
    v = fm.build_finance_view([{"money": money(as_of="2026-11-05")}], NOW)["money"]
    assert v["as_of_known"] is True and v["age_hours"] == 12.0 and v["stale"] is False
    for bad in (None, "garbage", "", 5, iso(-49)):
        r = fm.build_finance_view([{"money": money(as_of=bad)}], NOW)["money"]
        assert r["as_of_known"] is False and r["age_hours"] is None and r["stale"] is True
    s = money()
    del s["as_of"]
    r = fm.build_finance_view([{"money": s}], NOW)["money"]
    assert r["as_of_known"] is False and r["stale"] is True
    ok = fm.build_finance_view([{"money": money(as_of=iso(-12))}], NOW)["money"]
    assert ok["as_of_known"] is True


def test_finance_model_old_as_of_stays_stale_when_just_received():
    snap = {"received_at": NOW.isoformat(), "money": money(as_of=iso(24 * 30))}
    assert fm.build_finance_view([snap], NOW)["money"]["stale"] is True


def test_finance_model_unreadable_rows_counted_not_zeroed():
    flow = [{"category": "Ok", "bucket": "B", "amount": 10},
            {"category": "Neg", "bucket": "B", "amount": -1},
            {"category": "Str", "bucket": "B", "amount": "12"},
            {"category": "Bool", "bucket": "B", "amount": True},
            {"category": "Nan", "bucket": "B", "amount": float("nan")},
            {"category": "  ", "bucket": "B", "amount": 5},
            {"category": "x" * 61, "bucket": "B", "amount": 5},
            "junk"]
    v = fm.build_finance_view(
        [{"money": money(flow=flow, off_budget=[{"name": "A", "balance": "9"}])}], NOW)
    m = v["money"]
    assert m["unreadable"] == 8
    assert m["flow_total"] == 10.0 and len(m["buckets"][0]["categories"]) == 1
    assert m["off_budget"] == []
    allbad = fm.build_finance_view([{"money": money(flow=[{"amount": -1}]),
                                     "time": tsec(planned=[{"category": "S", "hours": -2}])}], NOW)
    assert allbad["money"]["buckets"] == [] and allbad["money"]["unreadable"] == 1
    assert allbad["time"]["rows"] == [] and allbad["time"]["unreadable"] == 1
    assert allbad["empty"] is False


def test_finance_model_duplicates_summed_and_caps():
    flow = [{"category": "A", "bucket": "B", "amount": 1.1}, {"category": "A", "bucket": "B", "amount": 2.2}]
    m = fm.build_finance_view([{"money": money(flow=flow)}], NOW)["money"]
    assert m["buckets"][0]["categories"] == [{"category": "A", "amount": 3.3}]
    t = fm.build_finance_view([{"time": tsec(planned=[{"category": "S", "hours": 1.04},
                                                      {"category": "S", "hours": 2.04}])}], NOW)["time"]
    assert t["rows"][0]["planned"] == 3.1
    big = [{"category": f"c{i}", "bucket": "B", "amount": 1} for i in range(250)]
    ob = [{"name": f"n{i}", "balance": 1} for i in range(60)]
    m = fm.build_finance_view([{"money": money(flow=big, off_budget=ob)}], NOW)["money"]
    assert len(m["buckets"][0]["categories"]) == 200 and len(m["off_budget"]) == 50
    assert m["unreadable"] == 50 + 10
    tc = [{"category": f"c{i}", "hours": 1} for i in range(70)]
    t = fm.build_finance_view([{"time": tsec(planned=tc)}], NOW)["time"]
    assert len(t["rows"]) == 60 and t["unreadable"] == 10


def test_finance_model_empty_inputs():
    for snaps in ([], [{}], [{"money": "x", "time": [1]}], None, ["x"]):
        v = fm.build_finance_view(snaps, NOW)
        assert v == {"empty": True, "money": None, "time": None}


MONEY_KEYS = {"as_of", "as_of_known", "age_hours", "stale", "received_at", "period", "buckets",
              "flow_total", "off_budget", "unreadable"}
TIME_KEYS = {"as_of", "as_of_known", "age_hours", "stale", "received_at", "week_of", "rows",
             "planned_total", "actual_total", "planned_gap", "actual_gap", "unreadable"}


def test_finance_model_json_safe_and_only_documented_keys():
    snap = {"received_at": "R", "secret": 1, "money": money(
        extra="leak", flow=[{"category": "G", "bucket": "N", "amount": 1, "memo": "x"}],
        off_budget=[{"name": "E", "balance": 1, "target": 2, "note": "n"}]),
        "time": tsec(planned=[{"category": "S", "hours": 1}], actual=[{"category": "S", "hours": 2}])}
    v = fm.build_finance_view([snap], NOW)
    dumped = json.dumps(v, allow_nan=False)
    assert set(v) == {"empty", "money", "time"}
    assert set(v["money"]) == MONEY_KEYS and set(v["time"]) == TIME_KEYS
    assert set(v["money"]["buckets"][0]) == {"bucket", "total", "categories"}
    assert set(v["money"]["buckets"][0]["categories"][0]) == {"category", "amount"}
    assert set(v["money"]["off_budget"][0]) == {"name", "balance", "target", "pct"}
    assert set(v["time"]["rows"][0]) == {"category", "planned", "actual", "delta"}
    assert "leak" not in dumped and "secret" not in dumped


def test_finance_model_source_has_no_forbidden_imports():
    src = Path(fm.__file__).read_text(encoding="utf-8")
    for mod in ("sqlite3", "main", "flask", "matplotlib", "plotly", "pandas"):
        assert f"import {mod}" not in src and f"from {mod} " not in src


def test_finance_model_overflow_dates_do_not_raise():
    for bad in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-23:59", "9999-12-31"):
        r = fm.build_finance_view([{"money": money(as_of=bad)}], NOW)["money"]
        assert r["as_of_known"] is False and r["stale"] is True and r["age_hours"] is None


def test_finance_model_pct_guards():
    ob = [{"name": "a", "balance": 1e300, "target": 1e-320},
          {"name": "b", "balance": 5, "target": -10},
          {"name": "c", "balance": 5, "target": 0},
          {"name": "d", "balance": 5, "target": 10}]
    m = fm.build_finance_view([{"money": money(off_budget=ob)}], NOW)["money"]
    assert [r["pct"] for r in m["off_budget"]] == [None, None, None, 50]


def test_finance_model_no_made_up_zero_totals():
    v = fm.build_finance_view([{"money": money(flow=[{"amount": -1}]),
                                "time": tsec(planned=[{"category": "S", "hours": 5}],
                                             actual=[{"category": "S", "hours": -1}])}], NOW)
    assert v["money"]["flow_total"] is None
    t = v["time"]
    assert t["actual_total"] is None and t["actual_gap"] is None
    assert t["planned_total"] == 5.0 and t["planned_gap"] == 163.0
    e = fm.build_finance_view([{"time": tsec()}], NOW)["time"]
    assert e["planned_total"] is None and e["planned_gap"] is None


def test_finance_model_totals_include_categories_past_display_cap():
    tc = [{"category": f"c{i}", "hours": 1} for i in range(70)]
    t = fm.build_finance_view([{"time": tsec(planned=tc)}], NOW)["time"]
    assert len(t["rows"]) == 60 and t["planned_total"] == 70.0


def test_finance_model_time_lists_capped_up_front():
    big = [{"category": "S", "hours": 1}] * 100000
    t = fm.build_finance_view([{"time": tsec(planned=big)}], NOW)["time"]
    assert t["unreadable"] == 100000 - fm.MAX_HOURS_ROWS
    assert t["planned_total"] == float(fm.MAX_HOURS_ROWS)


def test_finance_model_unparseable_as_of_not_echoed_raw():
    r = fm.build_finance_view([{"money": money(as_of="z" * 5000)}], NOW)["money"]
    assert r["as_of_known"] is False and len(r["as_of"]) <= 40
