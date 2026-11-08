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
    assert rows["Sleep"] == {"category": "Sleep", "planned": 56.0, "actual": 49.5, "delta": -6.5,
                             "manual": None, "combined": 49.5, "manual_only": False}
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
             "planned_total", "actual_total", "planned_gap", "actual_gap", "unreadable",
             "plan_source", "plan_version", "plan_updated_at", "actual_available",
             "editable_categories", "manual_total", "combined_total", "combined_gap",
             "manual_hidden_hours", "manual_week", "manual_entries", "manual_latest_at"}
ROW_KEYS = {"category", "planned", "actual", "delta", "manual", "combined", "manual_only"}


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
    assert set(v["time"]["rows"][0]) == ROW_KEYS
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


# ---- TASK-279: saved plan feeds the PLANNED column ----
import hours_plan  # noqa: E402  (test side only; finance_model itself must not import it)


def _saved(tmp_path, monkeypatch, hours):
    monkeypatch.setenv("PERSONAL_PLAN_DIR", str(tmp_path))
    cats = [h["category"] for h in hours]
    hours_plan.save_plan(hours, cats, None, now=NOW)
    return hours_plan.load_plan()


def _snap50():
    return {"received_at": "R", "time": tsec(
        planned=[{"category": "Sleep", "hours": 50}, {"category": "Work", "hours": 50}],
        actual=[{"category": "Sleep", "hours": 45}, {"category": "Gym", "hours": 3}])}


def test_plan_edit_saved_plan_overrides_snapshot_planned(tmp_path, monkeypatch):
    plan = _saved(tmp_path, monkeypatch, [{"category": "Sleep", "hours": 70},
                                          {"category": "Work", "hours": 98}])
    t = fm.build_finance_view([_snap50()], NOW, plan)["time"]
    rows = {r["category"]: r for r in t["rows"]}
    assert rows["Sleep"]["planned"] == 70.0 and rows["Work"]["planned"] == 98.0
    assert rows["Sleep"]["actual"] == 45.0 and rows["Sleep"]["delta"] == -25.0
    assert t["plan_source"] == "saved" and t["plan_version"] == 1
    assert t["plan_updated_at"] == plan["updated_at"]
    old = fm.build_finance_view([_snap50()], NOW, None)["time"]
    assert old["plan_source"] == "snapshot" and old["plan_version"] is None
    assert {r["category"]: r["planned"] for r in old["rows"]}["Sleep"] == 50.0


def test_plan_edit_categories_union_and_none_not_zero(tmp_path, monkeypatch):
    plan = _saved(tmp_path, monkeypatch, [{"category": "Sleep", "hours": 100},
                                          {"category": "Study", "hours": 68}])
    t = fm.build_finance_view([_snap50()], NOW, plan)["time"]
    rows = {r["category"]: r for r in t["rows"]}
    assert rows["Study"]["actual"] is None and rows["Study"]["delta"] is None
    assert rows["Gym"]["planned"] is None and rows["Gym"]["delta"] is None
    assert "Work" not in rows
    assert t["editable_categories"] == ["Sleep", "Study", "Gym"]


def test_plan_edit_plan_only_no_snapshots(tmp_path, monkeypatch):
    plan = _saved(tmp_path, monkeypatch, [{"category": "Sleep", "hours": 168}])
    v = fm.build_finance_view([], NOW, plan)
    t = v["time"]
    assert v["empty"] is False and t is not None
    assert t["actual_available"] is False and t["actual_total"] is None
    assert t["received_at"] is None and t["stale"] is False and t["as_of_known"] is False
    assert t["rows"][0]["actual"] is None
    assert fm.build_finance_view([], NOW, None)["empty"] is True


def test_plan_edit_168_is_exact(tmp_path, monkeypatch):
    plan = _saved(tmp_path, monkeypatch, [{"category": "A", "hours": 55.25},
                                          {"category": "B", "hours": 40.75},
                                          {"category": "C", "hours": 72}])
    t = fm.build_finance_view([_snap50()], NOW, plan)["time"]
    assert t["planned_total"] == 168.0 and t["planned_gap"] == 0.0


