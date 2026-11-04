import math

import pytest

import source_config as sc
from apify_feed import APIFY_COMPANIES_PER_RUN, APIFY_MONTHLY_CEILING_USD
from hirebase_feed import HIREBASE_COMPANIES_PER_RUN, HIREBASE_MONTHLY_JOBS


def test_source_config_absent_keys_are_todays_defaults():
    c = sc.parse_source_config({})
    assert (c.hirebase_enabled, c.hirebase_per_run, c.hirebase_monthly_jobs) == (
        True, HIREBASE_COMPANIES_PER_RUN, HIREBASE_MONTHLY_JOBS)
    assert (c.apify_enabled, c.apify_per_run, c.apify_monthly_usd) == (
        True, APIFY_COMPANIES_PER_RUN, APIFY_MONTHLY_CEILING_USD)
    assert c.problems == ()
    assert c.hirebase_on and c.apify_on


def test_source_config_none_equals_empty():
    assert sc.parse_source_config(None) == sc.parse_source_config({})


@pytest.mark.parametrize("raw", [False, "off", " No ", "FALSE", "0"])
def test_source_config_off_values(raw):
    c = sc.parse_source_config({"source_apify_enabled": raw, "source_hirebase_enabled": raw})
    assert not c.apify_enabled and not c.hirebase_enabled
    assert not c.apify_on and c.problems == ()


def test_source_config_blank_is_unset_not_a_problem():
    c = sc.parse_source_config({"source_apify_enabled": "", "source_apify_monthly_usd": ""})
    assert c.apify_enabled and c.problems == ()


def test_source_config_maybe_keeps_default_and_reports_key():
    c = sc.parse_source_config({"source_hirebase_enabled": "maybe"})
    assert c.hirebase_enabled is True
    assert len(c.problems) == 1 and "source_hirebase_enabled" in c.problems[0]


def test_source_config_over_max_clamped_and_reported():
    c = sc.parse_source_config({
        "source_hirebase_companies_per_run": 99,
        "source_apify_companies_per_run": "50",
        "source_apify_monthly_usd": 999,
        "source_hirebase_monthly_jobs": 5000,
    })
    assert c.hirebase_per_run == 20 and c.apify_per_run == 20
    assert c.apify_monthly_usd == 25.0
    assert c.hirebase_monthly_jobs == 500 == HIREBASE_MONTHLY_JOBS
    assert len(c.problems) == 4
    assert any(p.startswith("source_apify_monthly_usd=999 clamped to 25.0") for p in c.problems)


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "lots", "-3"])
def test_source_config_bad_numbers_default_with_problem(bad):
    c = sc.parse_source_config({"source_apify_monthly_usd": bad, "source_hirebase_companies_per_run": bad})
    assert c.apify_monthly_usd == APIFY_MONTHLY_CEILING_USD
    assert c.hirebase_per_run == HIREBASE_COMPANIES_PER_RUN
    assert len(c.problems) == 2


def test_source_config_zero_budget_reads_as_off():
    c = sc.parse_source_config({"source_apify_monthly_usd": 0, "source_hirebase_monthly_jobs": "0"})
    assert c.apify_enabled and not c.apify_on
    assert not c.hirebase_on and c.problems == ()


def test_source_config_float_per_run_rounds_down():
    c = sc.parse_source_config({"source_hirebase_companies_per_run": 3.9, "source_apify_companies_per_run": "4.7"})
    assert c.hirebase_per_run == 3 and c.apify_per_run == 4
    assert isinstance(c.hirebase_per_run, int)


def test_source_config_never_raises_on_junk():
    c = sc.parse_source_config({"source_apify_enabled": [1], "source_apify_monthly_usd": {"a": 1}})
    assert c is not None and len(c.problems) == 2


def test_source_config_ats_resolver_empty_config_is_off_with_the_default_budget():
    c = sc.parse_source_config({})
    assert (c.resolver_enabled, c.resolver_per_run, c.resolver_monthly_usd) == (False, 3, 2.00)
    assert c.resolver_on is False and c.problems == ()


@pytest.mark.parametrize("raw", ["on", True, "true", 1, "1", " Yes "])
def test_source_config_ats_resolver_on_values_enable_it(raw):
    c = sc.parse_source_config({"source_ats_resolver_enabled": raw})
    assert c.resolver_enabled is True and c.resolver_on is True and c.problems == ()


