"""Tests for hirebase_feed.py. Jobs are hand-built with the guessed keys (see module docstring)."""
import re
from datetime import datetime, timedelta, timezone

import hirebase_feed as hf


def norm(name):
    s = re.sub(r"[.,]", "", str(name or "")).strip()
    s = re.sub(r"\s+(inc|llc|corp|co|ltd)$", "", s, flags=re.I)
    return s.strip()


POSTED = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")


def make_job(**over):
    job = {
        "job_title": "Account Manager",
        "application_link": "https://careers.haworth.com/job/1",
        "location_raw": "Holland, MI",
        "locations": [{"city": "Holland", "region": "Michigan", "country": "United States"}],
        "date_posted": POSTED,
        "platform_job_id": "abc123",
        "company_name": "Haworth Inc.",
        "company_slug": "haworth",
        "description": "<p>Sell &amp; grow</p><ul><li>Travel</li></ul>",
    }
    job.update(over)
    return job


def test_hirebase_parse_slug_guesses_fifth_third():
    assert hf.slug_guesses("Fifth Third Bank", norm) == ["fifth-third-bank"]


def test_hirebase_parse_slug_guesses_haworth_inc():
    assert hf.slug_guesses("Haworth Inc.", norm) == ["haworth-inc", "haworth"]


def test_hirebase_parse_slug_guesses_ampersand_and_trailing_period():
    assert hf.slug_guesses("Johnson & Johnson", norm) == ["johnson-johnson"]
    assert hf.slug_guesses("Acme Corp.", norm) == ["acme-corp", "acme"]


def test_hirebase_parse_body_clamps_and_has_no_title_filter():
    body = hf.build_search_body(["haworth"], 999, 5000)
    assert body == {"company_slugs": ["haworth"], "geo_locations": [{"region": "Michigan"}],
                    "days_ago": 30, "page": 1, "limit": 100}
    low = hf.build_search_body(["x"], 0, 0)
    assert low["days_ago"] == 1 and low["limit"] == 1
    assert not any("title" in k or "exclu" in k for k in body)


def test_hirebase_parse_michigan_entry_chosen_over_first():
    locs = [{"city": "Orlando", "region": "Florida", "country": "United States"},
            {"city": "Holland", "region": "Michigan", "country": "United States"}]
    p = hf.parse_job(make_job(locations=locs))
    assert (p["job_city"], p["job_state"], p["job_country"]) == ("Holland", "MI", "US")


def test_hirebase_parse_orlando_only_is_not_michigan():
    locs = [{"city": "Orlando", "region": "Florida", "country": "United States"}]
    p = hf.parse_job(make_job(locations=locs, location_raw="Holland, MI"))
    assert p["job_city"] == "Orlando" and p["job_state"] != "MI"


def test_hirebase_parse_no_locations_gives_empty():
    p = hf.parse_job(make_job(locations=[]))
    assert (p["job_city"], p["job_state"]) == ("", "")


def test_hirebase_parse_html_stripped_and_job_id_carries_slug():
    p = hf.parse_job(make_job())
    assert p["job_description"] == "Sell & grow Travel"
    assert p["job_id"] == "hirebase_haworth_abc123"
    assert p["job_requisition_id"] == "abc123"
    assert p["employer_name"] == "Haworth Inc."
    assert p["job_posted_at_datetime_utc"] == POSTED


def test_hirebase_parse_malformed_counted_not_raised():
    jobs = [make_job(), make_job(application_link="http://x.com/j"), make_job(platform_job_id=None),
            make_job(job_title=""), "junk", None, make_job(company_slug=5)]
    kept, stats = hf.split_jobs(jobs, "Haworth Inc.", ["haworth"], norm)
    assert stats == {"returned": 7, "malformed": 6, "kept": 1, "unmatched": 0}
    assert len(kept) == 1


def test_hirebase_belongs_rejects_sibling_slug():
    sib = make_job(company_slug="haworth-international", company_name="Haworth International")
    assert not hf.job_belongs_to(sib, "Haworth Inc.", ["haworth-inc", "haworth"], norm)
    kept, stats = hf.split_jobs([sib], "Haworth Inc.", ["haworth"], norm)
    assert kept == [] and stats["unmatched"] == 1


