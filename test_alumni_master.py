"""Tests for alumni_master.py. The end-to-end test drives scripts/build_alumni_master.main() over
real files in a temp dir and reads the PASTE file back, because the failure this guards against is
a list that reconciles in memory and reads back different."""
import csv
import os
import sys

import pytest

import alumni_master as am

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import build_alumni_master as runner  # noqa: E402

TSV = (
    'Name\tTitle\tCompany\tProfile_URL\n'
    '"Ann Lee"\t"2nd degree connection"\t"Ford"\t"https://www.linkedin.com/in/annlee/"\n'
    '"Bob Ray, CPA"\t"Analyst at Ford"\t"Ford"\t"https://www.linkedin.com/in/bobray"\n'
    '"Pages people also viewed"\t"Calvin University"\t"Ford"\t"http://www.linkedin.com/in/junk1"\n'
    '"Dee Fox"\t"2nd degree connection"\t"Federal"\t"https://www.linkedin.com/in/deefox"\n'
)
CSV_ = (
    'Full Name,Headline,LinkedIn URL,Company Filter\n'
    '"Ann Lee","2nd degree connection | ��2nd | Engineer @ Ford | CAD","https://www.linkedin.com/in/annlee?x=1","Gentex"\n'
    '"Cy Moe","Student at Hope College","https://www.linkedin.com/in/cymoe",""\n'
    '"Eve Roe","Buyer","https://www.linkedin.com/in/everoe",""\n'
    '"Hal Poe","Nurse","https://www.linkedin.com/in/halpoe",""\n'
)
# sheet rows: 12 cells. Bob is there under a Hope Connection link, Cy has the URL in the Name
# cell, and another "Eve Roe" exists under a different URL.
SHEET = [
    ["", "Bob Ray", "Ford", "", "", "Warm Lead", "",
     "https://connection.hope.edu/hub/hopecollege/person?modal=profile&userProfile=bobray",
     "", "u1", "", "Ford"],
    ["", "https://www.linkedin.com/in/cymoe", "X", "", "", "Warm Lead", "", "", "", "u2", "", "X"],
    ["", "Eve Roe", "Acme", "", "", "Warm Lead", "", "https://www.linkedin.com/in/other-eve",
     "", "u3", "", "Acme"],
]


def test_parse_drops_junk_and_degree_badges():
    rows = am.parse_alumni_text(TSV, "a.tsv")
    assert [r["handle"] for r in rows] == ["annlee", "bobray", "deefox"]
    csv_rows = am.parse_alumni_text(CSV_, "b.csv")
    assert csv_rows[0]["headline"] == "Engineer @ Ford | CAD"
    assert csv_rows[0]["handle"] == "annlee"


def test_headline_tail_in_company_cell_falls_back_to_file_label():
    text = (
        "Name\tTitle\tCompany\tProfile_URL\n"
        '"Jon Potter"\t"Data Planner | Inspiring Teams | Leading with Purpose"\t"Leading with Purpose"'
        '\t"https://www.linkedin.com/in/jonp"\n'
        '"Ron H"\t"Sales Consultant at Elhart GMC"\t"Elhart GMC"\t"https://www.linkedin.com/in/ronh"\n'
    )
    rows = am.parse_alumni_text(text, "hope_alumni_Davenport_University_2026-10-26.tsv")
    assert [r["filter"] for r in rows] == ["Davenport University", "Elhart GMC"]


def test_school_fragments_and_mojibake_are_not_companies():
    for bad in ("Hope College studying Communication and Business", "Aurora � Autonomous Vehicles"):
        assert am.pick_company({"filters": [bad], "headline": ""}) == ""
    assert am.pick_company({"filters": ["Hope College majoring in X"], "headline": "Buyer at Ford"}) == "Ford"


def test_unrecognized_header_raises():
    with pytest.raises(am.AlumniFormatError):
        am.parse_alumni_text("October 23, 2026\tAnn\tFord\t\tMedium\n", "paste.tsv")


def test_sheet_matching_sees_hope_links_and_name_cell_urls():
    handles, names = am.index_sheet(SHEET)
    assert {"bobray", "cymoe", "other-eve"} <= handles
    assert "eve roe" in names
    assert "https://www.linkedin.com/in/cymoe" not in names


def test_company_prefers_real_filter_over_generic():
    p = {"filters": ["Federal", "Gentex"], "headline": "Engineer @ Ford"}
    assert am.pick_company(p) == "Gentex"
    assert am.pick_company({"filters": ["Federal"], "headline": "Analyst at Ford"}) == "Ford"
    assert am.pick_company({"filters": [], "headline": "Student at Hope College"}) == ""


def test_end_to_end_reconciles_and_pastes_only_new(tmp_path):
    (tmp_path / "hope_alumni_Ford_2026-10-26.tsv").write_text(TSV, encoding="utf-8")
    (tmp_path / "hope_alumni_5.csv").write_text(CSV_, encoding="utf-8")
    (tmp_path / "hope_alumni_carmen_warm.tsv").write_text("October 23, 2026\tAnn\tFord\n", encoding="utf-8")
    sheet = tmp_path / "sheet.tsv"
    with open(sheet, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, delimiter="\t").writerows(SHEET)

    rc = runner.main(["--downloads", str(tmp_path), "--sheet", str(sheet), "--today", "2026-10-26"])
    assert rc == 0

    out = tmp_path / "alumni_master_2026-10-26"
    paste = list(csv.reader(open(out / "PASTE_new.tsv", encoding="utf-8", newline=""), delimiter="\t"))
    assert [r[1] for r in paste] == ["Ann Lee"]  # Bob, Cy in sheet; Eve is REVIEW
    blank = list(csv.reader(open(out / "PASTE_new_NO_COMPANY.tsv", encoding="utf-8", newline=""), delimiter="	"))
    assert sorted(r[1] for r in blank) == ["Dee Fox", "Hal Poe"] and all(not r[2] for r in blank)
    assert all(len(r) == am.SHEET_COLUMNS for r in paste)
    ann = next(r for r in paste if r[1] == "Ann Lee")
    assert ann[2] == "Gentex" and "Gentex; Ford" in ann[8] and ann[5] == "Warm Lead"
    assert ann[0] == "October 26, 2026" and ann[7] == "https://www.linkedin.com/in/annlee"
    review = list(csv.reader(open(out / "REVIEW_same_name.tsv", encoding="utf-8", newline=""), delimiter="\t"))
    assert [r[0] for r in review[1:]] == ["Eve Roe"]
    allrows = list(csv.reader(open(out / "ALL.tsv", encoding="utf-8", newline=""), delimiter="\t"))
    assert len(allrows) - 1 == 6  # Ann, Bob, Dee, Cy, Eve, Hal: each person once
    assert len({r[9] for r in paste + blank}) == len(paste + blank)
