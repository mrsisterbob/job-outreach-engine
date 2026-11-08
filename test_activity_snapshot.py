import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import activity_snapshot as A

DET = ZoneInfo("America/Detroit")
TITLE = "ZZ-CANARY-TITLE-4471"
APP = "ZZ-CANARY-APP-4471.exe"


def ev(app, hours=None, secs=None):
    return {"id": 1, "timestamp": "2026-11-02T00:00:00Z",
            "duration": secs if secs is not None else hours * 3600,
            "data": {"app": app, "title": TITLE}}


def measured():
    return {"msedge.exe": 8.21 * 3600, "code.exe": 4.09 * 3600, "robloxplayerbeta.exe": 2.08 * 3600,
            "claude.exe": 0.23 * 3600, "explorer.exe": 0.08 * 3600, "lockapp.exe": 0.04 * 3600,
            "telegram.exe": 0.02 * 3600, APP.lower(): 0.5 * 3600}


NOW = datetime(2026, 11, 4, 15, 0, tzinfo=DET)


def hours_of(section):
    return {r["category"]: r["hours"] for r in section["actual"]}


def test_week_bounds_midweek():
    b = A.week_bounds(NOW)
    assert (b["week_of"], b["start"], b["end"]) == (
        "2026-11-02", "2026-11-02T05:00:00Z", "2026-11-09T05:00:00Z")
    assert b["tz"] == "America/Detroit"


def test_week_bounds_sunday_and_monday_edges():
    assert A.week_bounds(datetime(2026, 11, 8, 23, 59, tzinfo=DET))["week_of"] == "2026-11-02"
    assert A.week_bounds(datetime(2026, 11, 9, 0, 0, tzinfo=DET))["week_of"] == "2026-11-09"


def _len_hours(day):
    y, m, d = (int(x) for x in day.split("-"))
    b = A.week_bounds(datetime(y, m, d, 12, 0, tzinfo=DET))
    p = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    return (p(b["end"]) - p(b["start"])).total_seconds() / 3600


def test_week_bounds_dst_lengths():
    assert _len_hours("2026-10-28") == 169
    assert _len_hours("2027-03-10") == 167
    assert _len_hours("2026-11-18") == 168


def test_week_bounds_naive_and_utc_edges():
    assert A.week_bounds(datetime(2026, 11, 4, 20, 0))["week_of"] == "2026-11-02"
    assert A.week_bounds(datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc))["week_of"] == "2026-11-02"
    assert A.week_bounds(datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc))["week_of"] == "2026-10-26"


def test_app_seconds_nested_sums_and_counts_bad_rows():
    result = [[ev("Code.exe", 1), ev("code.exe", 2), ev(APP, 0.5),
               ev(None, 1), ev("  ", 1), ev("a.exe", secs="5"), ev("b.exe", secs=True),
               ev("c.exe", secs=float("nan")), ev("d.exe", secs=-1)]]
    secs, bad = A.app_seconds_from_query(result)
    assert secs == {"code.exe": 3 * 3600, APP.lower(): 1800}
    assert bad == 6
    assert TITLE not in repr((secs, bad))
    assert A.app_seconds_from_query(result[0]) == (secs, bad)


@pytest.mark.parametrize("bad", [None, {}, "x"])
def test_app_seconds_non_list(bad):
    assert A.app_seconds_from_query(bad) == ({}, 1)


def test_app_seconds_empty_shapes():
    assert A.app_seconds_from_query([]) == ({}, 0)
    assert A.app_seconds_from_query([[]]) == ({}, 0)
    assert A.app_seconds_from_query([None]) == ({}, 1)


def test_measured_week_buckets():
    m = measured()
    del m[APP.lower()]
    s = A.build_time_section(m, NOW)
    assert hours_of(s) == {"Building": 4.32, "Browsing": 8.21, "Gaming": 2.08,
                           "Social": 0.02, "Other": 0.08}
    assert [r["category"] for r in s["actual"]] == ["Building", "Browsing", "Gaming", "Social", "Other"]
    assert s["planned"] == [{"category": b, "hours": 0.0} for b in A.DEFAULT_BUCKETS]
    assert (s["week_of"], s["as_of"]) == ("2026-11-02", "2026-11-04")
    assert set(s) == {"as_of", "week_of", "planned", "actual"}


