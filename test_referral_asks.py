"""Pure tests for referral_asks.py. Every node name carries `referral_ask_drafts_stored` so the
task verify selects them; the SQLite write path is driven in test_main_integration.py."""
import json
import os

import pytest

import pipeline_utils as pu
import referral_asks as ra
import resume_engine

HERE = os.path.dirname(os.path.abspath(__file__))


def _role(title, n=1, req="", link=None):
    return ra.build_role(title, link or f"https://boards.greenhouse.io/acme/jobs/{n}", req, f"hash{n}")


def _entries(track):
    return resume_engine.filter_ats_bullet_entries(track)


def _experience():
    return resume_engine.load_evidence_bank().get("experience") or []


def _compose(roles, first="Dana", company="Acme Robotics"):
    return ra.compose_ask(first, company, roles, "Kevin Miller", _entries, _experience())


def _assert_clean(text):
    assert pu.negative_language_hits(text) == [], text


def test_referral_ask_drafts_stored_single_role():
    out = _compose([_role("Supply Chain Analyst", 1, req="R-1042")])
    body = out["body"]
    assert out["left_out"] == [] and len(out["kept"]) == 1
    assert body.startswith("Hi Dana,\n")
    assert "Acme Robotics has an opening that matches my background:" in body
    assert "- Supply Chain Analyst: https://boards.greenhouse.io/acme/jobs/1 (Req ID R-1042)" in body
    assert "Would you be willing to refer me for it?" in body
    assert body.count("?") == 1
    assert body.rstrip().endswith("My resume is attached.\n\nThank you,\nKevin")
    assert "coffee" not in body.lower()
    _assert_clean(body)


def test_referral_ask_drafts_stored_three_roles_one_draft_with_plural_wording():
    roles = [_role("Supply Chain Analyst", 1), _role("Logistics Coordinator", 2), _role("Procurement Analyst", 3)]
    out = _compose(roles)
    body = out["body"]
    assert "Acme Robotics has 3 openings that match my background:" in body
    assert body.count("\n- ") == 3
    assert "refer me for any of them?" in body
    assert body.count("?") == 1
    _assert_clean(body)
    two = _compose(roles[:2])["body"]
    assert "2 openings that match" in two and "refer me for either one?" in two
    _assert_clean(two)


def test_referral_ask_drafts_stored_requisition_id_only_when_valid():
    assert ra.valid_requisition_id("R-1042") == "R-1042"
    assert ra.valid_requisition_id("See Opening ID") == ""
    assert ra.valid_requisition_id("") == ""
    assert ra.valid_requisition_id(None) == ""
    assert ra.valid_requisition_id("  4471 ") == "4471"
    roles = [_role("Supply Chain Analyst", 1, req="See Opening ID"), _role("Logistics Coordinator", 2, req="")]
    body = _compose(roles)["body"]
    assert "Req ID" not in body
    _assert_clean(body)


def test_referral_ask_drafts_stored_flagged_title_is_left_out_others_stay():
    flagged = _role("No-Fault Claims Analyst", 1)
    ok = _role("Supply Chain Analyst", 2)
    assert pu.negative_language_hits("Role: No-Fault Claims Analyst")
    out = _compose([flagged, ok])
    assert [r["title"] for r in out["left_out"]] == ["No-Fault Claims Analyst"]
    assert [r["title"] for r in out["kept"]] == ["Supply Chain Analyst"]
    assert "No-Fault" not in out["body"] and "an opening that matches" in out["body"]
    _assert_clean(out["body"])


def test_referral_ask_drafts_stored_all_roles_flagged_stores_nothing():
    out = _compose([_role("No-Fault Claims Analyst", 1), _role("Cannot Systems Analyst", 2)])
    assert out["body"] is None and out["kept"] == []
    assert len(out["left_out"]) == 2


def test_referral_ask_drafts_stored_flag_in_the_link_leaves_the_role_out():
    out = _compose([_role("Supply Chain Analyst", 1, link="https://x.example/jobs/cannot-start"),
                    _role("Logistics Coordinator", 2)])
    assert [r["title"] for r in out["left_out"]] == ["Supply Chain Analyst"]
    _assert_clean(out["body"])


