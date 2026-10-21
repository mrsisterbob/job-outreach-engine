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