def test_plan_edit_garbage_plans_never_raise():
    base = fm.build_finance_view([_snap50()], NOW)["time"]
    for bad in ("x", {}, {"hours": "no"}, {"hours": [1, 2]}):
        t = fm.build_finance_view([_snap50()], NOW, bad)["time"]
        assert t["plan_source"] == "snapshot"
        assert t["rows"] == base["rows"] and t["planned_total"] == base["planned_total"]
        assert t["unreadable"] > base["unreadable"]


def test_plan_edit_json_safe_and_no_passthrough(tmp_path, monkeypatch):
    plan = _saved(tmp_path, monkeypatch, [{"category": "Sleep", "hours": 168}])
    plan["hours"][0]["secret"] = "leak"
    plan["extra"] = "leak2"
    v = fm.build_finance_view([_snap50()], NOW, plan)
    dumped = json.dumps(v, allow_nan=False)
    assert set(v["time"]) == TIME_KEYS
    assert "leak" not in dumped


def test_plan_edit_source_has_no_forbidden_imports():
    src = Path(fm.__file__).read_text(encoding="utf-8")
    for mod in ("hours_plan", "personal_snapshots", "flask", "sqlite3"):
        assert f"import {mod}" not in src and f"from {mod} " not in src


def test_plan_edit_huge_hours_do_not_raise():
    big = {"received_at": "R", "time": tsec(
        planned=[{"category": "A", "hours": 1e308}, {"category": "B", "hours": 5}],
        actual=[{"category": "A", "hours": 1e308}])}
    t = fm.build_finance_view([big], NOW)["time"]
    assert t["unreadable"] == 2 and t["actual_total"] is None
    json.dumps(t, allow_nan=False)
    plan = {"version": 1, "updated_at": "u", "hours": [{"category": "A", "hours": 1e308},
                                                       {"category": "B", "hours": 168}]}
    t = fm.build_finance_view([_snap50()], NOW, plan)["time"]
    assert t["plan_source"] == "saved" and t["unreadable"] >= 1 and t["planned_total"] == 168.0
    json.dumps(t, allow_nan=False)
    two = {"time": tsec(planned=[{"category": "A", "hours": 9999}, {"category": "A", "hours": 9999}])}
    t = fm.build_finance_view([two], NOW)["time"]
    assert t["unreadable"] == 1 and t["planned_total"] == 9999.0
    json.dumps(t, allow_nan=False)


# ---- TASK-285: hand-logged hours beside the PC totals ----
# manual_hours is imported lazily inside the tests that need it (finance_model itself must not import it)

NEW_TIME_KEYS = {"manual_total", "combined_total", "combined_gap", "manual_hidden_hours",
                 "manual_week", "manual_entries", "manual_latest_at"}


def _log(tmp_path, monkeypatch, *texts, now=NOW):
    import manual_hours as mh
    monkeypatch.setenv("PERSONAL_LOG_DIR", str(tmp_path))
    for t in texts:
        mh.log_entry(t, now=now)
    return mh.list_all()


def _sleep_snap(**kw):
    return {"received_at": "R", "time": tsec(
        planned=[{"category": "Sleep", "hours": 56}],
        actual=[{"category": "Sleep", "hours": 49.5}], **kw)}


def test_manual_log_adds_beside_pc_hours(tmp_path, monkeypatch):
    m = _log(tmp_path, monkeypatch, "sleep 1h")
    t = fm.build_finance_view([_sleep_snap()], NOW, None, m)["time"]
    r = t["rows"][0]
    assert (r["actual"], r["manual"], r["combined"], r["delta"]) == (49.5, 1.0, 50.5, -5.5)
    assert t["actual_total"] == 49.5 and t["manual_total"] == 1.0 and t["combined_total"] == 50.5
    assert t["combined_gap"] == 117.5 and t["actual_gap"] == 118.5
    assert t["manual_entries"][0]["category"] == "sleep"