def test_referral_ask_drafts_stored_no_fit_sentence_when_no_track_fires():
    assert ra.derive_track("Operations Analyst", "Acme Robotics") == ""
    out = _compose([_role("Operations Analyst", 1)])
    body = out["body"]
    assert "example" not in body and "paste into the referral form" not in body
    assert "Would you be willing to refer me for it?" in body  # the ask itself stays
    _assert_clean(body)


def test_referral_ask_drafts_stored_financial_employer_gets_no_title_derived_fit():
    assert ra.derive_track("Supply Chain Analyst", "Acme Robotics") == "g"
    assert ra.derive_track("Supply Chain Analyst", "First Capital Bank") == ""


def test_referral_ask_drafts_stored_fit_bullet_is_verbatim_from_the_real_bank():
    bank = json.load(open(os.path.join(HERE, "resume_bullets_bank.json"), encoding="utf-8"))
    evidence = json.load(open(os.path.join(HERE, "evidence_bank.json"), encoding="utf-8"))
    all_entries = [e for pool in bank.values() if isinstance(pool, list) for e in pool]
    all_text = {e if isinstance(e, str) else e["text"] for e in all_entries}
    for title in ("Supply Chain Analyst", "Logistics Coordinator", "Automation Analyst", "Data Analyst"):
        out = _compose([_role(title, 1)])
        body = out["body"]
        fit = ra.pick_fit(title, "Acme Robotics", _entries, _experience())
        assert fit is not None, title
        assert fit["bullet"] in all_text, (title, fit["bullet"])
        assert fit["bullet"] in body
        assert fit["sentence"] in body
        # The employer named, if any, is the one the bullet's own source_job says.
        track = ra.derive_track(title, "Acme Robotics")
        entry = next(e for e in _entries(track) if ra.clean_inline(ra._bullet_text(e)) == fit["bullet"])
        employer = ra.bullet_employer(entry, evidence["experience"])
        if employer:
            assert employer in {j["company"] for j in evidence["experience"]}
            assert f"From my time at {employer}," in fit["sentence"]
            assert isinstance(entry, dict) and evidence["experience"][entry["source_job"]]["company"] == employer
        else:
            assert "From my time at" not in fit["sentence"]
        _assert_clean(body)


def test_referral_ask_drafts_stored_employer_is_named_only_from_bullet_metadata():
    experience = [{"company": "Signal Advisors"}, {"company": "40 Acres"}]
    assert ra.bullet_employer("Audited accounts.", experience) == ""
    assert ra.bullet_employer({"text": "x"}, experience) == ""
    assert ra.bullet_employer({"text": "x", "source_job": 1}, experience) == "40 Acres"
    assert ra.bullet_employer({"text": "x", "source_job": 5}, experience) == ""
    assert ra.bullet_employer({"text": "x", "source_job": True}, experience) == ""
    assert ra.bullet_employer({"text": "x", "source_job": "1"}, experience) == ""


def test_referral_ask_drafts_stored_endorsement_lowercases_plain_verbs_only():
    assert ra.lowercase_first_word("Audited onboarding paperwork.") == "audited onboarding paperwork."
    assert ra.lowercase_first_word("Built DocuSign workflows.") == "built DocuSign workflows."
    assert ra.lowercase_first_word("Hand-moved 400 files.") == "hand-moved 400 files."
    assert ra.lowercase_first_word("SQL reports ran nightly.") is None
    assert ra.lowercase_first_word("iPhone rollouts.") is None
    assert ra.lowercase_first_word("Salesforce queues were cleared.") is None

    one = {"text": "SQL dashboards for the floor.", "source_job": 0}
    two = "Audited onboarding paperwork for 2,000 accounts."
    fit = ra.pick_fit("Supply Chain Analyst", "Acme", lambda t: [one], [{"company": "Signal Advisors"}])
    assert fit["endorsement"] == "" and fit["sentence"].startswith("From my time at Signal Advisors,")
    fit = ra.pick_fit("Supply Chain Analyst", "Acme", lambda t: [two], [{"company": "Signal Advisors"}])
    assert fit["endorsement"] == ("Kevin Miller audited onboarding paperwork for 2,000 accounts "
                                  "and is a strong fit for the Supply Chain Analyst role.")
    assert fit["sentence"].startswith("Here is one example from my background:")