def test_hirebase_belongs_rejects_matching_slug_different_name():
    other = make_job(company_name="Haworth International")
    assert not hf.job_belongs_to(other, "Haworth Inc.", ["haworth"], norm)


def test_hirebase_belongs_accepts_exact():
    assert hf.job_belongs_to(make_job(), "Haworth Inc.", ["haworth-inc", "haworth"], norm)


class _Resp:
    def __init__(self, code=200, data=None, raises=False):
        self.status_code, self._data, self._raises = code, data, raises

    def json(self):
        if self._raises:
            raise ValueError("secret body text")
        return self._data


def _post(resp=None, exc=None):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        if exc:
            raise exc
        return resp
    post.calls = calls
    return post


KEY = "sk-SECRET-123"


def test_hirebase_fetch_no_key_makes_no_call():
    post = _post(_Resp(200, {}))
    assert hf.fetch_company_jobs({}, None, post).status == "unconfigured"
    assert post.calls == []
    assert hf.get_key({"HIREBASE_API_KEY": "  "}) is None
    assert hf.get_key({"HIREBASE_API_KEY": " k "}) == "k"


def test_hirebase_fetch_raising_post_is_errored():
    r = hf.fetch_company_jobs({}, KEY, _post(exc=TimeoutError("boom " + KEY)))
    assert r.status == "errored" and r.error == "post raised TimeoutError"


def test_hirebase_fetch_500_and_429_are_errored_with_code():
    for code in (500, 429):
        r = hf.fetch_company_jobs({}, KEY, _post(_Resp(code, {"jobs": [], "total_count": 0})))
        assert r.status == "errored" and str(code) in r.error and r.jobs == []


def test_hirebase_fetch_bad_bodies_are_errored():
    for data in ([], {"total_count": 0}, {"jobs": "x", "total_count": 0}, {"jobs": []},
                 {"jobs": [], "total_count": "3"}):
        assert hf.fetch_company_jobs({}, KEY, _post(_Resp(200, data))).status == "errored"
    assert hf.fetch_company_jobs({}, KEY, _post(_Resp(200, raises=True))).status == "errored"


def test_hirebase_fetch_all_malformed_jobs_are_errored():
    r = hf.fetch_company_jobs({}, KEY, _post(_Resp(200, {"jobs": [{"x": 1}, "y"], "total_count": 2})))
    assert r.status == "errored" and "malformed" in r.error


def test_hirebase_fetch_empty_jobs_is_clean_zero():
    r = hf.fetch_company_jobs({}, KEY, _post(_Resp(200, {"jobs": [], "total_count": 0})))
    assert (r.status, r.jobs, r.capped) == ("ok", [], False)


def test_hirebase_fetch_total_above_returned_is_capped():
    jobs = [make_job(platform_job_id=str(i)) for i in range(5)]
    r = hf.fetch_company_jobs({}, KEY, _post(_Resp(200, {"jobs": jobs, "total_count": 38})))
    assert r.status == "ok" and r.capped and r.total_count == 38 and len(r.jobs) == 5


def test_hirebase_fetch_key_only_in_header():
    post = _post(_Resp(200, {"jobs": [make_job()], "total_count": 1}))
    r = hf.fetch_company_jobs({"q": 1}, KEY, post)
    assert KEY not in repr(r) and KEY not in post.calls[0]["url"]
    assert post.calls[0]["headers"] == {"x-api-key": KEY}
    bad = hf.fetch_company_jobs({}, KEY, _post(_Resp(200, raises=True)))
    assert KEY not in repr(bad) and "secret body" not in repr(bad)


# ---- plan_hirebase_run ----
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago, now=NOW):
    return (now - timedelta(days=days_ago)).isoformat()


def _cands(n):
    return [("co%02d" % i, "Co %d" % i, 100 - i) for i in range(n)]


def test_hirebase_plan_per_run_company_cap():
    plan = hf.plan_hirebase_run(_cands(20), [], NOW)
    assert len(plan.picks) == hf.HIREBASE_COMPANIES_PER_RUN


def test_hirebase_plan_biggest_network_first_ties_by_key():
    c = [("b", "B", 3), ("a", "A", 3), ("z", "Z", 9)]
    plan = hf.plan_hirebase_run(c, [], NOW)
    assert [p[0] for p in plan.picks] == ["z", "a", "b"]