def test_manual_log_typed_category_is_not_editable(tmp_path, monkeypatch):
    m = _log(tmp_path, monkeypatch, "side work 2h")
    t = fm.build_finance_view([_sleep_snap()], NOW, None, m)["time"]
    row = [r for r in t["rows"] if r["category"] == "side work"][0]
    assert row["manual_only"] is True and row["planned"] is None and row["actual"] is None
    assert "side work" not in t["editable_categories"] and t["editable_categories"] == ["Sleep"]
    assert t["combined_total"] == 51.5 and t["manual_total"] == 2.0
    assert t["rows"][0]["manual_only"] is False


def test_manual_log_other_weeks(tmp_path, monkeypatch):
    old = NOW - timedelta(days=14)
    nxt = NOW + timedelta(days=7)
    _log(tmp_path, monkeypatch, "gym 1h", now=old)
    _log(tmp_path, monkeypatch, "gym 3h", now=nxt)
    m = _log(tmp_path, monkeypatch, "gym 2h")
    # section covers the current week: older and later weeks are ignored
    t = fm.build_finance_view([_sleep_snap()], NOW, None, m)["time"]
    assert t["manual_total"] == 2.0 and t["manual_hidden_hours"] == 0.0
    # section covers an older week: the current week's entries are reported, not dropped
    snap_old = {"time": tsec(week_of="2026-10-19", actual=[{"category": "Sleep", "hours": 5}])}
    t = fm.build_finance_view([snap_old], NOW, None, m)["time"]
    assert t["manual_total"] == 1.0 and t["manual_week"] == "2026-10-19"
    assert t["manual_hidden_hours"] == 2.0
    snap_gap = {"time": tsec(week_of="2026-09-07", actual=[{"category": "Sleep", "hours": 5}])}
    t = fm.build_finance_view([snap_gap], NOW, None, m)["time"]
    assert t["manual_total"] is None and t["manual_hidden_hours"] == 2.0


def test_manual_log_only_manual_makes_a_time_section(tmp_path, monkeypatch):
    m = _log(tmp_path, monkeypatch, "reading 1.5h")
    v = fm.build_finance_view([], NOW, None, m)
    t = v["time"]
    assert v["empty"] is False and t is not None
    assert t["actual_available"] is False and t["actual_total"] is None
    assert t["manual_total"] == 1.5 and t["combined_total"] == 1.5
    assert fm.build_finance_view([], NOW, None, [])["empty"] is True


def test_manual_log_does_not_freshen_stale_pc_data(tmp_path, monkeypatch):
    m = _log(tmp_path, monkeypatch, "sleep 1h")
    snap = _sleep_snap()
    snap["time"]["as_of"] = iso(400)
    t = fm.build_finance_view([snap], NOW, None, m)["time"]
    assert t["stale"] is True and t["manual_latest_at"] == m[0]["logged_at"]
    assert fm.build_finance_view([_sleep_snap()], NOW)["time"]["manual_latest_at"] is None


def test_manual_log_garbage_is_unreadable_never_raises(tmp_path, monkeypatch):
    good = _log(tmp_path, monkeypatch, "sleep 1h")[0]
    bad = [dict(good, id=10, hours=True), dict(good, id=11, hours="2"), dict(good, id=12, hours=-1),
           dict(good, id=13, hours=0), dict(good, id=14, hours=float("nan")),
           dict(good, id=15, hours=1e308), {"hours": 1, "week_start": "2026-11-02"},
           dict(good, id=16, category=7), dict(good, id=17, week_start="11/02/2026"),
           dict(good, id=18, week_start=None), "x", None, 5]
    base = fm.build_finance_view([_sleep_snap()], NOW)["time"]
    t = fm.build_finance_view([_sleep_snap()], NOW, None, bad)["time"]
    assert t["unreadable"] == base["unreadable"] + len(bad)
    assert t["manual_total"] is None and t["rows"][0]["manual"] is None
    json.dumps(t, allow_nan=False)
    for junk in ("x", {"a": 1}, 5):
        assert fm.build_finance_view([_sleep_snap()], NOW, None, junk)["time"]["rows"] == base["rows"]