def test_referral_ask_drafts_stored_bullet_with_negative_wording_is_skipped_for_the_next():
    bad = "Fixed a queue nobody could not clear."
    good = "Cleared the documentation exceptions that stall a transfer."
    assert pu.negative_language_hits(bad)
    fit = ra.pick_fit("Supply Chain Analyst", "Acme", lambda t: [bad, good], [])
    assert fit["bullet"] == good


def test_referral_ask_drafts_stored_merge_roles_never_repeats_a_role():
    a, b, c = _role("Supply Chain Analyst", 1), _role("Logistics Coordinator", 2), _role("Procurement Analyst", 3)
    merged, added = ra.merge_roles([a], [a, b, b, c], taken_hashes={c["job_hash"]})
    assert [r["job_hash"] for r in merged] == ["hash1", "hash2"]
    assert [r["job_hash"] for r in added] == ["hash2"]
    merged, added = ra.merge_roles(None, [a], taken_hashes={"hash1"})
    assert merged == [] and added == []


def test_referral_ask_drafts_stored_greeting_without_a_name_and_quotes_in_titles():
    role = _role('Supply "Chain" Analyst', 1)
    assert '"' not in role["title"]
    body = _compose([role], first="")["body"]
    assert body.startswith("Hi,\n")
    _assert_clean(body)


def test_referral_ask_drafts_stored_placeholder_requisition_ids_are_not_shown():
    for placeholder in ("N/A", "n/a", "NA", "null", "None", "NIL", "TBD", "tba", "Unknown", "-", "--", "0",
                        "undefined", "Undefined", " null "):
        assert ra.valid_requisition_id(placeholder) == "", placeholder
        body = _compose([_role("Supply Chain Analyst", 1, req=placeholder)])["body"]
        assert "Req ID" not in body, placeholder
    assert ra.valid_requisition_id("R-0") == "R-0" and ra.valid_requisition_id("10") == "10"


def test_referral_ask_drafts_stored_role_without_a_link_is_left_out():
    nolink = _role("Supply Chain Analyst", 1, link=" ")
    nolink["link"] = ""
    ok = _role("Logistics Coordinator", 2)
    out = _compose([nolink, ok])
    assert [r["title"] for r in out["left_out"]] == ["Supply Chain Analyst"]
    assert [r["title"] for r in out["kept"]] == ["Logistics Coordinator"]
    assert "Supply Chain Analyst" not in out["body"].split("\n\n")[2]
    assert "- Logistics Coordinator: https://" in out["body"]
    assert _compose([nolink])["body"] is None


def test_referral_ask_drafts_stored_endorsement_uses_the_sender_name_passed_in():
    entries = lambda t: ["Audited onboarding paperwork for 2,000 accounts."]  # noqa: E731
    fit = ra.pick_fit("Supply Chain Analyst", "Acme", entries, [], sender_name="Jordan Avery")
    assert fit["endorsement"].startswith("Jordan Avery audited onboarding")
    assert "Kevin" not in fit["endorsement"]
    out = ra.compose_ask("Dana", "Acme", [_role("Supply Chain Analyst", 1)], "Jordan Avery", entries, [])
    assert '"Jordan Avery audited' in out["body"] and out["body"].rstrip().endswith("Thank you,\nJordan")


def test_referral_ask_drafts_stored_title_is_kept_as_the_board_wrote_it():
    title = "Sales, Marketing, and  Operations Analyst"
    role = ra.build_role(title, "https://x.example/1", "", "h1")
    assert role["title"] == "Sales, Marketing, and Operations Analyst"
    role = ra.build_role("Analyst: Supply Chain; Logistics", "https://x.example/2", "", "h2")
    assert role["title"] == "Analyst: Supply Chain; Logistics"