def test_rounding_and_omission():
    s = A.build_time_section({"code.exe": 17, "msedge.exe": 3599, "telegram.exe": 5400}, NOW)
    assert hours_of(s) == {"Browsing": 1.0, "Social": 1.5}


def test_section_never_carries_app_or_title():
    m = measured()
    s = A.build_time_section(m, NOW)
    strings = []

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                strings.append(k)
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
        elif isinstance(n, str):
            strings.append(n)
    walk(s)
    allowed = set(A.DEFAULT_BUCKETS) | {"as_of", "week_of", "planned", "actual", "category",
                                        "hours", "2026-11-02", "2026-11-04"}
    assert set(strings) <= allowed
    assert "CANARY" not in repr(s).upper().replace("CANARY", "CANARY")
    assert hours_of(s)["Other"] == 0.58
    d = A.describe_week(m, s, 2)
    assert APP.lower() in [r["app"] for r in d["apps_unmapped"]]
    assert d["unreadable"] == 2 and d["ignored_hours"] == 0.04
    assert d["tracked_hours"] == round(sum(r["hours"] for r in s["actual"]), 2)


def test_parse_config_defaults_and_valid_custom():
    assert A.parse_category_config(None) == (A.DEFAULT_BUCKETS, A.DEFAULT_APP_BUCKETS)
    assert A.parse_category_config({}) == (A.DEFAULT_BUCKETS, A.DEFAULT_APP_BUCKETS)
    b, a = A.parse_category_config({"buckets": ["Work", "Play"], "apps": {"MSEdge.exe": "play"}})
    assert b == ["Work", "Play", "Other"]
    assert a["msedge.exe"] == "Play"
    assert "code.exe" not in a and "telegram.exe" not in a


@pytest.mark.parametrize("cfg,needle", [
    ({"buckets": ["A", "a"]}, "duplicates"),
    ({"buckets": [f"b{i}" for i in range(31)]}, "1..30"),
    ({"buckets": ["A", "  "]}, "blank"),
    ({"buckets": ["A", 5]}, "not a string"),
    ({"buckets": ["x" * 41]}, "longer than 40"),
    ({"buckets": ["A"], "apps": {"x.exe": "Nope"}}, "not in buckets"),
    ({"apps": ["x"]}, "apps must be a dict"),
    ({"buckets": "A"}, "buckets must be a list"),
])
def test_parse_config_invalid(cfg, needle):
    with pytest.raises(ValueError) as e:
        A.parse_category_config(cfg)
    assert needle in str(e.value)


def test_parse_config_lists_every_problem():
    with pytest.raises(ValueError) as e:
        A.parse_category_config({"buckets": ["A", "a", ""], "apps": {"x.exe": "Zed"}})
    msg = str(e.value)
    assert "duplicates" in msg and "blank" in msg and "not in buckets" in msg


