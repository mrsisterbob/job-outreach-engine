import pytest

from workday_board import board_triple_from_url as parse


def test_job_url():
    u = "https://borgwarner.wd5.myworkdayjobs.com/BorgWarner_Careers/job/Auburn-Hills/Some-Title_R1234"
    assert parse(u) == "borgwarner/wd5/BorgWarner_Careers"


def test_locale_segment_first():
    u = "https://borgwarner.wd5.myworkdayjobs.com/en-US/BorgWarner_Careers/job/Auburn-Hills/Some-Title_R1234"
    assert parse(u) == "borgwarner/wd5/BorgWarner_Careers"


def test_bare_careers_root():
    assert parse("https://adient.wd3.myworkdayjobs.com/External") == "adient/wd3/External"


def test_query_and_trailing_slash():
    assert parse("https://adient.wd3.myworkdayjobs.com/External/?q=finance&x=1") == "adient/wd3/External"


def test_uppercase_host_keeps_site_case():
    assert parse("https://ADIENT.WD3.MyWorkdayJobs.com/External") == "adient/wd3/External"


@pytest.mark.parametrize("bad", [
    None, "", "   ", 42,
    "https://www.example.com/External",
    "https://adient.myworkdayjobs.com/External",
    "https://myworkdayjobs.com/External",
    "https://wd3.myworkdayjobs.com/External",
    "https://adient.wd3.myworkdayjobs.com/",
    "https://adient.wd3.myworkdayjobs.com",
    "https://adient.wd3.myworkdayjobs.com.evil.com/External",
    "https://adient.wd3.myworkdayjobs.com/en-US",
    "https://adient.wd3.myworkdayjobs.com/en-US/",
])
def test_returns_empty(bad):
    assert parse(bad) == ""


def test_cxs_api_url_read_cleanly():
    assert parse("https://adient.wd3.myworkdayjobs.com/wday/cxs/adient/External/jobs") == "adient/wd3/External"


def test_cxs_api_url_tenant_mismatch_or_truncated():
    assert parse("https://adient.wd3.myworkdayjobs.com/wday/cxs/other/External/jobs") == ""
    assert parse("https://adient.wd3.myworkdayjobs.com/wday/cxs/adient") == ""


def test_site_named_like_a_locale():
    # `/xx-YY/job/...` has no site after the locale-shaped segment, so it is the site.
    assert parse("https://acme.wd1.myworkdayjobs.com/fr-CA/job/Montreal/Analyst_R9") == "acme/wd1/fr-CA"
    # A real locale before a locale-shaped site still resolves to the site.
    assert parse("https://acme.wd1.myworkdayjobs.com/en-US/fr-CA/job/Montreal/Analyst_R9") == "acme/wd1/fr-CA"