# ---- TASK-126: the track comes from a public registry function, not from a probe ----

def test_referral_asks_review_followups_track_for_title_is_none_when_no_rule_fires():
    import track_registry as tr
    assert tr.track_for_title("Operations Analyst") is None
    assert tr.track_for_title("") is None and tr.track_for_title(None) is None
    assert tr.track_for_title("Supply Chain Analyst") == "g"
    assert tr.track_for_title("Logistics Coordinator") == "f"
    assert tr.track_for_title("Automation Analyst") == "h"
    assert tr.track_for_title("Data Analyst") == "d"


def test_referral_asks_review_followups_track_for_title_keeps_the_employer_guards():
    import track_registry as tr
    assert tr.track_for_title("Supply Chain Analyst", employer="First Capital Bank") is None
    assert tr.track_for_title("Supply Chain Analyst", tone_mode="conservative") is None
    assert tr.track_for_title("Supply Chain Analyst", employer="Acme Robotics") == "g"


def test_referral_asks_review_followups_override_track_for_title_is_unchanged():
    import track_registry as tr
    cases = [("e", "Supply Chain Analyst", "g"), ("a", "Logistics Coordinator", "f"),
             ("c", "Automation Analyst", "h"), ("b", "Data Analyst", "d"),
             ("e", "Operations Analyst", "e"), ("f", "Supply Chain Analyst", "f"),
             ("h", "Data Analyst", "h"), ("zzz", "Operations Analyst", tr.normalize_track("zzz"))]
    for track, title, want in cases:
        assert tr.override_track_for_title(track, title, employer="Acme Robotics") == want, (track, title)
    assert tr.override_track_for_title("e", "Supply Chain Analyst", employer="First Capital Bank") == "e"
    assert tr.override_track_for_title("e", "Supply Chain Analyst", tone_mode="conservative") == "e"


def test_referral_asks_review_followups_derive_track_does_not_probe_the_override(monkeypatch):
    import track_registry as tr

    def boom(*a, **kw):
        raise AssertionError("derive_track must not call override_track_for_title")

    monkeypatch.setattr(tr, "override_track_for_title", boom)
    assert ra.derive_track("Supply Chain Analyst", "Acme Robotics") == "g"
    assert ra.derive_track("Operations Analyst", "Acme Robotics") == ""
    assert ra.derive_track("Supply Chain Analyst", "First Capital Bank") == ""


def test_referral_asks_review_followups_no_rule_means_no_fit_sentence_and_no_endorsement():
    assert ra.pick_fit("Operations Analyst", "Acme Robotics", _entries, _experience()) is None
    body = _compose([_role("Operations Analyst", 1)])["body"]
    assert "example" not in body and "paste into the referral form" not in body
    assert "Would you be willing to refer me for it?" in body
    fit = ra.pick_fit("Supply Chain Analyst", "Acme Robotics", _entries, _experience())
    assert fit is not None and fit["sentence"] in _compose([_role("Supply Chain Analyst", 1)])["body"]


# ---- TASK-128: the contact pick and the ask size cap (node: warm_contact_pick_and_ask_cap) ----

def _c(name, note="Active relationship", priority=0, email="", uuid="u", company="Acme"):
    return {"name": name, "note": note, "priority_score": priority, "email": email,
            "sheet_uuid": uuid, "raw_company": company}


def _imp(title, joined="2024-05-01", company="Acme", prefix=""):
    return f"{prefix}{title} at {company}. Connected {joined} (LinkedIn import)"


