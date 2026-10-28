from datetime import datetime, timezone

from company_source import (COMPANIES_HEADER, build_source_records,
                            covered_contact_share, record_row)
from hirebase_registry import RegistryEntry

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def build(companies, identities=None, registry=None, feed=None, res=None):
    return build_source_records(companies, identities or {}, registry or {}, feed or {}, res or {}, NOW)


def one(**kw):
    return build([("acme", "Acme", 3)], **kw)[0]


def test_company_source_board_wins_over_registry():
    r = one(identities={"acme": {"ats_slug": "acme", "workday_board": ""}},
            registry={"acme": RegistryEntry("acme", "acme-hb", "covered", "2026-10-01")})
    assert (r["covered_by"], r["board_system"], r["board_token"]) == ("board", "ats", "acme")


def test_company_source_workday_board():
    r = one(identities={"acme": {"ats_slug": "x", "workday_board": "a|b|c"}})
    assert (r["board_system"], r["board_token"]) == ("workday", "a|b|c")


def test_company_source_registry_cover_without_board():
    r = one(registry={"acme": RegistryEntry("acme", "acme-hb", "covered", "2026-10-01")})
    assert r["covered_by"] == "hirebase" and r["covered"] is True


def test_company_source_feed_cover_only_with_rows():
    assert one(feed={"acme": ("d.com", "indeed", 4, "2026-10-02")})["covered_by"] == "feed"
    r = one(feed={"acme": ("d.com", "indeed", 0, "2026-10-02")})
    assert r["covered_by"] == "" and r["last_result"] == "indeed:empty"
    assert one(feed={"acme": ("d.com", "", 2, "")})["feed"] == "apify"


def test_company_source_uncovered():
    r = one()
    assert r["covered_by"] == "" and r["covered"] is False and r["last_result"] == "none"


def test_company_source_next_eligible_is_earliest():
    r = one(res={("acme", "apify"): ("error", "2026-10-20", "2026-11-05"),
                 ("acme", "hirebase"): ("empty", "2026-10-25", "2026-11-01"),
                 ("other", "apify"): ("empty", "2026-10-26", "2026-10-27")})
    assert r["next_eligible_at"] == "2026-11-01"
    assert r["last_checked_at"] == "2026-10-25"
    assert r["last_result"] == "hirebase:empty"


def test_company_source_bad_entries_do_not_raise():
    r = one(identities={"acme": None}, registry={"acme": "junk"}, feed={"acme": (1,)},
            res={("acme", "apify"): None, "bad": 3})
    assert r["covered_by"] == ""
    assert build([None, ("k", "K", "x")], identities=5, registry=5, feed=5, res=5)[0]["contacts"] == 0


def test_company_source_share_counts_contacts():
    recs = build([("big", "Big", 40), ("a", "A", 1), ("b", "B", 1)],
                 identities={"big": {"ats_slug": "big", "workday_board": ""}})
    assert covered_contact_share(recs) == (40, 42)


def test_company_source_record_row_matches_header():
    for r in build([("acme", "Acme", 3)]) + [{}]:
        row = record_row(r)
        assert len(row) == len(COMPANIES_HEADER) and None not in row
        assert all(isinstance(c, str) for c in row)
    assert COMPANIES_HEADER[0] == "key"
    assert record_row(one(identities={"acme": {"ats_slug": "a"}}))[-1] == "yes"


def test_company_source_ordering():
    recs = build([("b", "B", 2), ("a", "A", 2), ("z", "Z", 9), ("c", "C", 0)])
    assert [r["key"] for r in recs] == ["z", "a", "b", "c"]
