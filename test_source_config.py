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