def test_warm_contact_pick_and_ask_cap_note_parsing_with_and_without_the_date_prefix():
    assert ra.contact_title_and_date(_imp("Supply Chain Analyst")) == ("Supply Chain Analyst", "2024-05-01")
    assert ra.contact_title_and_date(_imp("Supply Chain Analyst", prefix="[2026-10-01] ")) == \
        ("Supply Chain Analyst", "2024-05-01")
    # a title that contains " at " keeps it; only the company is cut, from the END
    note = _imp("Analyst at Large Bank", company="Acme")
    assert ra.contact_title_and_date(note) == ("Analyst at Large Bank", "2024-05-01")
    assert ra.contact_title_and_date(note, "Acme") == ("Analyst at Large Bank", "2024-05-01")
    # a period inside the title does not end it
    assert ra.contact_title_and_date(_imp("Sr. Analyst"))[0] == "Sr. Analyst"
    # no title, no date, free text, empty, an impossible date
    assert ra.contact_title_and_date("Works at Acme. Connected 2023-01-02 (LinkedIn import)") == ("", "2023-01-02")
    assert ra.contact_title_and_date("Buyer at Acme (LinkedIn import)") == ("Buyer", "")
    assert ra.contact_title_and_date("Hiring manager") == ("", "")
    assert ra.contact_title_and_date("Active relationship at the gym") == ("", "")
    assert ra.contact_title_and_date(None) == ("", "")
    assert ra.contact_title_and_date(_imp("Buyer", joined="2024-13-45"))[1] == ""


def test_warm_contact_pick_and_ask_cap_equal_priority_goes_to_the_title_that_fits_the_role():
    fin = _c("Fin Person", _imp("Data Analyst"), uuid="u-fin")
    ops = _c("Ops Person", _imp("Supply Chain Analyst"), uuid="u-ops")
    assert ra.derive_track("Supply Chain Analyst", "Acme")
    assert ra.derive_track("Data Analyst", "Acme") != ra.derive_track("Supply Chain Analyst", "Acme")
    assert ra.pick_contact([fin, ops], ["Procurement Analyst"], "Acme")["name"] == "Ops Person"
    assert ra.pick_contact([ops, fin], ["Procurement Analyst"], "Acme")["name"] == "Ops Person"
    assert ra.pick_contact([ops, fin], ["Data Analyst II"], "Acme")["name"] == "Fin Person"
    # no title rule fires on the role, or on any contact: the sheet order stands
    assert ra.pick_contact([fin, ops], ["Zzz Wrangler"], "Acme")["name"] == "Fin Person"
    nothing = [_c("A", "Hiring manager", uuid="u-a"), _c("B", "Active relationship", uuid="u-b")]
    assert ra.pick_contact(nothing, ["Supply Chain Analyst"], "Acme")["name"] == "A"


def test_warm_contact_pick_and_ask_cap_no_fit_falls_back_to_the_most_recent_connection():
    old = _c("Old", _imp("Buyer", "2019-01-01"), uuid="u-old")
    new = _c("New", _imp("Buyer", "2025-06-30"), uuid="u-new")
    undated = _c("Undated", "Hiring manager", uuid="u-und")
    assert ra.pick_contact([undated, old, new], ["Supply Chain Analyst"], "Acme")["name"] == "New"
    assert ra.pick_contact([undated, old, new], [], "Acme")["name"] == "New"
    assert [c["name"] for c in sorted([undated, old, new], key=ra.contact_sort_key)] == ["New", "Old", "Undated"]
    # a fitting title beats recency; recency decides only among the fitting
    fit_old = _c("FitOld", _imp("Supply Chain Analyst", "2018-01-01"), uuid="u-fo")
    fit_new = _c("FitNew", _imp("Procurement Analyst", "2024-01-01"), uuid="u-fn")
    assert ra.pick_contact([new, fit_old, fit_new], ["Supply Chain Analyst"], "Acme")["name"] == "FitNew"


