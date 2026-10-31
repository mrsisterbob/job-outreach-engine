import re
from datetime import datetime, timedelta, timezone

import apify_feed as af


def norm(name):
    company = str(name or "").strip().lower()
    company = re.sub(r'\b(inc|llc|ltd|corp|corporation|co|holdings|plc|group)\b\.?', '', company, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', company).strip()


POSTED = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")


def _row(**kw):
    row = {"id": "123", "title": "Analyst", "organization": "Haworth Inc.", "url": "https://x.example/1",
           "locations_derived": ["Holland, Michigan, United States"], "date_posted": POSTED,
           "description_text": "desc", "organization_url": "https://www.haworth.com/about",
           "source_domain": "haworth.com"}
    row.update(kw)
    return row


def test_apify_belongs_good_row_accepted():
    assert af.row_belongs_to(_row(), "Haworth Inc.", norm)


def test_apify_belongs_sibling_entity_rejected():
    assert not af.row_belongs_to(_row(organization="Haworth International"), "Haworth Inc.", norm)


def test_apify_belongs_workday_null_org_url_matching_tenant_accepted():
    row = _row(organization="Fifth Third Bank", organization_url=None, source_domain="fifththird.wd5.myworkdayjobs.com")
    assert af.row_belongs_to(row, "Fifth Third Bank", norm)


def test_apify_belongs_workday_non_matching_tenant_rejected():
    row = _row(organization="Fifth Third Bank", organization_url=None, source_domain="other.wd5.myworkdayjobs.com")
    assert not af.row_belongs_to(row, "Fifth Third Bank", norm)


def test_apify_belongs_name_matches_domain_does_not_rejected():
    assert not af.row_belongs_to(_row(source_domain="unrelated.com", organization_url="https://unrelated.com"),
                                 "Haworth Inc.", norm)


def test_apify_belongs_no_domain_evidence_rejected():
    assert not af.row_belongs_to(_row(source_domain=None, organization_url=None), "Haworth Inc.", norm)


def test_apify_parse_orlando_only_row_is_not_michigan():
    job = af.parse_row(_row(locations_derived=["Orlando, Florida, United States"]))
    assert job["job_state"] != "MI" and job["job_city"] == "Orlando"


def test_apify_parse_michigan_picked_from_ten_locations():
    locs = [f"City{i}, Ohio, United States" for i in range(9)] + ["Troy, Michigan, United States"]
    job = af.parse_row(_row(locations_derived=locs))
    assert (job["job_city"], job["job_state"], job["job_country"]) == ("Troy", "MI", "US")


def test_apify_parse_shape_and_empty_locations():
    job = af.parse_row(_row(locations_derived=[]))
    assert job["job_id"] == "apify_123" and job["job_state"] == "" and job["job_city"] == ""
    assert job["job_posted_at_datetime_utc"] == POSTED
    assert job["apify_source_domain"] == "haworth.com"


def test_apify_parse_malformed_rows_return_none_and_are_counted():
    assert af.parse_row(_row(id=None)) is None
    assert af.parse_row(_row(url="")) is None
    assert af.parse_row("junk") is None
    kept, stats = af.split_rows([_row(title=None), _row(), _row(organization="Other Co")], ["Haworth Inc."], norm)
    assert stats == {"returned": 3, "malformed": 1, "kept": 1, "unmatched": 1}
    assert len(kept["Haworth Inc."]) == 1


def test_apify_parse_row_goes_only_to_its_own_company():
    kept, stats = af.split_rows([_row()], ["Haworth Inc.", "Steelcase Inc."], norm)
    assert list(kept) == ["Haworth Inc."] and stats["kept"] == 1 and stats["unmatched"] == 0


def test_apify_parse_build_run_input_clamps_and_is_plain():
    inp = af.build_run_input(["Haworth Inc.", "Fifth Third Bank"], 50)
    assert inp["limit"] == 200
    assert af.build_run_input(["A"], 500)["limit"] == 500
    assert inp["organizationSearch"] == ["Haworth Inc.", "Fifth Third Bank"]
    assert not any(":*" in n for n in inp["organizationSearch"])
    assert not any("date" in k.lower() for k in inp)
    assert "CSR/Teller" in inp["titleExclusionSearch"]


def test_apify_parse_duplicate_company_names_do_not_make_rows_unmatched():
    kept, stats = af.split_rows([_row()], ["Haworth Inc.", "Haworth, Inc.", "Haworth Inc."], norm)
    assert stats["kept"] == 1 and stats["unmatched"] == 0 and len(kept["Haworth Inc."]) == 1


def test_apify_belongs_ignores_punctuation_in_names():
    assert af.row_belongs_to(_row(organization="Haworth, Inc."), "Haworth Inc.", norm)
    assert af.row_belongs_to(_row(organization="Haworth Inc"), "Haworth, Inc.", norm)
    assert af.row_belongs_to(_row(organization="H.a.worth"), "Haworth Inc.", norm)


def test_apify_parse_short_location_forms():
    assert (lambda j: (j["job_city"], j["job_state"], j["job_country"]))(
        af.parse_row(_row(locations_derived=["Michigan"]))) == ("", "MI", "")
    assert (lambda j: (j["job_city"], j["job_state"], j["job_country"]))(
        af.parse_row(_row(locations_derived=["MI"]))) == ("", "MI", "")
    assert (lambda j: (j["job_city"], j["job_state"], j["job_country"]))(
        af.parse_row(_row(locations_derived=["Michigan, United States"]))) == ("", "MI", "US")


def test_apify_parse_rejects_bad_url_and_non_scalar_fields():
    assert af.parse_row(_row(url="ftp://x.example/1")) is None
    assert af.parse_row(_row(url="x.example/1")) is None
    assert af.parse_row(_row(url=["https://x.example"])) is None
    assert af.parse_row(_row(id={"a": 1})) is None
    assert af.parse_row(_row(title=["Analyst"])) is None
    assert af.parse_row(_row(organization={"n": "x"})) is None
    assert af.parse_row(_row(id=123)) is not None


TOK = "fake-token-abc"


class _Resp:
    def __init__(self, code=200, body=None, bad_json=False):
        self.status_code, self._body, self._bad = code, body, bad_json

    def json(self):
        if self._bad:
            raise ValueError("secret " + TOK)
        return self._body


class _Post:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


RUN_IN = {"limit": 3}


def test_apify_fetch_no_token_makes_no_call():
    post = _Post(_Resp(body=[]))
    r = af.fetch_feed(RUN_IN, None, post)
    assert r.status == "unconfigured" and post.calls == []
    assert af.get_token({}) is None and af.get_token({"APIFY_TOKEN": "  "}) is None
    assert af.get_token({"APIFY_TOKEN": " x "}) == "x"


def test_apify_fetch_raising_post_is_errored():
    r = af.fetch_feed(RUN_IN, TOK, _Post(exc=RuntimeError("boom " + TOK)))
    assert r.status == "errored" and "RuntimeError" in r.error and TOK not in repr(r)


def test_apify_fetch_500_is_errored():
    r = af.fetch_feed(RUN_IN, TOK, _Post(_Resp(500, body="oops")))
    assert r.status == "errored" and "500" in r.error


def test_apify_fetch_non_list_body_is_errored():
    assert af.fetch_feed(RUN_IN, TOK, _Post(_Resp(body={"error": "x"}))).status == "errored"
    assert af.fetch_feed(RUN_IN, TOK, _Post(_Resp(bad_json=True))).status == "errored"


def test_apify_fetch_all_malformed_rows_is_errored():
    r = af.fetch_feed(RUN_IN, TOK, _Post(_Resp(body=[{"foo": 1}, {"bar": 2}])))
    assert r.status == "errored" and r.rows == []


def test_apify_fetch_empty_list_is_ok_no_rows():
    r = af.fetch_feed(RUN_IN, TOK, _Post(_Resp(body=[])))
    assert r.status == "ok" and r.rows == [] and not r.capped


def test_apify_fetch_exactly_limit_rows_sets_capped():
    rows = [_row(id=str(i)) for i in range(3)]
    r = af.fetch_feed(RUN_IN, TOK, _Post(_Resp(body=rows)))
    assert r.status == "ok" and r.capped
    r2 = af.fetch_feed(RUN_IN, TOK, _Post(_Resp(body=rows[:2])))
    assert r2.status == "ok" and not r2.capped


def test_apify_fetch_token_only_in_authorization_header():
    post = _Post(_Resp(body=[_row()]))
    r = af.fetch_feed(RUN_IN, TOK, post)
    url, kw = post.calls[0]
    assert TOK not in repr(r) and TOK not in url
    assert kw["headers"] == {"Authorization": "Bearer " + TOK}
    assert TOK not in repr(kw["json"])


# ---------------------------------------------------------------------------
# plan_apify_run / actual_cost
NOW = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


def _cands(n, start=0):
    return [("co%02d" % i, "Co %d" % i, 100 - i) for i in range(start, start + n)]


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def test_apify_plan_caps_companies_per_run():
    plan = af.plan_apify_run(_cands(12), [], NOW)
    assert len(plan.picks) == af.APIFY_COMPANIES_PER_RUN == 8
    assert plan.skip_reason == ""


def test_apify_plan_biggest_network_first_ties_by_key():
    plan = af.plan_apify_run([("b", "B", 3), ("a", "A", 3), ("c", "C", 9)], [], NOW)
    assert [k for k, _ in plan.picks] == ["c", "a", "b"]


def test_apify_plan_limit_scales_but_not_below_min():
    assert af.plan_apify_run(_cands(2), [], NOW).limit == 200
    assert af.plan_apify_run(_cands(8), [], NOW).limit == 400
    p = af.plan_apify_run(_cands(8), [], NOW)
    assert abs(p.worst_case_usd - (0.001 + 0.002 * 400)) < 1e-9


def test_apify_plan_ok_row_blocks_for_retry_window():
    ledger = [(_ago(3), 0.5, "ok", ["a"]), (_ago(8), 0.5, "ok", ["b"])]
    plan = af.plan_apify_run([("a", "A", 5), ("b", "B", 4)], ledger, NOW)
    assert [k for k, _ in plan.picks] == ["b"]


def test_apify_plan_errored_row_allows_retry_but_counts_spend():
    ledger = [(_ago(1), 0.30, "errored", ["a"])]
    plan = af.plan_apify_run([("a", "A", 5)], ledger, NOW)
    assert [k for k, _ in plan.picks] == ["a"]
    assert abs(plan.month_spent_usd - 0.30) < 1e-9


def test_apify_plan_month_rollover_ignores_last_month():
    first = NOW.replace(day=1)
    last_month = (first - timedelta(days=2)).isoformat()
    plan = af.plan_apify_run(_cands(1), [(last_month, 3.9, "ok", ["zzz"])], NOW)
    assert plan.month_spent_usd == 0.0
    assert plan.skip_reason == ""


def test_apify_plan_ceiling_too_low_for_min_run():
    ledger = [(_ago(0), 9.80, "ok", ["x"])]  # room 0.20 < 0.401
    plan = af.plan_apify_run(_cands(3), ledger, NOW)
    assert plan.picks == [] and plan.skip_reason == "monthly_ceiling"


def test_apify_plan_shrinks_picks_to_fit_ceiling():
    ledger = [(_ago(0), 9.20, "ok", ["x"])]  # room 0.80: 8 picks (400) cost 0.801, 7 picks (350) cost 0.701
    plan = af.plan_apify_run(_cands(8), ledger, NOW)
    assert len(plan.picks) == 7 and plan.limit == 350
    assert [k for k, _ in plan.picks] == ["co00", "co01", "co02", "co03", "co04", "co05", "co06"]
    assert plan.worst_case_usd <= 0.80 + 1e-9


def test_apify_plan_bad_timestamp_counts_as_now():
    plan = af.plan_apify_run([("a", "A", 5)], [("garbage", 1.0, "ok", ["a"])], NOW)
    assert plan.month_spent_usd == 1.0
    assert plan.skip_reason == "nothing_eligible"


def test_apify_plan_unparseable_usd_spends_whole_ceiling():
    plan = af.plan_apify_run(_cands(2), [(_ago(1), "n/a", "ok", [])], NOW)
    assert plan.skip_reason == "monthly_ceiling"


def test_apify_plan_actual_cost():
    assert abs(af.actual_cost(0) - 0.001) < 1e-9
    assert abs(af.actual_cost(200) - 0.401) < 1e-9


def test_apify_fallthrough_prices_and_ceiling():
    assert af.START_FEE_USD == 0.001 and af.PER_JOB_USD == 0.002
    assert af.APIFY_MONTHLY_CEILING_USD == 10.00


def _ft(searched, registry, cands):
    return af.fallthrough_candidates(cands, searched, registry)


C = [("a", "A", []), ("b", "B", []), ("c", "C", []), ("d", "D", []), ("e", "E", []), ("f", "F", [])]


def test_apify_fallthrough_searched_zero_ok_and_capped():
    s = {"a": ("ok", 0, 0), "b": ("capped", 0, 0)}
    assert _ft(s, {}, C) == [C[0], C[1]]


def test_apify_fallthrough_errored_excluded():
    assert _ft({"a": ("errored", 0, 0)}, {"a": ("", "no_jobs")}, C) == []


def test_apify_fallthrough_returned_but_none_kept_excluded():
    assert _ft({"a": ("ok", 5, 0)}, {}, C) == []


def test_apify_fallthrough_registry_no_jobs_and_unknown_empty_slug():
    r = {"a": ("x", "no_jobs"), "b": ("", "unknown"), "c": ("slug", "unknown"), "d": ("slug", "covered")}
    assert _ft({}, r, C) == [C[0], C[1]]


def test_apify_fallthrough_unsearched_without_registry_row_excluded():
    assert _ft({}, {}, C) == []


def test_apify_fallthrough_order_and_duplicate_key():
    cands = [C[2], C[0], C[2]]
    s = {"a": ("ok", 0, 0), "c": ("ok", 0, 0)}
    assert _ft(s, {}, cands) == [C[2], C[0], C[2]]


def test_apify_fallthrough_domain_board():
    assert af.domain_board("  Acme.MyWorkdayJobs.com ") == "workday"
    assert af.domain_board("boards.greenhouse.io") == ""
    assert af.domain_board(None) == ""
    assert af.domain_board("myworkdayjobs.com") == ""


def test_apify_fallthrough_summarize_sources():
    kept = {"A": [{"apify_source_domain": "z.com"}, {"apify_source_domain": "b.com"},
                  {"apify_source_domain": "x.myworkdayjobs.com"}, {"apify_source_domain": ""}],
            "B": [{"apify_source_domain": "x.myworkdayjobs.com"}, {"apify_source_domain": "x.myworkdayjobs.com"},
                  {"apify_source_domain": "q.com"}],
            "C": [{}]}
    out = af.summarize_feed_sources(kept)
    assert out["A"] == {"source_domain": "b.com", "job_board": "", "rows": 4}
    assert out["B"] == {"source_domain": "x.myworkdayjobs.com", "job_board": "workday", "rows": 3}
    assert out["C"] == {"source_domain": "", "job_board": "", "rows": 1}
    assert af.summarize_feed_sources({}) == {}


def _every_row_is_dropped_or_kept_once(rows, companies):
    dropped = []
    kept, stats = af.split_rows(rows, companies, norm, dropped=dropped)
    assert stats["kept"] + len(dropped) == stats["returned"]
    assert len(dropped) == stats["unmatched"] + stats["malformed"]
    return kept, stats, dropped


def test_apify_dropped_names_the_haworth_case_lists_the_name_that_came_back():
    rows = [_row(), _row(id="2", organization="Haworth International")]
    kept, stats, dropped = _every_row_is_dropped_or_kept_once(rows, ["Haworth Inc."])
    assert [r["employer_name"] for r in kept["Haworth Inc."]] == ["Haworth Inc."]
    assert dropped == [("Haworth International", "no_match")]


def test_apify_dropped_names_a_row_claimed_by_several_companies_says_so():
    # split_rows de-duplicates company names on their normalized form, so two owners can only arise from a
    # normalize that changes between the de-duplication pass and the ownership pass: simulate exactly that.
    calls = {"n": 0}

    def shifting(name):
        calls["n"] += 1
        return f"{name}-x" if calls["n"] <= 2 else "haworth"
    dropped = []
    kept, stats = af.split_rows([_row(organization="Haworth")], ["A1", "A2"], shifting, dropped=dropped)
    assert kept == {} and stats["unmatched"] == 1
    assert dropped == [("Haworth", "claimed_by_several")]


def test_apify_dropped_names_a_malformed_row_gives_an_empty_name():
    _kept, stats, dropped = _every_row_is_dropped_or_kept_once([_row(title=None), "junk", _row()], ["Haworth Inc."])
    assert stats["malformed"] == 2 and dropped == [("", "malformed"), ("", "malformed")]


def test_apify_dropped_names_none_and_empty_list_give_the_same_kept_and_stats():
    rows = [_row(), _row(id="2", organization="Other Co"), _row(title=None)]
    assert af.split_rows(rows, ["Haworth Inc."], norm) == af.split_rows(rows, ["Haworth Inc."], norm, dropped=[])
    assert af.split_rows(rows, ["Haworth Inc."], norm) == af.split_rows(rows, ["Haworth Inc."], norm, dropped=None)


def test_apify_dropped_names_a_non_list_is_ignored_without_raising():
    rows = [_row(id="2", organization="Other Co")]
    for bad in ((), "text", {}, 5):
        kept, stats = af.split_rows(rows, ["Haworth Inc."], norm, dropped=bad)
        assert stats["unmatched"] == 1
    assert bad == 5
