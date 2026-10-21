"""Tests for linkedin_import.py. Synthetic data only: invented names, example.com addresses.

The header used here is a GUESS at LinkedIn's export columns (unverified against a real file).
normalize_company below is copied verbatim from main.normalize_company_for_match (main.py:3212),
so this file never imports main.
"""
import ast
import re
from pathlib import Path

import pytest

import linkedin_import as li

HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"


def normalize_company(company_name):
    # Verbatim copy of main.normalize_company_for_match.
    company = str(company_name or "").strip().lower()
    company = re.sub(r'\b(inc|llc|ltd|corp|corporation|co|holdings|plc|group)\b\.?', '', company, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', company).strip()


def _row(first="Dana", last="Reyes", url="", email="", company="Acme Logistics",
         position="Operations Manager", connected="15 Mar 2023"):
    return {"first_name": first, "last_name": last, "url": url, "email": email,
            "company": company, "position": position, "connected_on": connected}


def test_linkedin_connections_import_plan_parses_bom_preamble_quotes_and_newlines():
    text = (
        "﻿Notes:\n"
        "\"When exporting your connection data, you may notice that some email addresses are missing.\"\n"
        "\n"
        "\n"
        + HEADER + "\n"
        "Dana,Reyes,https://www.linkedin.com/in/dana-reyes-123,dana@example.com,\"Acme, Inc.\",Ops Manager,15 Mar 2023\n"
        "Sam,Ortiz,,,Globex,\"Head of\nOperations\",01 Jan 2022\n"
        "\n"
    )
    rows = li.parse_connections_csv(text)
    assert len(rows) == 2
    assert rows[0]["company"] == "Acme, Inc."
    assert rows[0]["email"] == "dana@example.com"
    assert rows[1]["position"] == "Head of\nOperations"
    assert rows[1]["first_name"] == "Sam"


def test_linkedin_connections_import_plan_maps_reordered_columns_and_ignores_extras():
    text = (
        "Company,Extra Thing,Last Name,First Name,Position,Profile URL,Email,Connected On\n"
        "Globex,zzz,Ortiz,Sam,Analyst,linkedin.com/in/sam-ortiz,sam@example.com,2022-01-01\n"
    )
    rows = li.parse_connections_csv(text)
    assert rows == [{
        "company": "Globex", "last_name": "Ortiz", "first_name": "Sam", "position": "Analyst",
        "url": "linkedin.com/in/sam-ortiz", "email": "sam@example.com", "connected_on": "2022-01-01",
    }]
    assert li.clean_connection(rows[0])["url"] == "https://www.linkedin.com/in/sam-ortiz"


def test_linkedin_connections_import_plan_missing_last_name_raises_listing_columns():
    text = "First Name,URL,Company,Position\nSam,,Globex,Analyst\n"
    with pytest.raises(li.ConnectionsFormatError) as exc:
        li.parse_connections_csv(text)
    msg = str(exc.value)
    assert "First Name" in msg and "URL" in msg and "Company" in msg and "Position" in msg


def test_linkedin_connections_import_plan_cleaning_whitespace_url_and_dates():
    clean = li.clean_connection(_row(
        first="  Dana ", last="Reyes\n", company="  Acme   Logistics ", position=" Ops\t Manager ",
        url="https://www.linkedin.com/in/Dana-Reyes-123/?trk=x", email=" Dana@Example.COM ",
        connected="15 Mar 2023"))
    assert clean["name"] == "Dana Reyes"
    assert clean["company"] == "Acme Logistics"
    assert clean["title"] == "Ops Manager"
    assert clean["url"] == "https://www.linkedin.com/in/dana-reyes-123"
    assert clean["email"] == "dana@example.com"
    assert clean["connected_on"] == "2023-03-15"
    assert li.clean_connection(_row(connected="sometime last spring"))["connected_on"] == ""
    assert li.clean_connection(_row(connected=""))["connected_on"] == ""
    assert li.canonical_linkedin_url("linkedin.com/in/Dana-Reyes-123#top") == "https://www.linkedin.com/in/dana-reyes-123"
    assert li.canonical_linkedin_url("https://example.com/in/dana") == ""
    assert li.clean_connection(_row(email="not an email"))["email"] == ""


def test_linkedin_connections_import_plan_company_reasons():
    rows = [
        _row(first="A", company=""),
        _row(first="B", company="Self-employed"),
        _row(first="C", company="freelance"),
        _row(first="D", company="Freelance Designer"),
        _row(first="E", company="Stealth"),
        _row(first="F", company="Initech"),
        _row(first="", last="", company="Initech"),
    ]
    plan = li.plan_import(rows, [], normalize_company)
    reasons = {s["name"]: s["reason"] for s in plan.skipped}
    assert reasons["A Reyes"] == li.NO_COMPANY
    assert reasons["B Reyes"] == li.NOT_A_COMPANY
    assert reasons["C Reyes"] == li.NOT_A_COMPANY
    assert reasons["D Reyes"] == li.NOT_A_COMPANY
    assert reasons["E Reyes"] == li.NOT_A_COMPANY
    assert reasons[""] == li.NO_NAME
    assert [a["name"] for a in plan.add] == ["F Reyes"]


def test_linkedin_connections_import_plan_dedupes_in_file_and_against_sheet():
    existing = [
        {"name": "Pat Lane", "company": "Intact Services USA", "email": "", "source_link": "", "sheet_uuid": "u1"},
        {"name": "Kim Voss", "company": "Globex", "email": "kim@example.com", "source_link": "", "sheet_uuid": "u2"},
        {"name": "Lee Park", "company": "Initech", "email": "", "source_link": "https://linkedin.com/in/Lee-Park/", "sheet_uuid": "u3"},
    ]
    rows = [
        # against the sheet: name + normalized company (LLC suffix variant), email, URL
        _row(first="Pat", last="Lane", company="Intact Services USA LLC"),
        _row(first="Kimberly", last="Voss", company="Other Co Labs", email="KIM@example.com"),
        _row(first="Leland", last="Park", company="Elsewhere Labs", url="https://www.linkedin.com/in/lee-park?x=1"),
        # within the file: first passes, the repeats are duplicates by each key
        _row(first="Ana", last="Cruz", company="Umbrella Works", url="https://www.linkedin.com/in/ana-cruz", email="ana@example.com"),
        _row(first="Ana", last="C", company="Umbrella Works", url="https://www.linkedin.com/in/ANA-CRUZ/"),
        _row(first="Anna", last="C", company="Umbrella Works", email="ana@example.com"),
        _row(first="Ana", last="Cruz", company="Umbrella Works Inc."),
        # one row matching all three keys at once is counted once, under the URL key
        _row(first="Ana", last="Cruz", company="Umbrella Works", url="https://www.linkedin.com/in/ana-cruz", email="ana@example.com"),
    ]
    plan = li.plan_import(rows, existing, normalize_company)
    got = [s["reason"] for s in plan.skipped]
    assert got == [
        li.ALREADY_IN_SHEET_NAME_COMPANY, li.ALREADY_IN_SHEET_EMAIL, li.ALREADY_IN_SHEET_URL,
        li.DUPLICATE_IN_FILE, li.DUPLICATE_IN_FILE, li.DUPLICATE_IN_FILE, li.DUPLICATE_IN_FILE,
    ]
    assert len(plan.add) == 1 and plan.add[0]["name"] == "Ana Cruz"
    assert plan.summary["total"] == 8 and plan.summary["by_reason"][li.DUPLICATE_IN_FILE] == 4

    triple = li.plan_import(
        [_row(first="Ana", last="Cruz", company="Umbrella Works", url="https://www.linkedin.com/in/ana-cruz", email="ana@example.com")],
        [{"name": "Ana Cruz", "company": "Umbrella Works Inc", "email": "ana@example.com",
          "source_link": "https://www.linkedin.com/in/ana-cruz", "sheet_uuid": "u9"}],
        normalize_company)
    assert [s["reason"] for s in triple.skipped] == [li.ALREADY_IN_SHEET_URL]
    assert triple.summary["skipped"] == 1 and sum(triple.summary["by_reason"].values()) == 1


def test_linkedin_connections_import_plan_rows_with_email_are_listed_for_check_and_still_added():
    rows = [
        _row(first="Ana", last="Cruz", company="Umbrella Works", email="Ana@Example.com"),
        _row(first="Bo", last="Diaz", company="Hooli"),
    ]
    plan = li.plan_import(rows, [], normalize_company)
    assert [a["name"] for a in plan.add] == ["Ana Cruz", "Bo Diaz"]
    assert plan.emails_to_check == [{
        "email": "ana@example.com", "name": "Ana Cruz", "company": "Umbrella Works",
        "needs_email_check": True,
    }]
    assert plan.add[0]["needs_email_check"] is True
    assert "needs_email_check" not in plan.add[1]
    assert plan.summary["emails_to_check"] == 1


def test_linkedin_connections_import_plan_summary_counts_add_up_for_mixed_file():
    existing = [{"name": "Pat Lane", "company": "Intact Services USA", "email": "", "source_link": "", "sheet_uuid": "u1"}]
    text = HEADER + "\n" + "\n".join([
        "Pat,Lane,,,Intact Services USA LLC,Buyer,01 Feb 2021",
        "Ana,Cruz,https://www.linkedin.com/in/ana-cruz,ana@example.com,Umbrella Works,Planner,02 Feb 2021",
        "Ana,Cruz,https://www.linkedin.com/in/ana-cruz,,Umbrella Works,Planner,02 Feb 2021",
        "Bo,Diaz,,,Hooli,Analyst,03 Feb 2021",
        "Cy,Eng,,,,Analyst,03 Feb 2021",
        "Di,Fox,,,Self-employed,Owner,03 Feb 2021",
        ",,,,Initech,Analyst,03 Feb 2021",
        "Ed,Gray,,ed@example.com,Globex,Lead,bad date",
        "Flo,Hart,,,Soylent,Lead,05 Feb 2021",
        "Gus,Ivy,,,freelance,Lead,05 Feb 2021",
        "Hal,Joy,,,Vandelay Industries,Lead,05 Feb 2021",
        "Ivy,Kerr,,,Wayne Enterprises,Lead,05 Feb 2021",
    ]) + "\n"
    rows = li.parse_connections_csv(text)
    assert len(rows) == 12
    plan = li.plan_import(rows, existing, normalize_company)
    s = plan.summary
    assert s["total"] == 12 == len(plan.add) + len(plan.skipped)
    assert s["added"] == len(plan.add) == 6
    assert s["skipped"] == len(plan.skipped) == 6
    assert sum(s["by_reason"].values()) == s["skipped"]
    assert s["by_reason"][li.ALREADY_IN_SHEET_NAME_COMPANY] == 1
    assert s["by_reason"][li.DUPLICATE_IN_FILE] == 1
    assert s["by_reason"][li.NO_COMPANY] == 1
    assert s["by_reason"][li.NOT_A_COMPANY] == 2
    assert s["by_reason"][li.NO_NAME] == 1
    assert s["emails_to_check"] == 2


def test_linkedin_connections_import_plan_warm_row_puts_title_in_note_not_priority():
    plan = li.plan_import(
        [_row(url="https://www.linkedin.com/in/Dana-Reyes-123/?trk=x", position="Operations Manager",
              company="Acme Logistics", connected="15 Mar 2023")],
        [], normalize_company)
    row = plan.add[0]
    assert row["priority"] == ""
    assert row["note"] == "Operations Manager at Acme Logistics. Connected 2023-03-15 (LinkedIn import)"
    assert "Operations Manager" not in str(row["priority"])
    assert row["source"] == "https://www.linkedin.com/in/dana-reyes-123"
    assert row["name"] == "Dana Reyes" and row["company"] == "Acme Logistics"
    assert row["status"] == "Warm Lead"
    assert "last_contact" not in row and "next_followup" not in row
    # No title, no date: the note still reads cleanly.
    bare = li.plan_import([_row(position="", connected="")], [], normalize_company).add[0]
    assert bare["note"] == "Works at Acme Logistics (LinkedIn import)"


def test_linkedin_connections_import_plan_module_is_pure():
    tree = ast.parse(Path(li.__file__).read_text(encoding="utf-8"))
    banned = {"main", "requests", "sqlite3", "os", "logging", "pathlib", "urllib.request", "socket",
              "subprocess", "shutil"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported |= {a.name, a.name.split(".")[0]}
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported |= {mod, mod.split(".")[0]}
            imported |= {f"{mod}.{a.name}" for a in node.names}
    assert not (imported & banned), imported & banned
    attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (attrs & {"read_text", "write_text", "urlopen"}), attrs
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "open" not in called and "print" not in called


def test_linkedin_connections_import_plan_real_names_with_employer_looking_prefixes_are_kept():
    names = ["Independent Bank Group", "Student Transportation of America", "Self Regional Healthcare",
             "Stealth BioTherapeutics", "Confidential Cures"]
    rows = [_row(first=f"P{i}", company=c) for i, c in enumerate(names)]
    plan = li.plan_import(rows, [], normalize_company)
    assert [a["company"] for a in plan.add] == names and plan.skipped == []
    rejected = [_row(first=f"Q{i}", company=c) for i, c in
                enumerate(["Freelance Designer", "Self-Employed Consultant", "Independent Consultant", "Student", "Stealth"])]
    plan = li.plan_import(rejected, [], normalize_company)
    assert [s["reason"] for s in plan.skipped] == [li.NOT_A_COMPANY] * 5


def test_linkedin_connections_import_plan_unparseable_url_cell_does_not_abort_the_plan():
    assert li.canonical_linkedin_url("https://[abc") == ""
    existing = [{"name": "Pat Lane", "company": "Acme Logistics", "email": "", "source_link": "https://[abc", "sheet_uuid": "u1"}]
    rows = [_row(first="Bo", last="Diaz", company="Hooli", url="https://[abc"),
            _row(first="Pat", last="Lane", company="Acme Logistics")]
    plan = li.plan_import(rows, existing, normalize_company)
    assert [a["name"] for a in plan.add] == ["Bo Diaz"]
    assert plan.add[0]["source"] == ""
    assert [s["reason"] for s in plan.skipped] == [li.ALREADY_IN_SHEET_NAME_COMPANY]


def test_linkedin_connections_import_plan_messy_sheet_rows_still_dedupe():
    existing = [
        {"name": "https://www.linkedin.com/in/pat-lane", "company": "", "email": "", "source_link": "", "sheet_uuid": "u1"},
        {"name": "Kim Voss", "company": "", "email": "", "source_link": "", "sheet_uuid": "u2"},
    ]
    rows = [_row(first="Pat", last="Lane", company="Acme Logistics", url="https://www.linkedin.com/in/Pat-Lane/"),
            _row(first="Kim", last="Voss", company="Acme Logistics"),
            _row(first="Zed", last="Quinn", company="Acme Logistics")]
    plan = li.plan_import(rows, existing, normalize_company)
    assert [s["reason"] for s in plan.skipped] == [li.ALREADY_IN_SHEET_URL, li.ALREADY_IN_SHEET_NAME_ONLY]
    assert [a["name"] for a in plan.add] == ["Zed Quinn"]
    # a URL-in-name row is not also treated as a person called "https://..."
    only_url = li.plan_import([_row(first="Zed", last="Quinn")], existing[:1], normalize_company)
    assert len(only_url.add) == 1


def test_linkedin_connections_import_plan_unmapped_company_header_fails_loudly():
    text = "First Name,Last Name,URL,Current Company,Position\nSam,Ortiz,,Globex,Analyst\n"
    with pytest.raises(li.ConnectionsFormatError) as exc:
        li.parse_connections_csv(text)
    assert "Current Company" in str(exc.value) and "Position" in str(exc.value)


def test_linkedin_connections_import_plan_empty_body_reports_zero_rows_in():
    rows = li.parse_connections_csv(HEADER + "\n")
    assert rows == []
    plan = li.plan_import(rows, [], normalize_company)
    assert plan.summary["rows_in"] == 0 and plan.summary["total"] == 0
    mixed = li.plan_import([_row(), _row(first="Bo", company="")], [], normalize_company)
    assert mixed.summary["rows_in"] == 2 == mixed.summary["total"]


def test_linkedin_connections_import_plan_punctuation_in_company_does_not_break_name_match():
    existing = [{"name": "Pat Lane", "company": "Acme, Inc.", "email": "", "source_link": "", "sheet_uuid": "u1"}]
    plan = li.plan_import([_row(first="Pat", last="Lane", company="Acme")], existing, normalize_company)
    assert [s["reason"] for s in plan.skipped] == [li.ALREADY_IN_SHEET_NAME_COMPANY]
    existing = [{"name": "Pat Lane", "company": "Acme", "email": "", "source_link": "", "sheet_uuid": "u1"}]
    plan = li.plan_import([_row(first="Pat", last="Lane", company="Acme, Inc.")], existing, normalize_company)
    assert [s["reason"] for s in plan.skipped] == [li.ALREADY_IN_SHEET_NAME_COMPANY]
