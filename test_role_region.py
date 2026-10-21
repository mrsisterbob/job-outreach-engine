"""Pure tests for role_region.py. Every node name carries `warm_radar_company_summaries` so the task
verify selects them; the real /w entry point is driven in test_main_integration.py."""
import pytest

import role_region as rr

# The distinct location.name values on a live Greenhouse board (posting locations are public),
# measured 2026-10-21, with the verdict the gate must give each. The title column is neutral.
MEASURED_LOCATIONS = [
    ("Germany", "foreign"),
    ("London", "foreign"),
    ("United States", "keep"),
    ("Japan; Tokyo", "foreign"),
    ("Buenos Aires", "foreign"),
    ("Detroit", "keep"),
    ("New York", "keep"),
    ("Milan", "foreign"),
    ("Chicago", "keep"),
    ("Paris", "foreign"),
    ("Dallas", "keep"),
    ("Washington, DC", "keep"),
    ("Munich", "foreign"),
    ("Tokyo", "foreign"),
    ("Sydney", "foreign"),
    ("Dusseldorf; Frankfurt; Munich", "foreign"),
    ("Boston", "keep"),
    ("Switzerland", "foreign"),
    ("Zurich", "foreign"),
    ("China", "foreign"),
    ("Los Angeles", "keep"),
    ("Hong Kong", "foreign"),
    ("Dusseldorf", "foreign"),
    ("Chicago; Dallas; Detroit; New York", "keep"),
    ("Boston; Chicago; Dallas; Detroit; New York", "keep"),
]


def _job(location="", remote=False, title="Operations Analyst"):
    return {"job_city": location, "job_state": "", "job_is_remote": remote, "job_title": title}


@pytest.mark.parametrize("location,expected", MEASURED_LOCATIONS)
def test_warm_radar_company_summaries_measured_board_locations(location, expected):
    verdict, reason = rr.job_region_verdict(_job(location))
    assert verdict == expected, (location, verdict, reason)
    assert reason


def test_warm_radar_company_summaries_us_marker_beats_a_foreign_name():
    for location in ("Paris, TX", "London, KY", "Dublin, OH", "Rome, GA", "Melbourne, FL"):
        assert rr.job_region_verdict(_job(location))[0] == "keep", location
    assert rr.job_region_verdict(_job("Toronto, Canada"))[0] == "foreign"
    assert rr.job_region_verdict(_job("Remote - US"))[0] == "keep"
    assert rr.job_region_verdict(_job("New York, NY, United States"))[0] == "keep"
    assert rr.job_region_verdict(_job("Anywhere in the U.S."))[0] == "keep"


def test_warm_radar_company_summaries_multi_location_needs_every_part_foreign():
    assert rr.job_region_verdict(_job("Berlin; London"))[0] == "foreign"
    assert rr.job_region_verdict(_job("Berlin | London / Paris"))[0] == "foreign"
    assert rr.job_region_verdict(_job("Berlin; New York"))[0] == "keep"
    assert rr.job_region_verdict(_job("London; Remote"))[0] == "keep"
    # a foreign part next to one nobody can read is not clearly foreign
    assert rr.job_region_verdict(_job("Berlin; Springfield"))[0] == "unknown"


def test_warm_radar_company_summaries_remote_with_and_without_a_foreign_place():
    assert rr.job_region_verdict(_job("Remote"))[0] == "keep"
    assert rr.job_region_verdict(_job("Remote - Germany"))[0] == "foreign"
    assert rr.job_region_verdict(_job("Remote, EMEA"))[0] == "foreign"
    assert rr.job_region_verdict(_job("", remote=True))[0] == "keep"


def test_warm_radar_company_summaries_empty_or_unreadable_location_is_unknown_and_kept():
    assert rr.job_region_verdict(_job(""))[0] == "unknown"
    assert rr.job_region_verdict({})[0] == "unknown"
    assert rr.job_region_verdict(_job("Springfield"))[0] == "unknown"
    assert rr.job_region_verdict(_job("   ;  "))[0] == "unknown"
    # New Mexico is not Mexico
    assert rr.job_region_verdict(_job("New Mexico"))[0] != "foreign"


def test_warm_radar_company_summaries_accented_foreign_city_folds():
    assert rr.job_region_verdict(_job("Düsseldorf"))[0] == "foreign"
    assert rr.job_region_verdict(_job("São Paulo"))[0] == "foreign"
    assert rr.job_region_verdict(_job("Zürich"))[0] == "foreign"