def test_hirebase_plan_limits_never_exceed_usable_left():
    ledgers = [
        [],
        [(_iso(1), 120, "ok", "x")],
        [(_iso(1), 263, "errored", "x"), (_iso(2), 40, "capped", "y")],
        [(_iso(1), 377, "ok", "x")],
        [(_iso(1), "junk", "ok", "x")],
    ]
    for led in ledgers:
        plan = hf.plan_hirebase_run(_cands(30), led, NOW)
        assert sum(p[3] for p in plan.picks) == plan.reserved <= plan.usable_left
        assert plan.month_used + plan.reserved <= hf.HIREBASE_MONTHLY_JOBS - hf.HIREBASE_RESERVE_JOBS or not plan.picks


def test_hirebase_plan_last_pick_shrinks_and_under_minimum_not_made():
    plan = hf.plan_hirebase_run(_cands(10), [(_iso(1), 342, "ok", "x")], NOW)  # 58 left
    assert [p[3] for p in plan.picks] == [25, 25, 8]
    plan = hf.plan_hirebase_run(_cands(10), [(_iso(1), 347, "ok", "x")], NOW)  # 53 left, 3 < min
    assert [p[3] for p in plan.picks] == [25, 25]


def test_hirebase_plan_first_search_14_days_later_is_days_since_plus_one():
    c = [("a", "A", 5), ("b", "B", 4)]
    plan = hf.plan_hirebase_run(c, [(_iso(10), 3, "ok", "a")], NOW)
    assert {p[0]: p[2] for p in plan.picks} == {"a": 11, "b": 14}


def test_hirebase_plan_days_ago_capped_at_30():
    plan = hf.plan_hirebase_run([("a", "A", 5)], [(_iso(60), 3, "ok", "a")], NOW)
    assert plan.picks[0][2] == 30


def test_hirebase_plan_capped_row_keeps_old_watermark():
    led = [(_iso(10), 3, "ok", "a"), (_iso(5), 25, "capped", "a")]
    plan = hf.plan_hirebase_run([("a", "A", 5)], led, NOW)
    assert plan.picks[0][2] == 11


def test_hirebase_plan_recent_ok_skipped_older_not():
    c = [("a", "A", 5), ("b", "B", 4)]
    plan = hf.plan_hirebase_run(c, [(_iso(2), 3, "ok", "a"), (_iso(4), 3, "ok", "b")], NOW)
    assert [p[0] for p in plan.picks] == ["b"]


def test_hirebase_plan_errored_counts_toward_month_but_does_not_block():
    led = [(_iso(1), 10, "errored", "a"), (_iso(1), 5, "capped", "a")]
    plan = hf.plan_hirebase_run([("a", "A", 5)], led, NOW)
    assert plan.month_used == 15
    assert [p[0] for p in plan.picks] == ["a"] and plan.picks[0][2] == 14


def test_hirebase_plan_month_rollover():
    now = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
    plan = hf.plan_hirebase_run(_cands(3), [(_iso(1, now), 390, "ok", "x")], now)  # Oct 31
    assert plan.month_used == 0 and plan.usable_left == 400


def test_hirebase_plan_monthly_cap_when_used_400_or_more():
    for used in (400, 450):
        plan = hf.plan_hirebase_run(_cands(5), [(_iso(1), used, "ok", "x")], NOW)
        assert plan.picks == [] and plan.skip_reason == "monthly_cap" and plan.reserved == 0


def test_hirebase_plan_nothing_eligible():
    plan = hf.plan_hirebase_run([("a", "A", 5)], [(_iso(1), 3, "ok", "a")], NOW)
    assert plan.picks == [] and plan.skip_reason == "nothing_eligible"


def test_hirebase_plan_bad_or_future_timestamp_is_now():
    led = [("garbage", 30, "ok", "a"), (_iso(-5), 30, "errored", "b")]
    plan = hf.plan_hirebase_run([("a", "A", 5), ("b", "B", 4)], led, NOW)
    assert plan.month_used == 60
    assert [p[0] for p in plan.picks] == ["b"]


def test_hirebase_plan_unparseable_jobs_count_as_full_cap():
    plan = hf.plan_hirebase_run(_cands(3), [(_iso(1), "n/a", "errored", "x")], NOW)
    assert plan.month_used == hf.HIREBASE_MONTHLY_JOBS
    assert plan.skip_reason == "monthly_cap" and plan.picks == []


