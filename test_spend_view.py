from datetime import datetime, timedelta, timezone

import spend_view as sv

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


def _ts(days_ago=0):
    return (NOW - timedelta(days=days_ago)).isoformat()


def _apify(rows=(), console=None, cap=10.0):
    return {"ledger_rows": None if rows is None else list(rows), "console_month_usd": console, "cap_usd": cap}


def _only(rows):
    (r,) = rows
    return r


def test_spend_view_hirebase_left_subtracts_the_held_back_reserve():
    r = _only(sv.build_rows({"month_used": 140, "cap": 500, "held_back": 100}, None, None, None, {sv.HIREBASE: 12}, NOW))
    assert (r.this_run, r.this_month, r.left, r.note, r.basis) == (12, 140, 260, "held back 100", "ledger")
    assert sv.render_lines([r]) == ["Hirebase: 12 jobs this run, 140 of 500 this month (260 left, held back 100)"]


def test_spend_view_hirebase_unknown_month_is_unknown_not_zero():
    r = _only(sv.build_rows({"month_used": None, "cap": 500, "held_back": 100}, None, None, None, {}, NOW))
    assert r.this_month is None and r.left is None and r.basis == "unknown"
    assert "unknown this month" in sv.render_lines([r])[0] and " 0 of" not in sv.render_lines([r])[0]


def test_spend_view_apify_month_is_the_ledger_sum_when_there_is_no_console_figure():
    r = _only(sv.build_rows(None, None, _apify([(_ts(1), 0.60, "ok", ["a"]), (_ts(2), 0.75, "ok", ["b"])]), None, {sv.APIFY: 0.6}, NOW))
    assert abs(r.this_month - 1.35) < 1e-9 and r.basis == "ledger" and r.note == ""


def test_spend_view_console_above_the_ledger_wins_and_names_the_difference():
    r = _only(sv.build_rows(None, None, _apify([(_ts(1), 0.60, "ok", ["a"])], console=2.10), None, {}, NOW))
    assert r.this_month == 2.10 and r.basis == "console"
    assert "console is higher by $1.50" in r.note and "runs outside /w" in r.note
    assert sv.render_lines([r])[0].endswith("($7.90 left, console)")


def test_spend_view_ledger_above_the_console_wins_with_an_empty_note():
    r = _only(sv.build_rows(None, None, _apify([(_ts(1), 3.00, "ok", ["a"])], console=1.00), None, {}, NOW))
    assert r.this_month == 3.00 and r.basis == "ledger" and r.note == ""


def test_spend_view_an_old_month_ledger_row_is_not_counted():
    r = _only(sv.build_rows(None, None, _apify([(_ts(40), 9.0, "ok", ["a"]), (_ts(0), 0.5, "ok", ["b"])]), None, {}, NOW))
    assert abs(r.this_month - 0.5) < 1e-9


def test_spend_view_an_errored_ledger_row_counts_as_spent():
    r = _only(sv.build_rows(None, None, _apify([(_ts(0), 1.10, "errored", ["a"])]), None, {}, NOW))
    assert abs(r.this_month - 1.10) < 1e-9


def test_spend_view_unreadable_ledger_and_no_console_is_unknown_with_a_note():
    r = _only(sv.build_rows(None, None, _apify(None), None, {}, NOW))
    assert (r.this_month, r.left, r.basis, r.note) == (None, None, "unknown", "ledger unreadable")
    line = sv.render_lines([r])[0]
    assert "unknown this month" in line and "$0.00" not in line.split("this run,")[1]


def test_spend_view_unreadable_ledger_with_a_console_figure_uses_the_console():
    r = _only(sv.build_rows(None, None, _apify(None, console=2.0), None, {}, NOW))
    assert r.this_month == 2.0 and r.basis == "console" and r.left == 8.0


def test_spend_view_cap_zero_is_off():
    r = _only(sv.build_rows(None, None, _apify([], cap=0), None, {}, NOW))
    assert r.left == 0 and r.note == "off" and sv.render_lines([r]) == ["Apify Feed: off (cap is 0)"]


def test_spend_view_at_cap_and_unknown_sources_partition_the_rows():
    rows = sv.build_rows({"month_used": 400, "cap": 500, "held_back": 100}, {"month_units": None, "cap": 24},
                         _apify([(_ts(0), 5.0, "ok", [])]), _apify(None), {}, NOW)
    assert sv.at_cap(rows) == {sv.HIREBASE}
    assert sv.unknown_sources(rows) == {sv.HIREBASE_LOOKUPS, sv.RESOLVER}
    assert not sv.at_cap(rows) & sv.unknown_sources(rows)
    assert "CAP REACHED" in sv.render_lines(rows)[0]


def test_spend_view_a_none_source_makes_no_row():
    assert sv.build_rows(None, None, None, None, {}, NOW) == []
    assert [r.source for r in sv.build_rows(None, None, _apify([]), None, {}, NOW)] == [sv.APIFY]


def test_spend_view_the_real_2026_10_26_case():
    r = _only(sv.build_rows(None, None, _apify([(_ts(0), 0.60, "ok", ["a"])]), None, {sv.APIFY: 0.60}, NOW))
    assert sv.render_lines([r]) == ["Apify Feed: $0.60 this run, $0.60 of $10.00 this month ($9.40 left)"]


def test_spend_view_under_a_cent_prints_three_decimals_and_zero_spend_is_a_measured_zero():
    r = _only(sv.build_rows(None, None, _apify([]), None, {sv.APIFY: 0.001}, NOW))
    line = sv.render_lines([r])[0]
    assert "$0.001 this run" in line and "$0.00 of $10.00 this month" in line and r.basis == "ledger"
