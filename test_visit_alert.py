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
    assert len(inspect.signature(va.format_visit_alert).parameters) == 6


def test_visit_alert_enabled_env():
    assert va.visit_alerts_enabled({}) is True
    for v in ("0", "false", "no", "off", "OFF"):
        assert va.visit_alerts_enabled({"VISIT_ALERTS_ENABLED": v}) is False
    assert va.visit_alerts_enabled({"VISIT_ALERTS_ENABLED": "1"}) is True


def test_visit_alert_geo_three_args_unchanged():
    out = va.format_visit_alert("3:42 PM", "/job-engine", "linkedin.com")
    assert out == ("👀 <b>Someone opened montelattice.com</b>\n"
                   "<code>/job-engine</code> · via linkedin.com\n3:42 PM")
    assert va.format_visit_alert("t", "/", "", None, "", None) == va.format_visit_alert("t", "/", "")


def test_visit_alert_geo_empty_place_reads_unknown():
    out = va.format_visit_alert("t", "/", "", place="", org="Acme")
    assert "📍 location unknown" in out and "Acme" not in out and "📍 \n" not in out


def test_visit_alert_geo_place_and_org():
    both = va.format_visit_alert("t", "/", "", place="Grand Rapids, Michigan", org="Steelcase Inc.")
    assert "📍 Grand Rapids, Michigan · Steelcase Inc." in both
    solo = va.format_visit_alert("t", "/", "", place="Grand Rapids, Michigan", org="")
    assert "📍 Grand Rapids, Michigan\n" in solo and "·  " not in solo and "Steelcase" not in solo


def test_visit_alert_geo_who_under_title_and_escaped():
    out = va.format_visit_alert("t", "/", "", place="<b>p</b>", org="<b>o</b>", who="<b>w</b>")
    lines = out.split("\n")
    assert lines[1] == "👤 Opened by &lt;b&gt;w&lt;/b&gt;"
    assert "&lt;b&gt;p&lt;/b&gt; · &lt;b&gt;o&lt;/b&gt;" in out and "<b>p" not in out
    assert "Opened by" not in va.format_visit_alert("t", "/", "", who="  ")
    capped = va.format_visit_alert("t", "/", "", place="p" * 500, org="o" * 500, who="w" * 500)
    assert "w" * 81 not in capped and "p" * 81 not in capped and "o" * 81 not in capped


def test_visit_alert_tag_valid_accepts_good_in_any_case_and_rejects_bad():
    assert va.valid_tag("Ab3dEf7h") == "ab3def7h"
    assert va.valid_tag("abcdef") == "abcdef" and va.valid_tag("a" * 12) == "a" * 12
    for bad in ("../x", "<script>", "a" * 200, "abc", None, 12345678, "", "abc def9", "a" * 13):
        assert va.valid_tag(bad) == "", bad


def test_visit_alert_tag_new_is_eight_chars_from_the_allowed_alphabet_and_differs():
    a, b = va.new_tag(), va.new_tag()
    assert len(a) == len(b) == 8 and a != b
    assert not set(a + b) & set("0o1li") and va.valid_tag(a) == a


def test_visit_alert_tag_new_uses_the_given_rng():
    class _R:
        def choice(self, seq):
            return seq[0]
    assert va.new_tag(_R()) == "a" * 8
