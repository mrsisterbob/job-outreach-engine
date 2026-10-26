"""Unit tests for warm_michigan (pure). The through-run test lives in test_main_integration.py."""
import pytest

import warm_michigan as wm


@pytest.mark.parametrize("role, expected", [
    ({"job_city": "Remote"}, True),
    ({"job_is_remote": True, "job_city": ""}, True),
    ({"job_is_remote": True}, True),
    ({"job_city": "Remote - Michigan"}, True),
    ({"job_is_remote": True, "job_city": "Hybrid - Southfield, MI"}, False),
    ({"job_is_remote": True, "job_city": "Remote / On-site in Troy, MI"}, False),
    ({"job_city": "Troy, MI"}, False),
    ({"job_city": ""}, False),
])
def test_warm_michigan_is_pure_remote(role, expected):
    assert wm.is_pure_remote(role) is expected


@pytest.mark.parametrize("role", [None, {}, "Remote", 5, {"job_city": None}, {"job_city": 7, "job_is_remote": None}])
def test_warm_michigan_is_pure_remote_never_raises(role):
    assert wm.is_pure_remote(role) is False


def test_warm_michigan_reason_words():
    assert wm.reason_words("out_of_state") == "outside Michigan"
    assert wm.reason_words("years_required") == "years_required"
