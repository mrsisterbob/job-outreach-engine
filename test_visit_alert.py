import inspect

import pytest

import visit_alert as va

T = 1_000_000.0


@pytest.fixture
def gate():
    g = va.VisitAlertGate()
    g.reset()
    return g


def test_visit_alert_first_human_view_admits(gate):
    assert gate.admit("v1", "/", None, T, T) == (True, "")


def test_visit_alert_repeat_inside_window_then_admits_at_600(gate):
    gate.admit("v1", "/", None, T, T)
    assert gate.admit("v1", "/", None, T + 599, T + 599) == (False, "repeat")
    assert gate.admit("v1", "/", None, T + 600, T + 600) == (True, "")


def test_visit_alert_different_visitor_admits(gate):
    gate.admit("v1", "/", None, T, T)
    assert gate.admit("v2", "/", None, T, T)[0] is True


@pytest.mark.parametrize("bot", ["crawler", "self", "preview:LinkedIn", "health"])
def test_visit_alert_bot_is_not_human_and_takes_no_slot(gate, bot):
    assert gate.admit("v1", "/", bot, T, T) == (False, "not_human")
    assert gate.admit("v1", "/", None, T, T) == (True, "")


def test_visit_alert_resume_path_blocked(gate):
    assert gate.admit("v1", "/resume/kevin-miller-resume.pdf", None, T, T) == (False, "resume_path")
    assert gate.admit("v1", "/RESUME/x.pdf", None, T, T) == (False, "resume_path")
    assert va.is_alertable_path("/resumes-page")
    assert va.is_alertable_path("/")
    assert gate.admit("v1", "/", None, T, T)[0] is True


def test_visit_alert_ceiling_then_ages_out(gate):
    for i in range(va.VISIT_ALERT_HOURLY_CEILING):
        assert gate.admit(f"v{i}", "/", None, T + i, T + i)[0] is True
    assert gate.admit("extra", "/", None, T + 20, T + 20) == (False, "ceiling")
    later = T + 3600
    assert gate.admit("extra", "/", None, later, later) == (True, "")


def test_visit_alert_stale_view_does_not_alert(gate):
    assert gate.admit("v1", "/", None, T - 660, T) == (False, "stale")
    assert gate.admit("v1", "/", None, None, T) == (True, "")


def test_visit_alert_table_full_fails_closed_and_new_gate_works(gate):
    for i in range(va.VISIT_ALERT_MAX_TRACKED):
        gate._last[f"x{i}"] = T
    assert gate.admit("new", "/", None, T, T) == (False, "table_full")
    assert va.VisitAlertGate().admit("new", "/", None, T, T) == (True, "")


def test_visit_alert_no_visitor(gate):
    assert gate.admit("", "/", None, T, T) == (False, "no_visitor")


def test_visit_alert_format_carries_path_host_and_escapes():
    out = va.format_visit_alert("3:42 PM", "/job-engine", "linkedin.com")
    assert "/job-engine" in out and "linkedin.com" in out and "3:42 PM" in out
    bad = va.format_visit_alert("t", "/<script>", "<b>x</b>")
    assert "<script>" not in bad and "&lt;script&gt;" in bad
    assert va.format_visit_alert("t", "/", "").count("direct") == 1
    assert len(va.format_visit_alert("t", "/" + "a" * 500, "")) < 250
    assert len(inspect.signature(va.format_visit_alert).parameters) == 3


def test_visit_alert_enabled_env():
    assert va.visit_alerts_enabled({}) is True
    for v in ("0", "false", "no", "off", "OFF"):
        assert va.visit_alerts_enabled({"VISIT_ALERTS_ENABLED": v}) is False
    assert va.visit_alerts_enabled({"VISIT_ALERTS_ENABLED": "1"}) is True
