import pytest

import inbound_hold as ih


@pytest.mark.parametrize("irt,refs,subj,expected", [
    ("<a@b>", "", "Hello", True),
    ("", "<a@b>", "Hello", True),
    ("", "", "Re: Kevin Miller", True),
    ("", "", "RE: x", True),
    ("", "", "  re: x", True),
    ("", "", "Reminder: your invoice", False),
    ("", "", "Re", False),
    ("", "", "", False),
    (None, None, None, False),
    (5, 6, 7, False),
    ("   ", "  ", "Hello", False),
])
def test_inbound_hold_direct_reply_truth_table(irt, refs, subj, expected):
    assert ih.is_direct_reply(irt, refs, subj) is expected


def _hold(**kw):
    base = dict(is_tier1=False, status_label="", in_reply_to="", references="", subject="Quick question")
    base.update(kw)
    return ih.hold_reason(**base)


def test_inbound_hold_an_unsolicited_mail_is_held():
    assert _hold() == "unsolicited, not a reply"


def test_inbound_hold_a_direct_reply_is_not_held():
    assert _hold(in_reply_to="<x@y>") == "" and _hold(subject="Re: Kevin Miller") == ""


def test_inbound_hold_tier1_is_not_held_even_with_no_reply_headers():
    assert _hold(is_tier1=True) == ""


def test_inbound_hold_a_rejection_is_never_held():
    assert _hold(status_label="REJECTION") == "" and _hold(status_label=" rejection ") == ""


def test_inbound_hold_alert_mode_never_holds():
    assert _hold(mode="alert") == ""


@pytest.mark.parametrize("mode", [None, "", "typo", "HOLD", 3])
def test_inbound_hold_an_unknown_mode_holds(mode):
    assert _hold(mode=mode) == "unsolicited, not a reply"


@pytest.mark.parametrize("raw,expected", [
    (None, "hold"), ("", "hold"), ("  ", "hold"), ("hold", "hold"), ("typo", "hold"), ("ALERT", "alert"),
    (" alerts ", "alert"), ("Permissive", "alert"), (5, "hold")])
def test_inbound_hold_parse_mode_table(raw, expected):
    assert ih.parse_mode(raw) == expected
    assert ih.DEFAULT_MODE == "hold"


def test_inbound_hold_line_for_no_rows_is_empty():
    assert ih.format_held_line([]) == ""


def test_inbound_hold_line_for_one_row_is_singular():
    line = ih.format_held_line([("Dana Reed", "d@x.com", "Quick question")])
    assert line == '📪 1 unsolicited email held, not pinged: Dana Reed - "Quick question"'


def test_inbound_hold_line_for_four_rows_shows_three_and_counts_the_rest():
    rows = [("Dana Reed", "d@x.com", "Quick question"), ("", "a@b.com", "Hello"),
            ("C", "c@x.com", "S3"), ("D", "d@y.com", "S4")]
    line = ih.format_held_line(rows)
    assert line.startswith("📪 4 unsolicited emails held, not pinged: Dana Reed - ")
    assert 'a@b.com - "Hello"' in line and "… (+1 more)" in line and "S4" not in line


def test_inbound_hold_line_cuts_long_subjects_and_names():
    line = ih.format_held_line([("N" * 100, "n@x.com", "S" * 100)])
    assert "N" * 41 not in line and "S" * 41 not in line and "…" in line


def test_inbound_hold_line_escapes_html_in_names_and_subjects():
    line = ih.format_held_line([("<b>Bold</b>", "b@x.com", "<script>x</script>")])
    assert "<b>" not in line and "&lt;b&gt;Bold" in line and "<script>" not in line


def test_inbound_hold_line_flattens_a_newline_in_a_subject():
    line = ih.format_held_line([("A", "a@x.com", "line one\nline two\tend")])
    assert "\n" not in line and "line one line two end" in line


def test_inbound_hold_line_for_an_unreadable_tray_is_not_a_zero():
    line = ih.format_held_line(None)
    assert "unavailable" in line and "0" not in line