def test_write_path_store_and_finance_view(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSONAL_SNAPSHOT_DIR", str(tmp_path))
    import personal_snapshots as P
    m = measured()
    del m[APP.lower()]
    s = A.build_time_section(m, NOW)
    P.validate_snapshot({"time": s})
    P.save_snapshot({"time": s})
    stored = P.load_latest()["time"]
    assert stored == s
    fm = pytest.importorskip("finance_model")
    rec = {"time": stored, "received_at": "2026-11-04T20:00:00+00:00"}
    view = fm.build_finance_view([rec], datetime(2026, 11, 4, 21, 0, tzinfo=timezone.utc))
    assert view["time"]["editable_categories"] == A.DEFAULT_BUCKETS
    assert view["time"]["actual_total"] == round(4.32 + 8.21 + 2.08 + 0.02 + 0.08, 1)


def test_empty_input_and_pure_imports():
    s = A.build_time_section({}, NOW)
    assert s["actual"] == []
    assert [r["category"] for r in s["planned"]] == A.DEFAULT_BUCKETS
    src = open(A.__file__, encoding="utf-8").read()
    for mod in ("requests", "urllib", "socket", "sqlite3", "main", "flask"):
        assert f"import {mod}" not in src and f"from {mod}" not in src


def test_query_body_holds_exact_query():
    b = A.build_query_body("a", "b")
    assert b["timeperiods"] == ["a/b"] and len(b["query"]) == 5
    assert b["query"][2].startswith("win = filter_period_intersect(win, filter_keyvals(afk")


def test_error_messages_never_echo_app_names():
    with pytest.raises(ValueError) as e:
        A.parse_category_config({"buckets": ["A"], "apps": {APP: "Nope", "  ": "A", 5: "A"}})
    assert "CANARY" not in str(e.value).upper()
    assert "#0" in str(e.value) and "#1" in str(e.value)


def test_tz_fallback_is_spec_defined_and_reported(monkeypatch):
    # spec keeps a UTC fallback; the caller learns which zone was used via week_bounds/describe_week
    assert A.week_bounds(NOW)["tz"] == "America/Detroit"
    assert A.describe_week({}, A.build_time_section({}, NOW))["tz"] == "America/Detroit"
    monkeypatch.setattr(A, "_TZ_NAME", "Nowhere/Nope")
    assert A.week_bounds(NOW)["tz"] == "UTC"
    assert A.describe_week({}, A.build_time_section({}, NOW))["tz"] == "UTC"


def test_build_canonicalises_buckets_and_tolerates_bad_values():
    s = A.build_time_section({"a.exe": 3600, "b.exe": 3600, "c.exe": 3600, "d.exe": 3600}, NOW,
                             buckets=[" work ", "WORK", "other", 7, None, ""],
                             app_buckets={"a.exe": "WORK", "b.exe": ["x"], "c.exe": 5,
                                          "d.exe": "nope"})
    assert [r["category"] for r in s["planned"]] == ["work", "Other"]
    assert hours_of(s) == {"work": 1.0, "Other": 3.0}


def test_bad_seconds_are_filtered_and_counted():
    secs = {"code.exe": 3600, "x.exe": float("inf"), "y.exe": -5, "z.exe": float("nan"),
            "w.exe": "9", "v.exe": True}
    s = A.build_time_section(secs, NOW)
    assert hours_of(s) == {"Building": 1.0}
    d = A.describe_week(secs, s, 1)
    assert d["unreadable"] == 6 and d["tracked_hours"] == 1.0


def test_sums_are_clamped_finite():
    secs = {"code.exe": 1.7e308, "claude.exe": 1.7e308}
    h = hours_of(A.build_time_section(secs, NOW))["Building"]
    assert math.isfinite(h)
    r, _ = A.app_seconds_from_query([[ev("a.exe", secs=1.7e308), ev("a.exe", secs=1.7e308)]])
    assert math.isfinite(r["a.exe"])


def test_multiple_inner_lists_are_all_summed():
    r, bad = A.app_seconds_from_query([[ev("a.exe", 1)], [ev("a.exe", 2), ev("b.exe", 1)]])
    assert r == {"a.exe": 3 * 3600, "b.exe": 3600} and bad == 0


def test_bucket_cap_is_30_excluding_appended_other():
    # spec: 1..30 supplied buckets; "Other" is appended after validation
    b, _ = A.parse_category_config({"buckets": [f"b{i}" for i in range(30)]})
    assert len(b) == 31 and b[-1] == "Other"


def test_describe_week_totals_use_unrounded_seconds():
    secs = {"code.exe": 17, "telegram.exe": 17, "msedge.exe": 17}  # each rounds to 0.00 alone
    s = A.build_time_section(secs, NOW)
    assert s["actual"] == []
    assert A.describe_week(secs, s)["tracked_hours"] == round(51 / 3600, 2)
