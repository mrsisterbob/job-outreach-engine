"""Structural tests for ats_endpoint_report. The JSON inputs are tiny hand-written STRUCTURAL
inputs exercising generic logic; none is claimed to be any vendor's real reply."""
import json

import pytest

import ats_endpoint_report as m
from board_system_classifier import VENDOR_MARKERS


def spec(list_path=(), **kw):
    base = dict(
        system="icims", method="GET", url_template="https://example.test/{0}/{1}",
        body=None, list_path=list_path,
        title_paths=(("title",), ("name",)), location_paths=(("loc",),),
        apply_paths=(("url",),), source_note="structural test spec",
    )
    base.update(kw)
    return m.EndpointSpec(**base)


FULL = {"title": "T", "loc": "L", "url": "U"}


def test_eight_systems():
    assert len(m.UNFETCHED_SYSTEMS) == 8
    assert all(s in VENDOR_MARKERS for s in m.UNFETCHED_SYSTEMS)
    for s in ("greenhouse", "lever", "ashby", "smartrecruiters", "workday"):
        assert s not in m.UNFETCHED_SYSTEMS


def test_parse_board_arg_ok():
    assert m.parse_board_arg("icims=acme/jobs") == ("icims", "acme/jobs")


@pytest.mark.parametrize("bad", [
    "icims", "greenhouse=acme", "icims=", "icims=https://x", "icims=a@b", "icims=a:b",
    "icims=a b", "icims=../x", "icims=a?b", "icims=a//b", "icims=a/", "icims=..",
])
def test_parse_board_arg_rejects(bad):
    with pytest.raises(ValueError):
        m.parse_board_arg(bad)


def test_build_url():
    assert m.build_url(spec(), "a/b") == "https://example.test/a/b"
    with pytest.raises(ValueError):
        m.build_url(spec(), "a")
    with pytest.raises(ValueError):
        m.build_url(spec(), "a/b/c")
    with pytest.raises(ValueError):
        m.build_url(spec(url_template="http://example.test/{0}/{1}"), "a/b")


def test_resolve_path():
    obj = {"a": [{"b": 1}]}
    assert m.resolve_path(obj, ("a", 0, "b")) == 1
    assert m.resolve_path(obj, ("a", 5)) is None
    assert m.resolve_path(obj, ("a", "b")) is None
    assert m.resolve_path(obj, ("x",)) is None
    assert m.resolve_path("str", ("x",)) is None
    assert m.resolve_path(obj, ()) is obj


def test_first_value_second_candidate_and_trim():
    obj = {"a": "  ", "b": "  hi  "}
    assert m.first_value(obj, (("a",), ("b",))) == ("hi", 1)
    assert m.first_value(obj, (("a",), ("z",))) == (None, None)
    assert m.first_value({"a": ["x", "y"]}, (("a",),)) == ("x", 0)
    assert m.first_value({"a": {"city": "Oslo"}}, (("a",),)) == ("Oslo", 0)


def test_outcomes():
    s = spec()
    assert m.measure_reply(s, None, None)["outcome"] == "no_answer"
    assert m.measure_reply(s, 404, "x")["outcome"] == "http_error"
    r = m.measure_reply(s, 200, "<html>")
    assert (r["outcome"], r["parses"]) == ("not_json", False)
    assert m.measure_reply(s, 200, json.dumps({"k": 1}))["outcome"] == "json_no_list"
    assert m.measure_reply(s, 200, "[]")["outcome"] == "ok_empty"
    ok = m.measure_reply(s, 200, json.dumps([FULL]))
    assert ok["outcome"] == "ok" and ok["http_status"] == 200 and ok["posting_count"] == 1
    assert set(ok) == set(m.RESULT_KEYS)


