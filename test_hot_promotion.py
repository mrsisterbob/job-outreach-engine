"""Tests for hot_promotion (pure, no Gmail, no CRM)."""
from datetime import date, datetime, timezone

import pytest

import hot_promotion as hp
from pipeline_utils import carmen_reply_anchor

OWN = ("kevin@example.org",)
ATS = ("myworkday.com", "greenhouse.io")


def dq(sender, **kw):
    return hp.sender_disqualifier(sender, kw.pop("own", OWN), kw.pop("ats", ATS), **kw)


def test_sender_address_forms():
    assert hp.sender_address('"Dr Lee" <Prof.Lee+x@Uni.EDU>') == "prof.lee+x@uni.edu"
    assert hp.sender_address("A@B.com") == "a@b.com"
    assert hp.sender_address("no address here") == ""
    assert hp.sender_address(None) == ""


def test_no_address():
    assert dq("undisclosed-recipients") == "no_address"


@pytest.mark.parametrize("sender", [
    "kevin@example.org", "Kevin <kevin@example.org>", "kevin+jobs@example.org",
])
def test_own_address_with_tag_and_display_name(sender):
    assert dq(sender) == "own_address"
    assert dq("kevin@example.org", own=("Kevin M <kevin+tag@example.org>",)) == "own_address"


def test_auto_reply_flag_wins_over_role_and_ats():
    assert dq("careers@x.myworkday.com", auto_reply=True) == "auto_reply"


def test_own_address_beats_auto_reply():
    assert dq("kevin@example.org", auto_reply=True) == "own_address"


def test_role_mailbox_beats_ats_and_automated():
    assert dq("careers@acme.com") == "role_mailbox"
    assert dq("noreply@acme.myworkday.com") == "role_mailbox"


def test_ats_domain_never_qualifies_even_a_person():
    assert dq("jane.doe@acme.myworkday.com") == "ats_domain"
    assert dq("jane.doe@greenhouse.io") == "ats_domain"
    assert dq("jane.doe@notgreenhouse.io") == ""


def test_automated_sender_and_relay_domain():
    assert dq("welcome-team@acme.com") == "automated_sender"
    assert dq("jane@a.b.c.acme.com") == "relay_domain"


def test_consumer_and_company_domains_pass():
    assert dq("Prof Lee <lee@gmail.com>") == ""
    assert dq("Sarah Chen <sarah.chen@sanctuarywealth.com>") == ""


@pytest.mark.parametrize("headers,expected", [
    ({}, False),
    ({"auto-submitted": "auto-replied"}, True),
    ({"auto-submitted": "no"}, False),
    ({"auto-submitted": "No"}, False),
    ({"precedence": "bulk"}, True),
    ({"precedence": "junk"}, True),
    ({"precedence": "auto_reply"}, True),
    ({"precedence": "list"}, False),
    ({"x-autoreply": "yes"}, True),
    ({"x-autorespond": ""}, True),
    (None, False),
])
def test_auto_reply_flag(headers, expected):
    assert hp.auto_reply_flag(headers) is expected


def test_decide_branches():
    d = hp.decide_promotion
    assert d("GENERAL", True, None, "role_mailbox") == ("none", "role_mailbox")
    assert d("INTERVIEW", True, "Carmen Cold") == ("none", "status_event")
    assert d("GENERAL", False, "Carmen Cold") == ("none", "no_kevin_message")
    assert d("GENERAL", True, hp.LOOKUP_FAILED) == ("none", "lookup_failed")
    assert d("GENERAL", True, None) == ("create", "new_person")
    assert d("GENERAL", True, "Carmen Hot") == ("none", "already_hot")
    for tab in hp.PROMOTABLE_SOURCE_TABS:
        assert d("GENERAL", True, tab) == ("move", "two_way_exchange")
    assert d("GENERAL", True, "Killed") == ("none", "archived")
    assert d("GENERAL", True, "Died") == ("none", "archived")
    assert d("GENERAL", True, "Mystery") == ("none", "unknown_tab")