def test_warm_contact_pick_and_ask_cap_a_higher_priority_contact_still_wins():
    boss = _c("Boss", "Hiring manager", priority=8, uuid="u-boss")
    fit = _c("Fit", _imp("Supply Chain Analyst"), priority=5, uuid="u-fit")
    assert ra.pick_contact([fit, boss], ["Procurement Analyst"], "Acme")["name"] == "Boss"
    # at equal priority a fitting title beats an email; an email decides only when no title fits
    mail = _c("Mail", _imp("Data Analyst", "2015-01-01"), priority=5, email="m@acme.com", uuid="u-m")
    assert ra.pick_contact([fit, mail], ["Procurement Analyst"], "Acme")["name"] == "Fit"
    assert ra.pick_contact([fit, mail], ["Zzz Wrangler"], "Acme")["name"] == "Mail"


def test_warm_contact_pick_and_ask_cap_single_contact_and_nothing_at_all():
    only = _c("Only", uuid="u-only")
    assert ra.pick_contact([only], ["Anything"], "Acme") is only
    assert ra.pick_contact([], ["Anything"], "Acme") is None
    assert ra.pick_contact(None, [], "Acme") is None


def test_warm_contact_pick_and_ask_cap_a_contact_without_a_uuid_is_skipped_while_another_has_one():
    no_id = _c("NoId", _imp("Supply Chain Analyst"), priority=9, uuid="")
    has_id = _c("HasId", "Hiring manager", priority=1, uuid="u-has")
    assert ra.pick_contact([no_id, has_id], ["Procurement Analyst"], "Acme")["name"] == "HasId"
    # none has one: still a contact (the caller counts it as "no CRM id"), the usual order applies
    assert ra.pick_contact([no_id, _c("AlsoNo", uuid="")], ["Logistics Coordinator"], "Acme")["name"] == "NoId"


def test_warm_contact_pick_and_ask_cap_placeholder_name_ranks_after_every_real_name():
    ghost = _c("Contact", _imp("Supply Chain Analyst"), priority=9, uuid="u-ghost")
    real = _c("Real Person", "Hiring manager", priority=5, uuid="u-real")
    assert ra.pick_contact([ghost, real], ["Logistics Coordinator"], "Acme")["name"] == "Real Person"
    assert [c["name"] for c in sorted([ghost, real], key=ra.contact_sort_key)] == ["Real Person", "Contact"]
    # a higher priority still wins among real names, and a lone placeholder is not dropped
    higher = _c("Higher", priority=7, uuid="u-h")
    assert ra.pick_contact([ghost, real, higher], [], "Acme")["name"] == "Higher"
    assert ra.pick_contact([ghost], ["Logistics Coordinator"], "Acme")["name"] == "Contact"
    assert ra.is_placeholder_name("") and ra.is_placeholder_name(" contact ") and not ra.is_placeholder_name("Dana")


def test_warm_contact_pick_and_ask_cap_merge_stops_at_the_cap_and_counts_what_did_not_fit():
    roles = [_role(f"Operations Analyst {i}", i) for i in range(1, 57)]
    merged, added, full = ra.merge_roles_counted([], roles, taken_hashes=())
    assert len(merged) == ra.REFERRAL_ASK_MAX_ROLES == 5 and len(added) == 5 and full == 51
    assert [r["job_hash"] for r in merged] == [f"hash{i}" for i in range(1, 6)]
    assert ra.merge_roles([], roles)[1] == added
    # a full ask takes nothing; a role already in the ask or in ANY ask is neither added nor counted as full
    merged2, added2, full2 = ra.merge_roles_counted(merged, roles[:7], taken_hashes={"hash7"})
    assert merged2 == merged and added2 == [] and full2 == 1   # hash6 only
    # an old ask already past the cap is left as it is
    big = [_role("X", i) for i in range(1, 9)]
    assert ra.merge_roles_counted(big, [_role("Y", 20)])[0] == big
    # no cap when asked for none
    assert len(ra.merge_roles([], roles, max_roles=None)[0]) == 56


