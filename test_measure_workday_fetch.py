"""Tests for scripts/measure_workday_fetch.py. Fake post callables only; no network."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import measure_workday_fetch as mwf  # noqa: E402


def fake_board(n, dup_at=None, error_at=None, cap=None, total_on_later=None, calls=None):
    """A fake `post`: n postings, serves limit 20 / 1 only, optional repeated posting, error offset."""
    def post(limit, offset, search=""):
        if calls is not None:
            calls.append((limit, offset, search))
        if limit not in (1, 20):
            return {"status": 400, "json": None, "text": "bad limit", "error": ""}
        if error_at is not None and offset >= error_at:
            return {"status": 400, "json": None, "text": "offset too large", "error": ""}
        if cap is not None and offset >= cap:
            return {"status": 200, "json": {"total": n, "jobPostings": []}, "text": "", "error": ""}
        items = [{"externalPath": f"/job/{i}", "title": f"Operations Analyst {i}"} for i in range(n)]
        if dup_at is not None:
            items[dup_at] = dict(items[0])
        page = items[offset:offset + limit]
        total = n if (offset == 0 or total_on_later is None) else total_on_later
        return {"status": 200, "json": {"total": total, "jobPostings": page}, "text": "", "error": ""}
    return post


def test_paging_45_postings_three_pages_no_duplicates():
    r = mwf.page_all(fake_board(45))
    assert (r["pages"], r["collected"], len(r["paths"]), r["duplicates"], r["end"]) == (3, 45, 45, 0, "empty")


def test_repeated_posting_counts_duplicate():
    r = mwf.page_all(fake_board(45, dup_at=30))
    assert r["collected"] == 45 and len(r["paths"]) == 44 and r["duplicates"] == 1


def test_total_zero_on_later_page_recorded_as_zero():
    r = mwf.page_all(fake_board(45, total_on_later=0))
    assert [t for _, t in r["totals"][:3]] == [45, 0, 0]


def test_non_200_midrun_is_error_not_empty():
    r = mwf.page_all(fake_board(100, error_at=40))
    assert r["end"] == "error" and r["stop_offset"] == 40 and r["stop_status"] == 400
    assert r["collected"] == 40


def test_no_answer_is_not_an_empty_page():
    def post(limit, offset, search=""):
        return {"status": None, "json": None, "text": "", "error": "Timeout"}
    assert mwf.page_all(post)["end"] == "no answer"


def test_cap_error_at_2000_reported_with_offset_and_status():
    r = mwf.measure_tenant(fake_board(2100, error_at=2000), terms=())
    assert r["total"] == 2100
    assert r["paging"]["stop_offset"] == 2000 and r["paging"]["stop_status"] == 400
    assert r["cap"][2000]["status"] == 400 and r["cap"][1980]["status"] == 200
    assert r["cap"][2500].startswith("skipped")


def test_cap_probes_only_when_total_reaches_offset():
    r = mwf.measure_tenant(fake_board(1000), terms=())
    assert all(isinstance(v, str) and v.startswith("skipped") for v in r["cap"].values())


def test_limit_probes_record_status_and_text():
    r = mwf.measure_tenant(fake_board(30), terms=())
    assert r["limits"][20]["n"] == 20
    assert r["limits"][21]["status"] == 400 and r["limits"][21]["text"] == "bad limit"


def test_search_local_substring_compared_when_whole_board_collected():
    r = mwf.measure_tenant(fake_board(30), terms=("operations analyst",))
    row = r["search"]["operations analyst"]
    assert row["local_substring"] == 30 and row["paged"] == 30


def test_request_budget_stops_run():
    calls = []
    shared = [5]
    post = mwf.budgeted(fake_board(500, calls=calls), 130, shared)
    r = mwf.measure_tenant(post)
    assert r["budget_hit"] is True and len(calls) == 5 and shared[0] == 0


def test_malformed_board_rejected_before_any_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(mwf.requests, "post", boom)
    assert mwf.main(["--board", "tenant/wd5"]) == 2
    with pytest.raises(ValueError):
        mwf.parse_board("a//c")
