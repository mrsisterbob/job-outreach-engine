import re
from datetime import datetime, timezone

import pytest

import site_traffic as st

NOW = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)   # 12:00 in Detroit (EDT)


def _row(ts="2026-10-30 14:00:00", vid="v1", path="/", ref=None, bot=None):
    return (ts, vid, path, ref, bot)


def _rep(rows, days=7, **kw):
    return st.build_report(rows, NOW, days=days, **kw)


def _cat(report, name):
    return report["categories"][name]


def test_site_traffic_there_are_ten_categories_and_every_row_lands_in_exactly_one():
    assert 5 <= len(st.CATEGORIES) <= 10 and len(st.CATEGORIES) == 10 and len(set(st.CATEGORIES)) == 10
    rows = [_row(vid="a"), _row(vid="b", ref="linkedin.com"), _row(vid="c", ref="google.com"), _row(vid="d", ref="web.whatsapp.com"),
            _row(vid="e", ref="example.org"), _row(vid="f", bot="self"), _row(vid="g", bot="preview"), _row(vid="h", bot="crawler"),
            _row(vid="i", bot="probe"), _row(vid="j", bot="health")]
    rep = _rep(rows)
    assert [_cat(rep, c)["visitors"] for c in st.CATEGORIES] == [1] * 10 and rep["visitors_total"] == 10


def test_site_traffic_one_visitor_day_counts_once_with_every_page_it_viewed():
    rep = _rep([_row(ts="2026-10-30 14:00:00", path="/", ref="linkedin.com"), _row(ts="2026-10-30 14:05:00", path="/job-engine")])
    li = _cat(rep, "linkedin")
    assert li["visitors"] == 1 and li["hits"] == 2 and li["pages"]["home"] == 1 and li["pages"]["job_engine"] == 1


def test_site_traffic_the_same_visitor_id_on_two_days_is_two_visitors():
    rep = _rep([_row(ts="2026-10-29 14:00:00"), _row(ts="2026-10-30 14:00:00")])
    assert _cat(rep, "direct")["visitors"] == 2 and rep["visitors_total"] == 2


def test_site_traffic_the_earliest_view_decides_the_source():
    rep = _rep([_row(ts="2026-10-30 15:00:00", ref="google.com"), _row(ts="2026-10-30 14:00:00", ref=None)])
    assert _cat(rep, "direct")["visitors"] == 1 and _cat(rep, "search")["visitors"] == 0


@pytest.mark.parametrize("host", ["linkedin.com", "m.linkedin.com", "com.linkedin.android", "lnkd.in"])
def test_site_traffic_linkedin_hosts_land_in_linkedin(host):
    assert _cat(_rep([_row(ref=host)]), "linkedin")["visitors"] == 1


@pytest.mark.parametrize("host", ["mail.google.com", "com.google.android.gm", "web.whatsapp.com"])
def test_site_traffic_mail_and_chat_hosts_land_in_mail_chat(host):
    assert _cat(_rep([_row(ref=host)]), "mail_chat")["visitors"] == 1


def test_site_traffic_a_job_board_is_other_sites_and_starred_in_its_host_line():
    rep = _rep([_row(ref="indeed.com"), _row(vid="v2", ref="blog.example.org")])
    assert _cat(rep, "other_sites")["visitors"] == 2
    text = "\n".join(st.render_report(rep))
    assert "★indeed.com 1" in text and "blog.example.org 1" in text and "★blog" not in text


def test_site_traffic_a_preview_bot_with_an_app_counts_hits_per_app():
    rep = _rep([_row(vid="p1", bot="preview:LinkedIn"), _row(vid="p2", bot="preview:LinkedIn"), _row(vid="p3", bot="preview")])
    p = _cat(rep, "preview")
    assert p["visitors"] == 3 and p["apps"] == {"LinkedIn": 2}


def test_site_traffic_each_bot_kind_has_its_own_category_and_an_unknown_one_is_a_crawler():
    rep = _rep([_row(vid="a", bot="crawler"), _row(vid="b", bot="probe"), _row(vid="c", bot="health"),
                _row(vid="d", bot="self"), _row(vid="e", bot="mystery-bot")])
    assert [_cat(rep, c)["visitors"] for c in ("crawler", "probe", "health", "self")] == [2, 1, 1, 1]


@pytest.mark.parametrize("path,column", [("/resume/Kevin_Miller.pdf?x=1", "resume"), ("/docfiler/", "docfiler"), ("/lattice", "other"),
                                         ("/", "home"), ("/job-engine#top", "job_engine"), ("", "home")])
def test_site_traffic_paths_map_to_page_columns(path, column):
    rep = _rep([_row(path=path)])
    assert _cat(rep, "direct")["pages"][column] == 1 and sum(_cat(rep, "direct")["pages"].values()) == 1