# ---- empty bench (TASK-189) ----
def _picked(ledger, **kw):
    plan = hf.plan_hirebase_run([("acme", "Acme", 5)], ledger, NOW, **kw)
    return [p[0] for p in plan.picks]


def test_hirebase_empty_first_zero_waits_14_days_not_3():
    led = [(_iso(4), 0, "ok", "acme")]
    assert _picked(led) == [] and hf.first_search_was_empty("acme", led, NOW)
    assert _picked([(_iso(13), 0, "ok", "acme")]) == []
    assert _picked([(_iso(15), 0, "ok", "acme")]) == ["acme"]


def test_hirebase_empty_earlier_rows_keep_the_3_day_retry():
    led = [(_iso(20), 7, "ok", "acme"), (_iso(4), 0, "ok", "acme")]
    assert _picked(led) == ["acme"] and not hf.first_search_was_empty("acme", led, NOW)
    led = [(_iso(20), 7, "capped", "acme"), (_iso(4), 0, "ok", "acme")]
    assert _picked(led) == ["acme"] and not hf.first_search_was_empty("acme", led, NOW)


def test_hirebase_empty_errored_row_never_benches_or_counts_as_rows():
    led = [(_iso(4), 25, "errored", "acme")]
    assert _picked(led) == ["acme"] and not hf.first_search_was_empty("acme", led, NOW)
    led = [(_iso(10), 25, "errored", "acme"), (_iso(4), 0, "ok", "acme")]
    assert _picked(led) == [] and hf.first_search_was_empty("acme", led, NOW)


def test_hirebase_empty_unbench_keys_plans_a_first_search():
    led = [(_iso(4), 0, "ok", "acme")]
    plan = hf.plan_hirebase_run([("acme", "Acme", 5)], led, NOW, unbench_keys=frozenset({"acme"}))
    assert [(p[0], p[2]) for p in plan.picks] == [("acme", hf.HIREBASE_FIRST_DAYS)]


def test_hirebase_empty_window_still_runs_from_last_ok_row():
    plan = hf.plan_hirebase_run([("acme", "Acme", 5)], [(_iso(15), 0, "ok", "acme")], NOW)
    assert plan.picks[0][2] == 16


def test_hirebase_empty_helper_agrees_with_planner_for_every_shape():
    shapes = [([(_iso(4), 0, "ok", "acme")], True),
              ([(_iso(20), 3, "ok", "acme"), (_iso(4), 0, "ok", "acme")], False),
              ([(_iso(4), 25, "errored", "acme")], False),
              ([(_iso(4), 0, "capped", "acme")], False), ([], False)]
    for led, benched in shapes:
        assert hf.first_search_was_empty("acme", led, NOW) is benched, led
        assert (_picked(led) == []) is benched, led   # inside 3-14 days only the bench holds it back


def _slugreg_row(name, slug):
    return {"job_title": "Analyst", "company_name": name, "company_slug": slug, "platform_job_id": "1",
            "application_link": "https://x.example/1", "locations": [{"city": "Troy", "region": "Michigan", "country": "United States"}]}


def test_hirebase_slugreg_trusted_slug_passes_on_slug_alone_guessed_keeps_name_rule():
    norm = lambda s: s.lower()   # noqa: E731
    row = _slugreg_row("Trinity Health System", "trinity-health-system")
    kept, _ = hf.split_jobs([row], "Trinity Health", ["trinity-health-system"], norm)
    assert kept == []   # guessed: the strict name rule rejects
    kept, st = hf.split_jobs([row], "Trinity Health", ["trinity-health-system"], norm, trusted_slugs=["trinity-health-system"])
    assert len(kept) == 1 and st["kept"] == 1


def test_hirebase_slugreg_sibling_slug_fails_even_under_a_trusted_slug():
    norm = lambda s: s.lower()   # noqa: E731
    row = _slugreg_row("Haworth International", "haworth-international")
    kept, st = hf.split_jobs([row], "Haworth", ["haworth"], norm, trusted_slugs=["haworth"])
    assert kept == [] and st["unmatched"] == 1


# ---- Company Search (TASK-192) ----
NOW_C = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, code=200, data=None, bad_json=False):
        self.status_code = code
        self._data = data
        self._bad = bad_json
        self.text = "BODYSECRET"

    def json(self):
        if self._bad:
            raise ValueError("BODYSECRET")
        return self._data