def test_ok_empty_distinct_from_json_no_list():
    s = spec(list_path=("jobs",))
    empty = m.measure_reply(s, 200, json.dumps({"jobs": []}))
    nolist = m.measure_reply(s, 200, json.dumps({"other": []}))
    assert empty["outcome"] == "ok_empty" and empty["posting_count"] == 0
    assert nolist["outcome"] == "json_no_list" and nolist["posting_count"] is None
    assert nolist["top_level_keys"] == ["other"]


def test_counts_over_every_posting():
    body = json.dumps({"jobs": [
        FULL, {"name": "alt", "loc": "L"}, {"title": "T", "url": "U"}, {"loc": "L", "url": "U"},
    ]})
    r = m.measure_reply(spec(list_path=("jobs",)), 200, body)
    assert (r["posting_count"], r["with_title"], r["with_location"],
            r["with_apply_link"], r["with_all_three"]) == (4, 3, 3, 3, 1)
    assert r["first_posting_keys"] == ["title", "loc", "url"]


def test_mixed_types_and_bare_values_never_raise():
    r = m.measure_reply(spec(), 200, json.dumps([FULL, "str", 5, None, [1]]))
    assert r["posting_count"] == 5 and r["with_all_three"] == 1
    bare = m.measure_reply(spec(), 200, json.dumps("just a string"))
    assert bare["outcome"] == "json_no_list" and bare["parses"] is True
    assert bare["top_level_keys"] == []
    assert m.measure_reply(spec(), 200, "5")["outcome"] == "json_no_list"


def test_no_posting_text_stored_and_key_caps():
    sentinel = "SENTINEL_POSTING_TEXT_9137"
    first = {"title": sentinel, "loc": sentinel, "url": sentinel}
    first.update({"k%d" % i: 1 for i in range(30)})
    body = json.dumps({**{"t%d" % i: 1 for i in range(20)}, "jobs": [first]})
    r = m.measure_reply(spec(list_path=("jobs",)), 200, body)
    assert sentinel not in repr(r)
    assert len(r["top_level_keys"]) == 10 and len(r["first_posting_keys"]) == 15


def test_non_reply_results():
    s = spec()
    r = m.no_answer_result(s, "X" * 500)
    assert r["outcome"] == "no_answer" and len(r["error_text"]) == 120
    assert m.no_public_endpoint_result("adp", "none")["outcome"] == "no_public_endpoint"
    assert m.not_measured_result("ukg")["outcome"] == "not_measured"
    assert set(m.RESULT_KEYS) <= set(m.not_measured_result("ukg"))


def test_render_report_order_and_summary():
    good = m.measure_reply(spec(system="taleo"), 200, json.dumps([FULL, {"title": "x"}]))
    dead = m.no_answer_result(spec(system="oracle"), "TimeoutError")
    none_ = m.no_public_endpoint_result("adp", "no public API")
    empty = m.measure_reply(spec(system="ukg"), 200, "[]")
    lines = m.render_report([none_, dead, good, empty])
    heads = [ln.split(":")[0] for ln in lines if not ln.startswith(("    ", "Measured"))]
    assert heads == list(m.UNFETCHED_SYSTEMS)
    assert "icims: not measured (no board supplied)" in lines
    assert any("taleo: ok" in ln and "title 2/2" in ln and "all three 1/2" in ln for ln in lines)
    assert any("structural test spec" in ln for ln in lines)
    summary = lines[-1]
    assert "1 of 8 returned" in summary and "2 did not answer or have no public endpoint" in summary
    assert "working" not in summary.lower() and "verified" not in summary.lower()


@pytest.mark.parametrize("bad", ["icims=acme\n", "icims=acme/jobs\n", "icims=a\nb"])
def test_parse_board_arg_rejects_newline(bad):
    with pytest.raises(ValueError):
        m.parse_board_arg(bad)


@pytest.mark.parametrize("ref", ["a@b.com/x", "a/..", "a?x/b", "a#/b", "a /b", "a\n/b", "a/b\n", "a/", "/b"])
def test_build_url_validates_parts(ref):
    with pytest.raises(ValueError):
        m.build_url(spec(), ref)
