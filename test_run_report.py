import json

import pytest

import run_report as rr

BUILD = {"commit": "a" * 40, "short": "aaaaaaa", "source": "env:RENDER_GIT_COMMIT"}


def _rec(build=BUILD):
    return rr.RunRecorder("run-1", "2026-10-31 08:00", build)


@pytest.mark.parametrize("job_id,expected", [
    ("gh_acme_1", "greenhouse"), ("lever_acme_1", "lever"), ("ashby_acme_1", "ashby"), ("wd_acme_R1", "workday"),
    ("hirebase_acme_9", "hirebase"), ("apify_acme1", "apify"), ("abc123", "jsearch"), ("", "unknown"),
    (None, "unknown"), (12, "unknown")])
def test_run_report_source_from_job_id_table(job_id, expected):
    assert rr.source_from_job_id(job_id) == expected


def test_run_report_hirebase_and_apify_calls_for_one_company_stay_separate():
    r = _rec()
    r.note_source_call("hirebase", "Acme", status="empty", returned=0)
    r.note_source_call("apify", "Acme", status="ok", returned=4, kept=4)
    (c,) = r.snapshot()["companies"]
    assert set(c["sources"]) == {"hirebase", "apify"}
    assert c["sources"]["hirebase"]["status"] == "empty" and c["sources"]["apify"]["kept"] == 4


def test_run_report_two_calls_for_one_company_and_source_add_up_and_keep_both_statuses():
    r = _rec()
    r.note_source_call("hirebase", "Acme", status="ok", returned=25, kept=20)
    r.note_source_call("hirebase", "Acme", status="errored", returned=0, error="http 500")
    e = r.snapshot()["companies"][0]["sources"]["hirebase"]
    assert e["returned"] == 25 and e["kept"] == 20 and e["statuses"] == ["ok", "errored"] and e["error"] == "http 500"


def _haworth():
    r = _rec()
    r.note_source_call("apify", "Haworth Inc.", status="ok", returned=3, kept=1,
                       dropped=[("Haworth International", "no match"), ("Haworth International", "no match")])
    r.note_posting("apify", "Haworth Inc.", "Haworth Inc.")
    return r


def test_run_report_the_haworth_case_keeps_both_names_with_counts_and_shows_the_dropped_reason():
    snap = _haworth().snapshot()
    e = snap["companies"][0]["sources"]["apify"]
    assert e["returned_names"] == {"Haworth International": 2, "Haworth Inc.": 1} and e["dropped_count"] == 2
    text = rr.render_company(snap, "Haworth Inc.")
    assert 'sent "Haworth Inc."' in text and '"Haworth International" (no match) ×2' in text


def test_run_report_note_posting_counts_per_source_and_per_returned_name():
    r = _rec()
    for name in ("Acme", "Acme", "ACME Corp"):
        r.note_posting("greenhouse", "Acme", name)
    r.note_posting("apify", "Acme", "Acme")
    srcs = r.snapshot()["companies"][0]["sources"]
    assert srcs["greenhouse"]["postings"] == 3 and srcs["apify"]["postings"] == 1
    assert srcs["greenhouse"]["returned_names"] == {"Acme": 2, "ACME Corp": 1}


def test_run_report_names_over_the_cap_fold_into_a_count_and_truncated_is_true():
    r = _rec()
    for i in range(rr.MAX_RETURNED_NAMES + 3):
        r.note_posting("apify", "Acme", f"Name {i}")
    snap = r.snapshot()
    e = snap["companies"][0]["sources"]["apify"]
    assert len(e["returned_names"]) == rr.MAX_RETURNED_NAMES and e["other_names"] == 3 and snap["truncated"] is True
    assert "+3 other names" in rr.render_company(snap, "Acme")


def test_run_report_snapshot_round_trips_through_json_unchanged():
    r = _haworth()
    r.note_outcome("Haworth Inc.", cards=1, digest=2, rejected=3)
    r.note_problem("Apify errored")
    r.set_counts(screened=9, cards=1, junk="x")
    snap = r.snapshot()
    assert json.loads(json.dumps(snap)) == snap and snap["version"] == 1 and snap["counts"] == {"screened": 9, "cards": 1}


def test_run_report_short_message_with_thirty_problems_stays_within_the_caps_and_keeps_build_and_hint():
    r = _rec()
    r.set_counts(screened=40, cards=2, digest=3, below_70=9, boards_scanned=30, boards_total=120)
    r.note_source_call("hirebase", "Acme", status="ok", returned=12, kept=12)
    text = rr.compose_short(r.snapshot(), headline="🏁 <b>Warm scan complete</b>", money_line="Hirebase: 12 jobs",
                            attention=[f"Problem number {i} " + "x" * 120 for i in range(30)])
    lines = text.splitlines()
    assert len(lines) <= 12 and len(text) <= 1400
    assert lines[-1].startswith("Detail: /wrun") and any(ln.startswith("Build aaaaaaa") for ln in lines)
    assert "(+" in text and "more in /wrun)" in text