def _post_returning(resp):
    return lambda url, **kw: resp


def _row(name, slug, **over):
    r = {"company_name": name, "company_slug": slug, "company_link": "https://www.%s.com/" % slug,
         "aliases": []}
    r.update(over)
    return r


def _ago(days):
    return (NOW_C - timedelta(days=days)).isoformat()


def test_hirebase_company_body_exact_keys_and_clamp():
    b = hf.build_company_search_body("Corewell Health", limit=99)
    assert set(b) == {"company_name", "page", "limit"} and b["page"] == 1 and b["limit"] == 2
    assert hf.build_company_search_body("X", limit=0)["limit"] == 1
    assert hf.build_company_search_body("X", limit=1)["limit"] == 1


def test_hirebase_company_body_blank_is_none():
    assert hf.build_company_search_body("   ") is None
    assert hf.build_company_search_body(None) is None


def test_hirebase_company_fetch_ok_rows_and_drops_non_dicts():
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return FakeResp(200, {"companies": [_row("A", "a"), "junk", 3]})
    res = hf.fetch_company_rows("A", "SECRET-123", post=post)
    assert res.status == "ok" and len(res.rows) == 1
    assert seen["json"] == {"company_name": "A", "page": 1, "limit": 2}
    assert seen["headers"] == {"x-api-key": "SECRET-123"}


def test_hirebase_company_fetch_ok_empty_is_not_error():
    res = hf.fetch_company_rows("A", "k", post=_post_returning(FakeResp(200, {"companies": []})))
    assert res.status == "ok" and res.rows == [] and res.error == ""


def test_hirebase_company_fetch_errors_by_status():
    for code in (401, 422):
        res = hf.fetch_company_rows("A", "k", post=_post_returning(FakeResp(code, {})))
        assert res.status == "errored" and res.error == "http status %d" % code and res.rows == []


def test_hirebase_company_fetch_errored_on_raise_and_bad_json():
    def boom(url, **kw):
        raise RuntimeError("SECRET-123 " + url)
    r1 = hf.fetch_company_rows("A", "SECRET-123", post=boom)
    assert r1.status == "errored" and r1.error == "RuntimeError"
    r2 = hf.fetch_company_rows("A", "k", post=_post_returning(FakeResp(200, bad_json=True)))
    assert r2.status == "errored" and r2.error == "ValueError"


def test_hirebase_company_fetch_errored_on_bad_shapes():
    for data in ({}, {"companies": {"a": 1}}, {"companies": None}, [1]):
        res = hf.fetch_company_rows("A", "k", post=_post_returning(FakeResp(200, data)))
        assert res.status == "errored", data
    slugless = {"companies": [{"company_name": "A"}, {"company_name": "B", "company_slug": ""}]}
    res = hf.fetch_company_rows("A", "k", post=_post_returning(FakeResp(200, slugless)))
    assert res.status == "errored"


def test_hirebase_company_error_text_leaks_nothing():
    def boom(url, **kw):
        raise RuntimeError("SECRET-123 " + url)
    outs = [hf.fetch_company_rows("A", "SECRET-123", post=boom),
            hf.fetch_company_rows("A", "SECRET-123", post=_post_returning(FakeResp(500, {}))),
            hf.fetch_company_rows("A", "SECRET-123", post=_post_returning(FakeResp(200, bad_json=True)))]
    for res in outs:
        assert "SECRET-123" not in res.error and "api.hirebase" not in res.error
        assert "BODYSECRET" not in res.error


def test_hirebase_company_pick_exact_name():
    rows = [_row("Haworth Inc.", "haworth")]
    assert hf.pick_company_row(rows, "Haworth", norm)["company_slug"] == "haworth"


def test_hirebase_company_pick_by_alias():
    rows = [_row("Zeta Holdings", "zeta", aliases=["Acme Corp"])]
    assert hf.pick_company_row(rows, "Acme", norm)["company_slug"] == "zeta"


def test_hirebase_company_pick_by_domain():
    rows = [_row("Different Name", "dn", company_link="https://www.acme.com/about")]
    assert hf.pick_company_row(rows, "Acme", norm) is None
    got = hf.pick_company_row(rows, "Acme", norm, known_domain="http://ACME.com")
    assert got["company_slug"] == "dn"