@pytest.mark.parametrize("title", [
    "Management Berater:in (m/w/d)",
    "Praktikum im Consulting",
    "Werkstudent:in Marketing & Communications",
    "Business Analyst (w/m/d)",
    "Analyst (m/f/d)",
    "Stagiaire Finance",
    "Responsable Operations",
    "Ingénieur Données Qualité Édition",
])
def test_warm_radar_company_summaries_non_english_title_cues(title):
    assert rr.title_looks_non_english(title) is True, title


@pytest.mark.parametrize("title", [
    "Operations Analyst",
    "Senior Consultant, Restructuring",
    "Account Manager - José Álvarez Team",     # two accents in a name: still English
    "Customer Success Manager, Strategic Accounts",
    "In charge of Logistics Coordination",
    "",
    None,
])
def test_warm_radar_company_summaries_english_titles_stay_english(title):
    assert rr.title_looks_non_english(title) is False, title


def test_warm_radar_company_summaries_title_cue_wins_over_an_unknown_location():
    job = _job("", title="Werkstudent:in Marketing & Communications")
    assert rr.role_region_gate(job)[0] == "non_english"
    # and an English title at a foreign location is foreign, not non-English
    assert rr.role_region_gate(_job("Munich"))[0] == "foreign"
    assert rr.role_region_gate(_job("Detroit"))[0] == "keep"
    assert rr.role_region_gate(_job(""))[0] == "unknown"


def test_warm_radar_company_summaries_rank_keys_are_deterministic():
    keys = [
        rr.role_rank_key(False, "keep", 0),
        rr.role_rank_key(True, "unknown", 5),
        rr.role_rank_key(True, "keep", 7),
        rr.role_rank_key(False, "unknown", 1),
        rr.role_rank_key(False, "keep", 3),
    ]
    assert sorted(keys) == [
        rr.role_rank_key(True, "keep", 7),
        rr.role_rank_key(True, "unknown", 5),
        rr.role_rank_key(False, "keep", 0),
        rr.role_rank_key(False, "keep", 3),
        rr.role_rank_key(False, "unknown", 1),
    ]
    companies = [(5, 2, "Beta"), (9, 1, "Zed"), (5, 4, "Alpha"), (5, 2, "alpha two")]
    ordered = sorted(companies, key=lambda c: rr.company_order_key(*c))
    assert ordered == [(9, 1, "Zed"), (5, 4, "Alpha"), (5, 2, "alpha two"), (5, 2, "Beta")]
    assert rr.company_order_key(None, 1, "x")[0] == 0.0


@pytest.mark.parametrize("location", ["Hamburg, DE", "Mumbai, IN", "Pune, IN", "Bogota, CO", "Toronto, CA",
                                      "Toronto, ON, CA", "Tel Aviv, IL", "Jakarta, ID"])
def test_warm_radar_company_summaries_country_codes_that_are_also_state_codes_read_foreign(location):
    assert rr.job_region_verdict(_job(location))[0] == "foreign", location


@pytest.mark.parametrize("location", ["Denver, CO", "Los Angeles, CA", "Indianapolis, IN", "Wilmington, DE",
                                      "Paris, TX", "London, KY", "Dublin, OH", "Springfield, IL",
                                      "Boston, MA", "Pittsburgh, PA"])
def test_warm_radar_company_summaries_state_codes_after_a_us_place_stay_us(location):
    assert rr.job_region_verdict(_job(location))[0] == "keep", location


@pytest.mark.parametrize("location", ["Paris, Texas", "London, Kentucky", "Dublin, Ohio", "Rome, New York",
                                      "Lima, Ohio", "Paris, Washington", "Berlin, Wisconsin"])
def test_warm_radar_company_summaries_full_state_names_count_as_us(location):
    assert rr.job_region_verdict(_job(location))[0] == "keep", location
    # a country that is not a state is still foreign
    assert rr.job_region_verdict(_job("Paris, France"))[0] == "foreign"
    assert rr.job_region_verdict(_job("Tbilisi, Georgia"))[0] != "keep"


def test_warm_radar_company_summaries_us_eeo_suffix_is_not_a_gender_marker():
    for title in ("Security Officer (M/F/D/V)", "Analyst M/F/D/V", "Analyst (m/f/d/v)", "Clerk (M/F/Disability/Vet)"):
        assert rr.title_looks_non_english(title) is False, title
    for title in ("Analyst (m/w/d)", "Analyst (M/W/D)", "Analyst m/w/d", "Analyst (f/m/d)"):
        assert rr.title_looks_non_english(title) is True, title
