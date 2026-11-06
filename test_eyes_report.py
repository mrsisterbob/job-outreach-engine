from datetime import datetime, timezone

import eyes_report as er

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def _row(hours_ago, vid="a", path="/", ref=None, bot=None, label=None):
    from datetime import timedelta
    t = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return (t, vid, path, ref, bot, label)


def test_eyes_cmd_pure_window_boundaries():
    rep = er.build_eyes([_row(1), _row(23.5, "b"), _row(25, "c"), _row(24 * 6, "d"), _row(24 * 8, "e")], NOW)
    assert rep["windows"]["24h"]["visitors"] == 2 and rep["windows"]["24h"]["views"] == 2
    assert rep["windows"]["7d"]["visitors"] == 4 and rep["windows"]["7d"]["views"] == 4


def test_eyes_cmd_pure_top5_cut():
    rows = [_row(1, f"v{i}", f"/p{i}") for i in range(9)]
    rep = er.build_eyes(rows, NOW)
    assert len(rep["windows"]["24h"]["pages"]) == 5


def test_eyes_cmd_pure_recent_cap_and_order():
    rows = [_row(i + 1, f"v{i}", f"/p{i}") for i in range(15)]
    rep = er.build_eyes(rows, NOW)
    assert len(rep["recent"]) == 10
    assert [r["path"] for r in rep["recent"]] == [f"/p{i}" for i in range(10)]


def test_eyes_cmd_pure_unparseable_timestamp_counted():
    rep = er.build_eyes([("garbage", "a", "/", None, None, None), _row(1)], NOW)
    assert rep["skipped_rows"] == 1 and rep["windows"]["24h"]["views"] == 1


def test_eyes_cmd_pure_bots_excluded_and_counted_once():
    rep = er.build_eyes([_row(1, bot="self"), _row(2, bot="crawler"), _row(3)], NOW)
    w = rep["windows"]["24h"]
    assert w["visitors"] == 1 and w["excluded_views"] == 2
    text = "\n".join(er.render_eyes(rep, lambda s: s))
    assert "2 bot or own-device views not counted" in text


def test_eyes_cmd_pure_chunks_under_limit_with_200_pages():
    rows = [_row(1, f"v{i}", "/" + "x" * 300 + str(i), label="L" * 300) for i in range(200)]
    pieces = er.render_eyes(er.build_eyes(rows, NOW), lambda s: s)
    assert pieces and all(len(p) < 3500 for p in pieces)


def test_eyes_cmd_pure_escapes_and_empty():
    rep = er.build_eyes([_row(1, path="/<b>x</b>", label="Jane <b>Doe</b>")], NOW)
    text = "\n".join(er.render_eyes(rep, lambda s: s))
    assert "<b>Doe</b>" not in text and "&lt;b&gt;Doe" in text
    empty = "\n".join(er.render_eyes(er.build_eyes([], NOW), lambda s: s))
    assert "no visits" in empty


def test_eyes_cmd_pure_tagged_line_capped_with_and_n_more():
    rows = [_row(1, f"v{i}", "/", label=f"Contact{i:02d}-" + "L" * 280) for i in range(60)]
    pieces = er.render_eyes(er.build_eyes(rows, NOW), lambda s: s)
    text = "\n".join(pieces)
    assert all(len(p) < 3500 for p in pieces)
    tagged = [l for l in text.split("\n") if l.startswith("Tagged links:")]
    assert tagged and all(l.endswith(", and 50 more") for l in tagged)   # whole line kept, not chunk-cut


def test_eyes_cmd_pure_singular_forms():
    text = "\n".join(er.render_eyes(er.build_eyes([_row(1)], NOW), lambda s: s))
    assert "1 visitor, 1 pageview" in text and "1 visitors" not in text and "1 pageviews" not in text