def test_hirebase_company_pick_corewell_subsidiary_first():
    rows = [_row("Corewell Health Urgent Care", "corewell-health-urgent-care-se"),
            _row("Corewell Health", "corewell-health")]
    assert hf.pick_company_row(rows, "corewell health", lambda s: norm(s).lower())["company_slug"] == "corewell-health"


def test_hirebase_company_pick_ambiguous_and_no_match():
    two = [_row("Acme", "acme-1"), _row("Acme Inc", "acme-2")]
    assert hf.pick_company_row(two, "Acme", norm) is None
    same = [_row("Acme", "acme"), _row("Acme Inc", "acme")]
    assert hf.pick_company_row(same, "Acme", norm)["company_slug"] == "acme"
    assert hf.pick_company_row([_row("Other", "other")], "Acme", norm) is None
    assert hf.pick_company_row([], "Acme", norm) is None


def test_hirebase_company_pick_malformed_aliases_ignored():
    for bad in ("Acme", 5, {"a": "Acme"}, [None, 3], None):
        rows = [_row("Zeta", "zeta", aliases=bad)]
        assert hf.pick_company_row(rows, "Acme", norm) is None


def test_hirebase_company_plan_cap_math():
    ledger = [(_ago(2), 22, "ok", "old")]
    plan = hf.plan_company_lookups([("a", "A", 5), ("b", "B", 4)], ledger, NOW_C)
    assert plan.month_units_used == 22 and plan.usable_left == 2
    assert [p[0] for p in plan.picks] == ["a"] and plan.skip_reason == ""


def test_hirebase_company_plan_errored_counts_and_monthly_cap():
    ledger = [(_ago(2), 12, "ok", "x"), (_ago(3), 12, "errored", "y")]
    plan = hf.plan_company_lookups([("a", "A", 5)], ledger, NOW_C)
    assert plan.month_units_used == 24 and plan.picks == [] and plan.skip_reason == "monthly cap"


def test_hirebase_company_plan_old_rows_leave_window():
    plan = hf.plan_company_lookups([("a", "A", 1)], [(_ago(31), 24, "ok", "x")], NOW_C)
    assert plan.month_units_used == 0 and len(plan.picks) == 1


def test_hirebase_company_plan_retry_skip_includes_errored():
    ledger = [(_ago(1), 2, "errored", "a"), (_ago(8), 2, "ok", "b")]
    plan = hf.plan_company_lookups([("a", "A", 9), ("b", "B", 1)], ledger, NOW_C)
    assert [p[0] for p in plan.picks] == ["b"]
    only = hf.plan_company_lookups([("a", "A", 9)], ledger, NOW_C)
    assert only.picks == [] and only.skip_reason == "all recently tried"


def test_hirebase_company_plan_per_run_and_ordering():
    cands = [("c", "C", 3), ("a", "A", 3), ("z", "Z", 10), ("b", "B", 1)]
    plan = hf.plan_company_lookups(cands, [], NOW_C)
    assert [p[0] for p in plan.picks] == ["z", "a", "c"]
    plan1 = hf.plan_company_lookups(cands, [], NOW_C, per_run=1)
    assert [p[0] for p in plan1.picks] == ["z"]


def test_hirebase_company_plan_skip_reasons():
    assert hf.plan_company_lookups([], [], NOW_C).skip_reason == "no candidates"
    assert hf.plan_company_lookups([("a", "A", 1)], [], NOW_C).skip_reason == ""


# ---- capped continuation (TASK-197) ----
def _cap_plan(led, cands=None, **kw):
    return hf.plan_hirebase_run(cands or [("a", "A", 5)], led, NOW, **kw)


def test_hirebase_capped_page_one_row_plans_page_two_with_pinned_days():
    led = [(_iso(1), 25, "capped", "a", 1, 14)]
    plan = _cap_plan(led)
    assert plan.pages == {"a": 2}
    assert plan.picks[0][:3] == ("a", "A", 14)  # pinned, not recomputed from now


def test_hirebase_capped_page_two_row_ends_the_window():
    led = [(_iso(1), 25, "capped", "a", 1, 14), (_iso(0.5), 25, "capped", "a", 2, 14)]
    plan = _cap_plan(led)
    assert plan.picks == [] and plan.skip_reason == "nothing_eligible"  # retry gap applies
    plan = _cap_plan([(_iso(4), 25, "capped", "a", 2, 14)])
    assert plan.pages == {}  # no page 3
    assert plan.picks[0][2] == 5  # next window starts at the capped row's time (4 days + 1)