def test_decide_invariants():
    tabs = [None, hp.LOOKUP_FAILED, "Carmen Hot", "Killed", "Died", "Carmen Cold", "x"]
    for tab in tabs:
        if tab == hp.LOOKUP_FAILED:
            assert hp.decide_promotion("GENERAL", True, tab).action != "create"
        if tab == "Carmen Hot":
            assert hp.decide_promotion("GENERAL", True, tab).action != "move"
        for label in ("INTERVIEW", "OFFER", "REJECTION"):
            assert hp.decide_promotion(label, True, tab).action == "none"
    for src in ("Carmen Hot", "Killed", "Died"):
        assert src not in hp.PROMOTABLE_SOURCE_TABS


def test_exchange_date_midday_utc_is_one_date_across_zones(monkeypatch):
    ms = int(datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    fb = date(2000, 1, 1)
    assert hp.exchange_date_from_internal_ms(ms, fb) == datetime.fromtimestamp(ms / 1000).date()
    assert hp.exchange_date_from_internal_ms(str(ms), fb) == hp.exchange_date_from_internal_ms(ms, fb)
    # Local conversion lands on the UTC calendar date for any zone within UTC-11..UTC+11.


def test_exchange_date_fallback():
    fb = date(2026, 1, 2)
    assert hp.exchange_date_from_internal_ms(None, fb) == fb
    assert hp.exchange_date_from_internal_ms("garbage", fb) == fb
    assert hp.exchange_date_from_internal_ms(10 ** 30, fb) == fb


def test_followup_date_boundaries():
    assert hp.hot_followup_date(date(2026, 10, 28)) == "2026-11-04"
    assert hp.hot_followup_date(date(2026, 12, 28)) == "2027-01-04"
    assert hp.hot_followup_date(date(2026, 1, 25)) == "2026-02-01"


def test_note_roundtrips_through_reply_anchor():
    d = date(2026, 10, 28)
    for action, tab, frag in (("move", "Carmen Warm", "moved from Carmen Warm"),
                              ("create", None, "new contact, not previously in the CRM")):
        note = hp.promotion_note(d, action, tab, "Re: hello")
        assert note.startswith("[2026-10-28] Inbound reply received")
        assert frag in note and "2026-11-04" in note
        assert carmen_reply_anchor(note) == d


def test_note_subject_collapsed_and_truncated():
    note = hp.promotion_note(date(2026, 10, 28), "create", None, "a  b\n\tc " + "x" * 300)
    assert "subject: a b c x" in note
    assert note.endswith("x" * (120 - len("a b c ")))
    assert "\n" not in note


def test_hostile_subject_escaped_in_alert():
    out = hp.promotion_alert_html("A <b>&", "a@b.com", "Co & Sons", "move", "Carmen Cold",
                                  date(2026, 10, 28), "<script>alert(1)</script> & more")
    assert "<script" not in out and "&lt;script&gt;" in out and "&amp; more" in out
    assert "A &lt;b&gt;&amp;" in out and "Co &amp; Sons" in out


def test_alert_names_who_why_and_source():
    mv = hp.promotion_alert_html("Jane", "jane@x.com", "", "move", "Carmen Warm",
                                 date(2026, 10, 28), "Hi")
    assert "Jane" in mv and "jane@x.com" in mv and "Carmen Warm" in mv
    assert "they replied and you had sent a message in the thread" in mv
    new = hp.promotion_alert_html("Jane", "jane@x.com", "X", "create", None,
                                  date(2026, 10, 28), "")
    assert "new contact" in new.lower() and "None" not in new


def test_note_hostile_subject_has_no_script_tag_in_alert_only_escaped():
    note = hp.promotion_note(date(2026, 10, 28), "create", None, "<script>x</script>")
    assert carmen_reply_anchor(note) == date(2026, 10, 28)


@pytest.mark.parametrize("sender", ["careers+x@co.com", "info+x@co.com", "careers@co.com"])
def test_plus_tagged_role_mailbox_disqualified(sender):
    assert hp.sender_disqualifier(sender, []) == "role_mailbox"


def test_bare_string_own_and_ats_are_not_iterated_per_character():
    assert hp.sender_disqualifier("kevin@example.org", "kevin@example.org") == "own_address"
    assert hp.sender_disqualifier("jane@acme.myworkday.com", [], "myworkday.com") == "ats_domain"
    assert hp.sender_disqualifier("jane@acme.com", "kevin@example.org", "myworkday.com") == ""
