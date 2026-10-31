import copy
import json
import pathlib

import pytest

import visit_geo
from visit_geo import Geo, company_org, lookup_url, parse_reply

SAMPLE = json.loads((pathlib.Path(__file__).parent / "visit_geo_sample.json").read_text(encoding="utf-8"))


def sample(**edit):
    d = copy.deepcopy(SAMPLE)
    d.update(edit)
    return d


def test_visit_geo_sample_parses():
    g = parse_reply(200, SAMPLE)
    assert g.place == "San Jose, California"
    assert g.org == ""  # Google LLC is hosting/cloud, not a company


def test_visit_geo_company_path_one_field_edit():
    conn = dict(SAMPLE["connection"], org="Steelcase Inc.", isp="Steelcase Inc.")
    assert parse_reply(200, sample(connection=conn)) == Geo("San Jose, California", "Steelcase Inc.")


def test_visit_geo_blocked_org_does_not_fall_to_isp():
    conn = dict(SAMPLE["connection"], org="Comcast Cable", isp="Steelcase Inc.")
    assert parse_reply(200, sample(connection=conn)).org == ""


def test_visit_geo_missing_org_uses_isp():
    conn = {"isp": "Steelcase Inc."}
    assert parse_reply(200, sample(connection=conn)).org == "Steelcase Inc."


@pytest.mark.parametrize("name", ["Spectrum Health", "Cox Automotive", "Steelcase Inc.", "Oracle"[:0] + "Acme Corp"])
def test_visit_geo_real_employers_survive(name):
    assert company_org(name) == name


@pytest.mark.parametrize("name", ["Comcast Cable Communications", "Amazon.com, Inc.", "Charter Communications Inc",
                                  "Cox Communications Inc.", "Spectrum", "Spectrum Business", "Google LLC",
                                  "Oracle Corporation", "Alibaba Cloud", "Vultr Holdings", "Leaseweb USA",
                                  "M247 Ltd", "Acme Hosting", "Acme Datacenter", "Acme VPN"])
def test_visit_geo_isp_hosting_blank(name):
    assert company_org(name) == ""


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "::1", "", "not an ip", None, "169.254.1.1", "224.0.0.1"])
def test_visit_geo_url_rejects_non_public(ip):
    assert lookup_url(ip) is None


def test_visit_geo_url_public_https_with_ip_only_in_url():
    u = lookup_url("8.8.8.8")
    assert u.startswith("https://") and "8.8.8.8" in u
    assert visit_geo.TIMEOUT_SECONDS <= 3


@pytest.mark.parametrize("status,body", [
    (429, SAMPLE), (500, SAMPLE), (200, [SAMPLE]), (200, {}), (200, None),
    (200, {"success": False, "message": "Invalid IP address"}),
    (200, sample(success=False, message="You've hit the monthly limit")),
    (200, sample(city="", region="")),
])
def test_visit_geo_failures_are_unknown(status, body):
    assert parse_reply(status, body) == Geo("", "")


def test_visit_geo_region_only():
    assert parse_reply(200, sample(city="")).place == "California"


def test_visit_geo_isp_org_is_blank():
    conn = dict(SAMPLE["connection"], org="Comcast Cable Communications", isp="Comcast Cable")
    g = parse_reply(200, sample(connection=conn))
    assert g.org == "" and g.place


def test_visit_geo_company_org():
    assert company_org("Comcast Cable Communications") == ""
    assert company_org("Amazon.com, Inc.") == ""
    assert company_org("Steelcase Inc.") == "Steelcase Inc."
    assert company_org(None) == ""


def test_visit_geo_no_disk_or_logging():
    import ast
    tree = ast.parse(pathlib.Path(visit_geo.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"logging", "requests", "os", "pathlib", "sqlite3", "json"}
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"open", "print"}