def test_warm_contact_pick_and_ask_cap_a_maximum_ask_fits_one_telegram_message():
    long_title = ("Senior Associate, Global Supply Chain Operations and Strategic Sourcing Analytics (Hybrid) "
                  "- Midwest Region - Financial Planning Group")
    assert 100 < len(long_title) < 200
    link = "https://boards.greenhouse.io/acme/jobs/1234567890?gh_src=" + "x" * 150 + "&gh_jid=1234567890"
    roles = [_role(long_title + f" {i}", i, req=f"REQ-{i:07d}", link=link + str(i)) for i in range(1, 6)]
    composed = _compose(roles)
    body = composed["body"]
    assert body and len(composed["kept"]) == 5 and composed["left_out"] == []
    assert len(body) < 3900, len(body)
    # the title is cut on the ask line only; the stored role keeps its full title
    assert body.count("...: https") == 5
    assert composed["kept"][0]["title"].startswith(long_title)
    _assert_clean(body)
    # titles of exactly 120 characters are not cut
    near = [_role("T" * 120, i, link=link) for i in range(1, 6)]
    text = ra.render_ask("Dana", "Acme", near, "Kevin Miller")
    assert len(text) < 3900 and "..." not in text


def test_warm_contact_pick_and_ask_cap_absurd_links_leave_the_newest_roles_out_not_one_giant_ask():
    link = "https://example.test/apply?x=" + "q" * 900
    roles = [_role("Operations Analyst", i, link=link + str(i)) for i in range(1, 6)]
    composed = _compose(roles)
    assert composed["body"] and len(composed["body"]) <= ra.REFERRAL_ASK_MAX_CHARS
    assert 1 <= len(composed["kept"]) < 5
    assert [r["job_hash"] for r in composed["kept"]] == [f"hash{i}" for i in range(1, len(composed["kept"]) + 1)]
    assert len(composed["kept"]) + len(composed["too_long"]) == 5 and composed["left_out"] == []


def test_warm_contact_pick_and_ask_cap_56_roles_rendered_through_the_cap_are_one_short_ask():
    roles = [_role(f"Operations Analyst {i}", i) for i in range(1, 57)]
    merged, _added, _full = ra.merge_roles_counted([], roles)
    body = ra.render_ask("Jennifer", "AlixPartners", merged, "Kevin Miller")
    assert body.count("\n- ") == 5 and len(body) < 1500


# ---- TASK-128 review follow-ups ----

def test_warm_contact_pick_and_ask_cap_unusable_roles_take_no_slot_before_the_cap():
    existing = [_role(f"Operations Analyst {i}", i) for i in range(1, 4)]
    no_link = ra.build_role("Buyer", "", "", "hash-nolink")
    flagged = _role("No-Fault Claims Analyst", 20)
    good = _role("Supply Chain Analyst", 21)
    assert not ra.role_usable(no_link) and not ra.role_usable(flagged) and ra.role_usable(good)
    merged, added, full = ra.merge_roles_counted(existing, [no_link, flagged, good])
    assert full == 0 and "hash21" in [r["job_hash"] for r in added]
    # the unusable ones still travel to compose_ask, which drops and reports them; the good one is kept
    out = _compose(merged)
    assert [r["job_hash"] for r in out["kept"]][-1] == "hash21" and len(out["kept"]) == 4
    assert {r["job_hash"] for r in out["left_out"]} == {"hash-nolink", "hash20"} and out["too_long"] == []
    # usable roles still stop at the cap, and only they are counted as full
    merged2, added2, full2 = ra.merge_roles_counted(existing, [no_link, flagged] + [_role(f"Planner {i}", 30 + i) for i in range(4)])
    assert full2 == 2 and len([r for r in merged2 if ra.role_usable(r)]) == 5


def test_warm_contact_pick_and_ask_cap_fit_sentence_follows_the_roles_that_remain_after_a_trim():
    plain = _role("Zzz Wrangler", 1)
    fitting = _role("Supply Chain Analyst", 2, link="https://example.test/apply?x=" + "q" * 3600)
    fit = ra.pick_fit("Supply Chain Analyst", "Acme Robotics", _entries, _experience(), "Kevin Miller")
    assert fit and fit["sentence"]
    assert ra.pick_fit("Zzz Wrangler", "Acme Robotics", _entries, _experience(), "Kevin Miller") is None
    short = _compose([plain, _role("Supply Chain Analyst", 2)])
    assert short["fit_title"] == "Supply Chain Analyst" and fit["sentence"] in short["body"]
    out = _compose([plain, fitting])
    assert [r["job_hash"] for r in out["kept"]] == ["hash1"]
    assert [r["job_hash"] for r in out["too_long"]] == ["hash2"] and out["left_out"] == []
    assert out["fit_title"] == "" and fit["sentence"] not in out["body"] and "Supply Chain" not in out["body"]
    _assert_clean(out["body"])