def test_source_config_ats_resolver_bad_boolean_falls_back_to_off_and_names_the_key():
    c = sc.parse_source_config({"source_ats_resolver_enabled": "maybe"})
    assert c.resolver_enabled is False
    assert any("source_ats_resolver_enabled" in x for x in c.problems), c.problems


def test_source_config_ats_resolver_monthly_above_max_clamps_and_says_so():
    c = sc.parse_source_config({"source_ats_resolver_enabled": True, "source_ats_resolver_monthly_usd": "50"})
    assert c.resolver_monthly_usd == sc.MAX_ATS_RESOLVER_MONTHLY_USD == 10.00
    assert any("source_ats_resolver_monthly_usd" in x and "clamped" in x for x in c.problems), c.problems


def test_source_config_ats_resolver_per_run_clamps_to_the_shared_maximum():
    c = sc.parse_source_config({"source_ats_resolver_companies_per_run": 99})
    assert c.resolver_per_run == sc.MAX_COMPANIES_PER_RUN


@pytest.mark.parametrize("key", ["source_ats_resolver_monthly_usd", "source_ats_resolver_companies_per_run"])
def test_source_config_ats_resolver_zero_budget_or_count_is_off_even_when_enabled(key):
    c = sc.parse_source_config({"source_ats_resolver_enabled": True, key: 0})
    assert c.resolver_enabled is True and c.resolver_on is False


def test_source_config_ats_resolver_keys_leave_the_older_sources_unchanged():
    base = sc.parse_source_config({"source_apify_monthly_usd": 4, "source_hirebase_enabled": "off"})
    both = sc.parse_source_config({"source_apify_monthly_usd": 4, "source_hirebase_enabled": "off",
                                   "source_ats_resolver_enabled": True, "source_ats_resolver_monthly_usd": 5})
    for f in ("hirebase_enabled", "hirebase_per_run", "hirebase_monthly_jobs",
              "apify_enabled", "apify_per_run", "apify_monthly_usd"):
        assert getattr(base, f) == getattr(both, f), f


def test_source_config_apify_plan_empty_config_is_free_with_no_problem():
    c = sc.parse_source_config({})
    assert c.apify_plan == "free" and c.problems == ()
    assert sc.parse_source_config({"source_apify_plan": "  "}).apify_plan == "free"


@pytest.mark.parametrize("raw", ["Starter ", "STARTER", "starter"])
def test_source_config_apify_plan_starter_is_case_and_space_insensitive(raw):
    c = sc.parse_source_config({"source_apify_plan": raw})
    assert c.apify_plan == "starter" and c.problems == ()


def test_source_config_apify_plan_unknown_name_falls_back_to_free_and_names_the_key():
    c = sc.parse_source_config({"source_apify_plan": "enterprise"})
    assert c.apify_plan == "free"
    assert any("source_apify_plan" in x for x in c.problems), c.problems


@pytest.mark.parametrize("raw", [19, True, 1.5])
def test_source_config_apify_plan_a_number_or_bool_falls_back_to_free_with_a_problem(raw):
    c = sc.parse_source_config({"source_apify_plan": raw})
    assert c.apify_plan == "free" and any("source_apify_plan" in x for x in c.problems)


def test_source_config_apify_plan_leaves_the_older_sources_unchanged():
    base = sc.parse_source_config({"source_apify_monthly_usd": 4, "source_hirebase_enabled": "off"})
    both = sc.parse_source_config({"source_apify_monthly_usd": 4, "source_hirebase_enabled": "off",
                                   "source_apify_plan": "starter"})
    for f in ("hirebase_enabled", "hirebase_per_run", "hirebase_monthly_jobs",
              "apify_enabled", "apify_per_run", "apify_monthly_usd", "resolver_enabled"):
        assert getattr(base, f) == getattr(both, f), f


def test_source_config_apify_plan_names_equal_the_apify_feed_pricing_keys():
    import apify_feed
    if not hasattr(apify_feed, "PRICING"):
        pytest.skip("apify_feed.PRICING does not exist yet (TASK-248 is not merged)")
    assert set(sc.APIFY_PLANS) == set(apify_feed.PRICING)
