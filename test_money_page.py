from datetime import datetime, timedelta, timezone

import money_page as mp

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def test_money_page_render_escapes_and_shows_age():
    snap = {"received_at": (NOW - timedelta(minutes=5)).isoformat(), "<b>k</b>": "<script>x</script>"}
    out = mp.render_money_page(snap, NOW)
    assert "<script>" not in out and "&lt;script&gt;" in out and "&lt;b&gt;k" in out
    assert "received 5 minutes ago" in out and "STALE" not in out


def test_money_page_depth_cap():
    # 6 nested levels below the snapshot render; the 7th is replaced by a stub.
    node = {"leaf": "deepvalue"}
    for i in range(5, -1, -1):
        node = {f"lvl{i}": node}
    ok = mp.render_money_page({"received_at": NOW.isoformat(), **node}, NOW)
    assert "deepvalue" in ok
    node = {"leaf": "deepvalue"}
    for i in range(6, -1, -1):
        node = {f"lvl{i}": node}
    cut = mp.render_money_page({"received_at": NOW.isoformat(), **node}, NOW)
    assert "too deep" in cut and "deepvalue" not in cut


def test_money_page_future_received_at_is_not_fresh():
    snap = {"received_at": (NOW + timedelta(hours=3)).isoformat(), "a": 1}
    age, stale = mp.age_text(snap["received_at"], NOW)
    assert stale and "clock ahead" in age
    assert "STALE" in mp.render_money_page(snap, NOW)
    # a minute or two of skew is still fresh
    assert mp.age_text((NOW + timedelta(minutes=2)).isoformat(), NOW)[1] is False


def test_money_page_row_cap_says_truncated():
    snap = {"received_at": NOW.isoformat(), **{f"k{i}": i for i in range(500)}}
    out = mp.render_money_page(snap, NOW)
    assert "truncated" in out and out.count("<tr>") <= mp.MAX_ROWS


def test_money_page_stale_flag_over_36h():
    old = {"received_at": (NOW - timedelta(hours=37)).isoformat(), "a": 1}
    fresh = {"received_at": (NOW - timedelta(hours=35)).isoformat(), "a": 1}
    assert "STALE" in mp.render_money_page(old, NOW)
    assert "STALE" not in mp.render_money_page(fresh, NOW)


def test_money_page_unparseable_received_at_is_unknown():
    out = mp.render_money_page({"received_at": "garbage", "a": 1}, NOW)
    assert "unknown" in out
    assert "unknown" in mp.render_money_page({"a": 1}, NOW)