def test_manual_log_none_and_empty_equal_old_view(tmp_path, monkeypatch):
    plan = _saved(tmp_path, monkeypatch, [{"category": "Sleep", "hours": 100},
                                          {"category": "Study", "hours": 68}])
    for p in (None, plan):
        old = fm.build_finance_view([_snap50()], NOW, p)
        for m in (None, []):
            assert fm.build_finance_view([_snap50()], NOW, p, m) == old
    t = old["time"]
    assert t["manual_total"] is None and t["combined_total"] == t["actual_total"]
    assert t["manual_entries"] == [] and t["manual_week"] is None
    for r in t["rows"]:
        assert r["combined"] == r["actual"] and r["manual"] is None


def test_manual_log_json_safe_and_no_passthrough(tmp_path, monkeypatch):
    m = _log(tmp_path, monkeypatch, "sleep 1h", "side work 2h")
    m[0]["secret"] = "leak"
    v = fm.build_finance_view([_sleep_snap()], NOW, None, m)
    dumped = json.dumps(v, allow_nan=False)
    assert "leak" not in dumped
    assert set(v["time"]) == TIME_KEYS and NEW_TIME_KEYS <= TIME_KEYS
    assert set(v["time"]["rows"][0]) == ROW_KEYS
    assert set(v["time"]["manual_entries"][0]) == {"category", "hours", "logged_at"}


def test_manual_log_case_and_spacing_add_to_existing(tmp_path, monkeypatch):
    m = _log(tmp_path, monkeypatch, "sleep 1h", "Sleep 0.5h")
    m.append(dict(m[0], id=99, category="  SLEEP  "))
    t = fm.build_finance_view([_sleep_snap()], NOW, None, m)["time"]
    assert len(t["rows"]) == 1 and t["rows"][0]["category"] == "Sleep"
    assert t["rows"][0]["manual"] == 2.5 and t["rows"][0]["combined"] == 52.0


def test_manual_log_totals_exact_and_over_168(tmp_path, monkeypatch):
    plan = _saved(tmp_path, monkeypatch, [{"category": "A", "hours": 55.25},
                                          {"category": "B", "hours": 40.75},
                                          {"category": "C", "hours": 72}])
    t = fm.build_finance_view([_snap50()], NOW, plan, [])["time"]
    assert t["planned_total"] == 168.0
    import manual_hours as mh
    monkeypatch.setenv("PERSONAL_LOG_DIR", str(tmp_path / "log"))
    for _ in range(7):
        mh.log_entry("deep work 24h", now=NOW)
    t = fm.build_finance_view([_snap50()], NOW, plan, mh.list_all())["time"]
    assert t["planned_total"] == 168.0 and t["planned_gap"] == 0.0
    assert t["manual_total"] == 168.0 and t["actual_total"] == 48.0
    assert t["combined_total"] == 216.0 and t["combined_gap"] == -48.0


def test_manual_log_source_has_no_forbidden_imports():
    src = Path(fm.__file__).read_text(encoding="utf-8")
    for mod in ("manual_hours", "hours_plan", "personal_snapshots", "flask", "sqlite3"):
        assert f"import {mod}" not in src and f"from {mod} " not in src


def _e(i, cat, hours, ws="2026-11-02", at="2026-11-03T10:00:00+00:00"):
    return {"id": i, "logged_at": at, "week_start": ws, "category": cat, "hours": hours}