def test_warm_contact_pick_and_ask_cap_fit_moves_to_the_next_remaining_role_after_a_trim():
    first = _role("Supply Chain Analyst", 1)
    second = _role("Procurement Analyst", 2, link="https://example.test/apply?x=" + "q" * 3600)
    out = _compose([first, second])
    assert [r["job_hash"] for r in out["kept"]] == ["hash1"] and out["fit_title"] == "Supply Chain Analyst"
    swap = _compose([_role("Zzz Wrangler", 3), _role("Procurement Analyst", 4),
                     _role("Logistics Analyst", 5, link="https://example.test/apply?x=" + "q" * 3600)])
    assert [r["job_hash"] for r in swap["kept"]] == ["hash3", "hash4"]
    assert swap["fit_title"] == "Procurement Analyst"


def test_warm_contact_pick_and_ask_cap_date_and_title_survive_text_after_the_import_line():
    note = "Buyer at Acme. Connected 2025-03-01 (LinkedIn import) - met at conference"
    assert ra.contact_title_and_date(note, "Acme") == ("Buyer", "2025-03-01")
    assert ra.contact_title_and_date(note) == ("Buyer", "2025-03-01")
    assert ra.contact_title_and_date("[2026-10-01] " + note, "Acme") == ("Buyer", "2025-03-01")
    # free text after the line that mentions " at " does not change the title
    assert ra.contact_title_and_date(note + " at the booth")[0] == "Buyer"
    # a date with no import marker still sorts; the title still needs the marker
    assert ra.contact_title_and_date("Met at a conference. Connected 2024-02-03") == ("", "2024-02-03")
    a = _c("Older", note.replace("2025-03-01", "2019-03-01"), uuid="u-a")
    b = _c("Newer", note, uuid="u-b")
    assert [c["name"] for c in sorted([a, b], key=ra.contact_sort_key)] == ["Newer", "Older"]
    assert ra.pick_contact([a, b], ["Procurement Analyst"], "Acme")["name"] == "Newer"


def test_alumni_note_title_is_read_from_the_headline():
    note = "[2026-10-23] LinkedIn alumni | employer filter: Gentex | Supply Chain Analyst at Gentex Corporation"
    assert ra.contact_title_and_date(note, "Gentex") == ("Supply Chain Analyst", "")
    bare = "LinkedIn alumni | employer filter: Gentex | Supply Chain Analyst"
    assert ra.contact_title_and_date(bare, "Gentex")[0] == "Supply Chain Analyst"
    assert ra.contact_title_and_date("LinkedIn alumni | employer filter: Gentex", "Gentex")[0] == ""


def test_warm_linkedin_unverified_connection_true_for_import_note_with_date():
    note = "Analyst at Acme. Connected 2024-03-05 (LinkedIn import)"
    assert ra.is_linkedin_connection({"note": note, "raw_company": "Acme"}) is True


def test_warm_linkedin_unverified_connection_false_for_hand_note():
    assert ra.is_linkedin_connection({"note": "Active relationship"}) is False
    assert ra.is_linkedin_connection({}) is False


def test_warm_linkedin_unverified_connection_false_for_malformed_date():
    assert ra.is_linkedin_connection({"note": "Analyst at Acme. Connected 2024-13-40 (LinkedIn import)"}) is False


def test_warm_linkedin_unverified_connection_false_for_alumni_note():
    assert ra.is_linkedin_connection({"note": "LinkedIn alumni | employer filter: Acme | Analyst at Acme"}) is False
