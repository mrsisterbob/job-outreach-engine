import json
import os
from datetime import datetime, timedelta, timezone

import hirebase_registry as hr

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
HERE = os.path.dirname(os.path.abspath(__file__))


def _e(slug, status, days):
    return hr.RegistryEntry("k", slug, status, (NOW - timedelta(days=days)).isoformat())


def test_hirebase_registry_no_entry_searches_guesses_with_lookup():
    assert hr.route_company(None, NOW) == ("search_guesses", "", True)


def test_hirebase_registry_covered_searches_slug():
    assert hr.route_company(_e("haworth", "covered", 10), NOW) == ("search_slug", "haworth", False)


def test_hirebase_registry_no_jobs_skips_until_45_days():
    assert hr.route_company(_e("s", "no_jobs", 44), NOW).action == "skip"
    assert hr.route_company(_e("s", "no_jobs", 45), NOW) == ("search_slug", "s", False)


def test_hirebase_registry_expired_no_jobs_still_searches_slug_without_lookup():
    assert hr.route_company(_e("s", "no_jobs", 100), NOW) == ("search_slug", "s", False)


def test_hirebase_registry_old_slug_relooks_up():
    assert hr.route_company(_e("haworth", "covered", 180), NOW) == ("search_guesses", "", True)
    assert hr.route_company(_e("haworth", "covered", 179), NOW).action == "search_slug"


def test_hirebase_registry_unknown_ttl_lookup_flag():
    assert hr.route_company(_e("", "unknown", 29), NOW) == ("search_guesses", "", False)
    assert hr.route_company(_e("", "unknown", 30), NOW) == ("search_guesses", "", True)


def test_hirebase_registry_unrecognized_status_is_unknown():
    assert hr.route_company(_e("", "weird", 5), NOW) == ("search_guesses", "", False)
    assert hr.route_company(_e("", "weird", 40), NOW).needs_lookup is True


def test_hirebase_registry_bad_or_future_timestamp_is_now():
    bad = hr.RegistryEntry("k", "s", "no_jobs", "garbage")
    assert hr.route_company(bad, NOW).action == "skip"
    fut = hr.RegistryEntry("k", "s", "no_jobs", (NOW + timedelta(days=9)).isoformat())
    assert hr.route_company(fut, NOW).action == "skip"


def test_hirebase_registry_seed_file_parses_to_four_entries():
    with open(os.path.join(HERE, "hirebase_slug_seed.json"), encoding="utf-8") as f:
        entries, rejected = hr.parse_seed(f.read(), str.lower)
    assert rejected == 0
    got = {e.key: (e.slug, e.status) for e in entries}
    assert got == {
        "steelcase": ("steelcase", "no_jobs"),
        "trinity health": ("trinity-health-system", "no_jobs"),
        "haworth": ("haworth", "covered"),
        "fifth third bank": ("fifth-third-bank", "covered"),
    }


def test_hirebase_registry_parse_seed_rejects_malformed_keeps_good():
    good = {"name": "A", "slug": "a", "status": "covered", "checked": "2026-10-01"}
    bad = [
        {"name": "B", "slug": "b", "status": "covered"},
        {"name": "C", "slug": "c", "status": "maybe", "checked": "2026-10-01"},
        {"name": "D", "slug": "", "status": "covered", "checked": "2026-10-01"},
        {"name": "E", "slug": "", "status": "no_jobs", "checked": "2026-10-01"},
        {"name": "F", "slug": "f", "status": "covered", "checked": "10/01/2026"},
        "nonsense",
    ]
    entries, rejected = hr.parse_seed(json.dumps([good] + bad), str.lower)
    assert [e.key for e in entries] == ["a"]
    assert rejected == 6
    unk = [{"name": "U", "slug": "", "status": "unknown", "checked": "2026-10-01"}]
    ok, rej = hr.parse_seed(json.dumps(unk), str.lower)
    assert len(ok) == 1 and rej == 0


def test_hirebase_registry_parse_seed_invalid_json():
    assert hr.parse_seed("{not json", str.lower) == ([], 1)


def test_hirebase_registry_seed_has_no_secret_fields():
    with open(os.path.join(HERE, "hirebase_slug_seed.json"), encoding="utf-8") as f:
        data = json.load(f)
    for el in data:
        assert set(el) == {"name", "slug", "status", "checked"}
        for k in el:
            assert not any(w in k.lower() for w in ("api", "key", "token"))