def test_hirebase_capped_older_than_seven_days_is_not_a_continuation():
    plan = _cap_plan([(_iso(8), 25, "capped", "a", 1, 14)])
    assert plan.pages == {}
    assert plan.picks[0][2] == hf.HIREBASE_FIRST_DAYS


def test_hirebase_capped_four_tuple_plans_as_today():
    plan = _cap_plan([(_iso(1), 25, "capped", "a")])
    assert plan.pages == {} and plan.picks[0][2] == hf.HIREBASE_FIRST_DAYS


def test_hirebase_capped_continuation_bounded_by_usable_left():
    led = [(_iso(1), 25, "capped", "a", 1, 14)]
    plan = _cap_plan(led, monthly=128, reserve=100)
    assert plan.picks == [] and plan.skip_reason == "monthly_cap"
    plan = _cap_plan(led, monthly=100 + 25 + 30, reserve=100)
    assert plan.picks[0][3] <= plan.usable_left
    plan = _cap_plan(led, monthly=100 + 25 + 25, reserve=100)
    assert plan.picks[0][3] == 25 and plan.pages == {"a": 2}


def test_hirebase_capped_continuation_skipped_when_full_limit_does_not_fit():
    led = [(_iso(1), 25, "capped", "a", 1, 14)]
    plan = _cap_plan(led, monthly=100 + 25 + 8, reserve=100)  # 8 left < 25
    assert plan.picks == [] and plan.pages == {}  # not page 2 at limit 8, not page 1 again


def _rows(n, total, page, limit):
    jobs = [{"id": i} for i in range(n)]
    return _Resp(200, {"jobs": jobs, "total_count": total})


def test_hirebase_capped_fetch_marks_capped_only_when_rows_remain(monkeypatch):
    monkeypatch.setattr(hf, "parse_job", lambda j: {"ok": 1})
    r = hf.fetch_company_jobs({"page": 1, "limit": 25}, KEY, _post(_rows(25, 38, 1, 25)))
    assert r.capped is True
    r = hf.fetch_company_jobs({"page": 2, "limit": 25}, KEY, _post(_rows(13, 38, 2, 25)))
    assert r.capped is False
    r = hf.fetch_company_jobs({"page": 2, "limit": 25}, KEY, _post(_rows(25, 80, 2, 25)))
    assert r.capped is True
    r = hf.fetch_company_jobs({}, KEY, _post(_rows(13, 38, 1, 25)))
    assert r.capped is True  # no usable page: page 1


def test_hirebase_capped_build_search_body_carries_page():
    assert hf.build_search_body(["a"], 7, 25, page=2)["page"] == 2
    assert hf.build_search_body(["a"], 7, 25)["page"] == 1
    for bad in (0, -3, "x", None):
        assert hf.build_search_body(["a"], 7, 25, page=bad)["page"] == 1


def test_hirebase_capped_ok_row_newer_than_capped_is_not_a_continuation():
    led = [(_iso(2), 25, "capped", "a", 1, 14), (_iso(1), 10, "ok", "a")]
    plan = _cap_plan(led)
    assert plan.pages == {} and plan.picks == []  # fresh ok, retry gap


def test_hirebase_capped_total_pages_bounded_over_three_rounds(monkeypatch):
    monkeypatch.setattr(hf, "parse_job", lambda j: {"ok": 1})
    ledger, requests = [], []
    for rnd in range(3):
        now = NOW + timedelta(hours=rnd)
        plan = hf.plan_hirebase_run([("a", "A", 5)], ledger, now)
        for key, _n, days, limit in plan.picks:
            page = plan.pages.get(key, 1)
            body = hf.build_search_body([key], days, limit, page=page)
            n = min(limit, 80 - (page - 1) * limit)
            res = hf.fetch_company_jobs(body, KEY, _post(_rows(n, 80, page, limit)))
            requests.append(page)
            ledger.append((now.isoformat(), n, "capped" if res.capped else "ok", key, page, days))
    assert requests == [1, 2] and len(requests) <= hf.HIREBASE_MAX_PAGES_PER_WINDOW