def test_run_report_short_message_with_no_build_commit_says_build_unknown():
    text = rr.compose_short(_rec(None).snapshot(), headline="🏁 done")
    assert "Build unknown" in text and "()" not in text


def test_run_report_short_message_omits_sources_with_no_company():
    r = _rec()
    r.note_source_call("apify", "Acme", status="ok", returned=5, kept=5)
    text = rr.compose_short(r.snapshot(), headline="h")
    assert "Apify 1 companies, 5 rows" in text and "Hirebase" not in text and "ATS resolver" not in text


def test_run_report_render_company_exact_prefix_ambiguous_and_none():
    r = _rec()
    for name in ("Haworth Inc.", "Hayes Corp", "Hay Group"):
        r.note_source_call("apify", name, status="ok", returned=1, kept=1)
    snap = r.snapshot()
    assert rr.render_company(snap, "hAYES CORP").startswith("<b>Hayes Corp</b>")
    assert rr.render_company(snap, "haw").startswith("<b>Haworth Inc.</b>")
    amb = rr.render_company(snap, "hay")
    assert "Several companies match" in amb and "Hayes Corp" in amb and "Hay Group" in amb
    assert rr.render_company(snap, "<zzz>") == 'No company matching "&lt;zzz&gt;" in run run-1'


def test_run_report_html_special_company_name_is_escaped_in_every_renderer():
    r = _rec()
    r.note_source_call("apify", "<b>X</b> & Co", status="ok", returned=1, kept=1, dropped=[("<i>Y</i>", "no match")])
    snap = r.snapshot()
    for text in (rr.render_company(snap, "<b>X</b> & Co"), "\n".join(rr.render_overview(snap)),
                 rr.compose_short(snap, headline="h")):
        assert "<b>X</b>" not in text and "<i>Y</i>" not in text
    assert "&lt;b&gt;X&lt;/b&gt; &amp; Co" in rr.render_company(snap, "<b>X</b> & Co")


def test_run_report_chunk_splits_nine_thousand_characters_on_line_boundaries():
    text = "\n".join(f"line {i:04d} " + "y" * 80 for i in range(100))
    assert len(text) > 8000
    pieces = rr.chunk(text)
    assert all(len(p) <= 3500 for p in pieces) and len(pieces) >= 3
    assert "\n".join(pieces).split("\n") == text.split("\n")
    assert rr.chunk("z" * 9000)[0].endswith("...") and len(rr.chunk("z" * 9000)[0]) <= 3500
    assert rr.render_full(text) == pieces


def test_run_report_overview_flags_sent_not_returned_only_for_a_real_difference():
    r = _rec()
    r.note_source_call("apify", "Haworth Inc.", status="ok", returned=2, kept=1, dropped=[("Haworth International", "no match")])
    r.note_posting("apify", "Haworth Inc.", "Haworth Inc.")
    r.note_source_call("hirebase", "Same Co", status="ok", returned=1, kept=1)
    r.note_posting("hirebase", "Same Co", "SAME, CO")
    text = "\n".join(rr.render_overview(r.snapshot()))
    assert "sent≠returned: Haworth International" in text
    same_line = [ln for ln in text.splitlines() if ln.startswith("Same Co")][0]
    assert "sent≠returned" not in same_line
    assert text.splitlines()[0].startswith("Run run-1") and "build aaaaaaa" in text.splitlines()[0]


def test_run_report_a_company_with_no_calls_and_no_postings_says_nothing_came_back_not_no_jobs():
    r = _rec()
    r.note_outcome("Quiet Co", cards=0)
    snap = r.snapshot()
    text = rr.render_company(snap, "Quiet Co")
    assert "no source returned anything for this company this run" in text
    assert "no jobs" not in text.lower() and "no jobs" not in "\n".join(rr.render_overview(snap)).lower()


def test_run_report_junk_arguments_never_raise():
    r = rr.RunRecorder(None, None, "not a dict")
    r.note_source_call(None, None, status=None, returned="x", kept=None, dropped=[None, 5, ("a",)], error=None)
    r.note_posting(None, None, None)
    r.note_outcome(None, cards="x")
    r.note_problem(None)
    r.set_counts(a="x", b=None, c=3)
    snap = r.snapshot()
    assert json.dumps(snap) and snap["counts"] == {"c": 3}
    assert rr.compose_short(None, headline=None) and rr.render_company(None, None) and rr.render_overview(None)
    assert rr.chunk(None) == [""]
