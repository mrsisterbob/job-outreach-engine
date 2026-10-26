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