def test_manual_log_duplicate_ids_count_once():
    one = _e(1, "Sleep", 1)
    t = fm.build_finance_view([_sleep_snap()], NOW, None, [one, dict(one), one])["time"]
    assert t["manual_total"] == 1.0 and len(t["manual_entries"]) == 1
    both = fm.build_finance_view([_sleep_snap()], NOW, None, [one, _e(2, "Sleep", 1)])["time"]
    assert both["manual_total"] == 2.0
    # entries without a usable id are kept
    noid = [{k: v for k, v in one.items() if k != "id"}] * 2
    assert fm.build_finance_view([_sleep_snap()], NOW, None, noid)["time"]["manual_total"] == 2.0


def test_manual_log_non_monday_week_start_is_hidden_not_lost():
    t = fm.build_finance_view([_sleep_snap()], NOW, None,
                              [_e(1, "Sleep", 2, ws="2026-11-04"), _e(2, "Sleep", 1)])["time"]
    assert t["manual_total"] == 1.0 and t["manual_hidden_hours"] == 2.0 and t["unreadable"] == 0


def test_manual_log_row_cap_moves_dropped_hours_to_hidden():
    planned = [{"category": f"c{i}", "hours": 1} for i in range(59)]
    snap = {"time": tsec(planned=planned)}
    m = [_e(1, "typed one", 2), _e(2, "typed two", 3)]
    t = fm.build_finance_view([snap], NOW, None, m)["time"]
    assert len(t["rows"]) == fm.MAX_TIME_CATEGORIES
    assert [r["category"] for r in t["rows"] if r["manual_only"]] == ["typed one"]
    assert t["manual_total"] == 2.0 and t["combined_total"] == 2.0
    assert t["manual_hidden_hours"] == 3.0 and t["unreadable"] == 0
    assert [e["category"] for e in t["manual_entries"]] == ["typed one"]
    assert sum(e["hours"] for e in t["manual_entries"]) == t["manual_total"]


def test_manual_log_category_cap_is_order_independent():
    a, b = _e(1, "Sleep", 9000), _e(2, "Sleep", 9000)
    r1 = fm.build_finance_view([_sleep_snap()], NOW, None, [a, b])["time"]
    r2 = fm.build_finance_view([_sleep_snap()], NOW, None, [b, a])["time"]
    assert r1["manual_total"] == r2["manual_total"] == 9000.0
    assert r1["unreadable"] == r2["unreadable"] == 1
    assert r1["manual_entries"] == r2["manual_entries"]


def test_manual_log_entries_add_up_to_total():
    m = [_e(1, "Sleep", 0.333), _e(2, "Sleep", 0.333), _e(3, "Sleep", 0.333)]
    t = fm.build_finance_view([_sleep_snap()], NOW, None, m)["time"]
    assert round(sum(e["hours"] for e in t["manual_entries"]), 2) == 0.99
    assert t["manual_total"] == 1.0


def test_manual_log_naive_now_and_bad_week_of():
    naive = datetime(2026, 11, 5, 12, 0)
    m = [_e(1, "Sleep", 1)]
    assert fm.build_finance_view([_sleep_snap()], naive, None, m)["time"]["manual_total"] == 1.0
    bad = {"time": tsec(week_of="not a date", actual=[{"category": "Sleep", "hours": 5}])}
    t = fm.build_finance_view([bad], NOW, None, m)["time"]
    assert t["manual_week"] == "2026-11-02" and t["manual_total"] == 1.0
    json.dumps(t, allow_nan=False)


def test_manual_log_plan_only_manual_only_row(tmp_path, monkeypatch):
    plan = _saved(tmp_path, monkeypatch, [{"category": "Sleep", "hours": 168}])
    v = fm.build_finance_view([], NOW, plan, [_e(1, "side work", 2), _e(2, "sleep", 1)])
    t = v["time"]
    rows = {r["category"]: r for r in t["rows"]}
    assert rows["side work"]["manual_only"] is True and rows["Sleep"]["combined"] == 1.0
    assert t["editable_categories"] == ["Sleep"] and t["actual_available"] is False
    assert t["planned_total"] == 168.0 and t["combined_total"] == 3.0
