import pytest
from warm_company_kind import non_employer_kind, lookup_order


@pytest.mark.parametrize("name,kind", [
    ("Lakeview Christian School", "school"),
    ("Grace Community Church", "church"),
    ("Smith & Jones Law Offices", "law firm"),
    ("Brightpath Cleaning Services", "cleaning"),
])
def test_warm_lookup_queue_kind_caught(name, kind):
    assert non_employer_kind(name) == kind


@pytest.mark.parametrize("name", ["Legal Services Corporation", "LegalZoom", "Walmart", "Ford",
                                  "General Motors", "Rocket Companies"])
def test_warm_lookup_queue_false_positives_stay_employers(name):
    assert non_employer_kind(name) == ""


def test_warm_lookup_queue_university_health_not_skipped():
    assert non_employer_kind("University of Michigan Health") == ""
    assert non_employer_kind("Ohio State University Credit Union") == ""


def test_warm_lookup_queue_law_needs_companion_word():
    assert non_employer_kind("Doe Law Group") == "law firm"
    assert non_employer_kind("Law Street Software") == ""


def test_warm_lookup_queue_biggest_first_regardless_of_arrival():
    rows = [("zeta", 1, 5, ""), ("yankee", 1, 5, ""), ("acme", 14, 5, "")]
    assert lookup_order(rows)[0] == "acme"


def test_warm_lookup_queue_priority_then_key_break_ties():
    rows = [("b", 2, 5, ""), ("a", 2, 5, ""), ("c", 2, 9, "")]
    assert lookup_order(rows) == ["c", "a", "b"]


def test_warm_lookup_queue_bigger_attempted_company_beats_smaller_never_attempted():
    rows = [("old", 30, 9, "2026-01-01"), ("new", 1, 1, ""), ("older", 2, 2, "2025-12-01")]
    assert lookup_order(rows) == ["old", "older", "new"]


def test_warm_lookup_queue_five_plus_group_comes_before_every_smaller_company():
    rows = [("tiny", 4, 99, ""), ("five", 5, 0, "2026-01-01"), ("big", 9, 0, "")]
    assert lookup_order(rows) == ["big", "five", "tiny"]