def test_site_traffic_per_day_has_days_entries_oldest_first_and_local_midnight_decides_the_day():
    rep = _rep([_row(ts="2026-11-01 03:59:59", vid="a"), _row(ts="2026-11-01 04:00:00", vid="b")], days=3)
    # 2026-11-01 04:00 UTC is local midnight in Detroit (EDT, UTC-4): one second earlier is still Oct 31.
    now = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
    rep = st.build_report([_row(ts="2026-11-01 03:59:59", vid="a"), _row(ts="2026-11-01 04:00:00", vid="b")], now, days=3)
    assert rep["window"] == ["2026-10-30", "2026-10-31", "2026-11-01"] and _cat(rep, "direct")["per_day"] == [0, 1, 1]
    # Outside DST (EST, UTC-5) the boundary is 05:00 UTC.
    now = datetime(2026, 12, 15, 16, 0, tzinfo=timezone.utc)
    rep = st.build_report([_row(ts="2026-12-15 04:59:59", vid="a"), _row(ts="2026-12-15 05:00:00", vid="b")], now, days=2)
    assert rep["window"] == ["2026-12-14", "2026-12-15"] and _cat(rep, "direct")["per_day"] == [1, 1]


def test_site_traffic_days_are_clamped_to_one_through_ninety():
    assert _rep([], days=200)["days"] == 90 and _rep([], days=0)["days"] == 1 and _rep([], days="x")["days"] == 7


def test_site_traffic_malformed_rows_are_counted_and_never_raise():
    rows = [("only", "three", "fields"), ("not a time", "v", "/", None, None), ("2026-10-30 14:00:00", 5, "/", None, None),
            ("2026-10-30 14:00:00", "", "/", None, None), None, _row()]
    rep = _rep(rows)
    assert rep["skipped_rows"] == 5 and rep["visitors_total"] == 1


def test_site_traffic_the_contact_line_is_unknown_when_unreadable_and_a_count_when_read():
    assert "Contact form submissions: unknown (could not be read)" in "\n".join(st.render_report(_rep([_row()], lead_times=None)))
    leads = ["2026-10-30 10:00:00", "2026-10-31 10:00:00", "2026-10-29 10:00:00", "garbage", "2025-01-01 10:00:00"]
    text = "\n".join(st.render_report(_rep([_row()], lead_times=leads)))
    assert "Contact form submissions: 3 (cannot be tied to a source)" in text
    assert "Contact form submissions: 0" in "\n".join(st.render_report(_rep([_row()], lead_times=[])))


def test_site_traffic_an_empty_window_says_no_pageviews_and_keeps_the_cannot_know_block():
    text = "\n".join(st.render_report(_rep([])))
    assert "No pageviews stored for this window" in text and "90 days" in text
    assert all(s in text for s in st.CANNOT_KNOW)


def test_site_traffic_the_render_never_shows_a_visitor_id_and_escapes_a_hostile_host():
    rep = _rep([_row(vid="SECRETVISITORID123", ref="<script>alert(1)</script>.example.org")])
    text = "\n".join(st.render_report(rep))
    assert "SECRETVISITORID123" not in text and "<script>" not in text and "&lt;script&gt;" in text


@pytest.mark.parametrize("rows", [[], [_row()], [_row(bot="probe")]])
def test_site_traffic_every_cannot_know_sentence_appears_in_every_render(rows):
    text = "\n".join(st.render_report(_rep(rows)))
    assert all(s in text for s in st.CANNOT_KNOW) and len(st.CANNOT_KNOW) == 9


def test_site_traffic_a_full_ninety_day_render_is_at_most_three_messages_of_3500_characters():
    rows = []
    for d in range(90):
        ts = f"2026-{(7 + (d + 3) // 30):02d}-{((d + 3) % 30) + 1:02d} 14:00:00"
        for i, (ref, bot) in enumerate([(None, None), ("linkedin.com", None), ("google.com", None), ("mail.google.com", None),
                                         ("indeed.com", None), (None, "self"), (None, "preview:Slack"), (None, "crawler"),
                                         (None, "probe"), (None, "health")]):
            rows.append((ts, f"v{d}-{i}", "/", ref, bot))
    rep = st.build_report(rows, datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc), days=90)
    pieces = st.render_report(rep)
    assert 1 <= len(pieces) <= 3 and all(len(p) <= 3500 for p in pieces)
    assert all(_cat(rep, c)["visitors"] > 0 for c in st.CATEGORIES)


def test_site_traffic_longer_windows_are_shown_per_week_with_a_label_that_says_so():
    short = "\n".join(st.render_report(_rep([_row()], days=14)))
    assert any(ln.startswith("per day:") for ln in short.splitlines()) and "per week" not in short
    long_text = "\n".join(st.render_report(_rep([_row()], days=20)))
    assert "per week (7 days each, ending today; oldest bucket is 6 days):" in long_text
    assert not any(ln.startswith("per day:") for ln in long_text.splitlines())
    line = [ln for ln in long_text.splitlines() if ln.startswith("per week")][0]
    assert len(line.split(": ", 1)[1].split()) == 3   # 6 + 7 + 7 days


def test_site_traffic_totals_across_categories_equal_visitors_total():
    rows = [_row(vid=f"v{i}", ref=r, bot=b) for i, (r, b) in enumerate(
        [(None, None), ("linkedin.com", None), ("x.org", None), (None, "crawler"), (None, "weird")] * 3)]
    rep = _rep(rows)
    assert sum(_cat(rep, c)["visitors"] for c in st.CATEGORIES) == rep["visitors_total"] == 15
    assert not re.search(r"v\d+", str(rep["categories"]))   # no visitor id in the report data
