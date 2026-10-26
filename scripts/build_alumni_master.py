"""Build one master list from the saved LinkedIn alumni files and a Carmen Warm export.

    python scripts/build_alumni_master.py --downloads C:/Users/kjmil/Downloads \
        --sheet "C:/Users/kjmil/Downloads/Job_Outreach_CRM - Carmen Warm.tsv" --today 2026-10-26

Reads hope_alumni_*.tsv / .csv, writes three files into <downloads>/alumni_master_<today>/:
  ALL.tsv              every person, with their status, filters and source files (the audit list)
  PASTE_new.tsv        the NEW people who have a Company, as 12-column Carmen Warm rows
  PASTE_new_NO_COMPANY.tsv  the NEW people with no usable company, same format, for a separate tab
  REVIEW_same_name.tsv people whose exact name is already in the sheet under another URL
Nothing is written to the sheet. Prints counts that reconcile; files it cannot read are listed.
"""
import argparse
import csv
import glob
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alumni_master as am  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        return fh.read()


def _write_tsv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, delimiter="\t", lineterminator="\n").writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--downloads", required=True)
    ap.add_argument("--sheet", required=True, help="Carmen Warm export, tab-separated, no header")
    ap.add_argument("--today", default=date.today().isoformat())
    args = ap.parse_args(argv)

    d = date.fromisoformat(args.today)
    today_long = f"{d:%B} {d.day}, {d.year}"

    sheet_rows = list(csv.reader(_read(args.sheet).splitlines(), delimiter="\t"))
    records, used, skipped = [], 0, []
    for path in sorted(glob.glob(os.path.join(args.downloads, "hope_alumni_*"))):
        if not path.lower().endswith((".tsv", ".csv")):
            continue
        try:
            records += am.parse_alumni_text(_read(path), source=os.path.basename(path))
            used += 1
        except am.AlumniFormatError as exc:
            skipped.append((os.path.basename(path), str(exc)))

    result = am.build_master(records, sheet_rows, args.today, today_long)
    out = os.path.join(args.downloads, f"alumni_master_{args.today}")
    os.makedirs(out, exist_ok=True)

    all_rows = [["Status", "Name", "Company", "Profile URL", "Headline", "Employer filters", "Files"]]
    review = [["Name", "Company", "Profile URL", "Headline", "Employer filters", "Sheet row company"]]
    handles, names = am.index_sheet(sheet_rows)
    for status, p in sorted(result["people"], key=lambda sp: (sp[0], sp[1]["name"].lower())):
        company = am.pick_company(p)
        all_rows.append([status, p["name"], company, p["url"], p["headline"],
                         "; ".join(p["filters"]), "; ".join(p["sources"])])
        if status == am.REVIEW_SAME_NAME:
            review.append([p["name"], company, p["url"], p["headline"], "; ".join(p["filters"]),
                           "; ".join(c for c in names.get(am.name_key(p["name"]), []) if c)])
    ordered = sorted(result["rows"].values(), key=lambda r: (r[2].lower(), r[1].lower()))
    paste = [r for r in ordered if r[2]]
    no_company = [r for r in ordered if not r[2]]

    _write_tsv(os.path.join(out, "ALL.tsv"), all_rows)
    _write_tsv(os.path.join(out, "PASTE_new.tsv"), paste)
    _write_tsv(os.path.join(out, "PASTE_new_NO_COMPANY.tsv"), no_company)
    _write_tsv(os.path.join(out, "REVIEW_same_name.tsv"), review)

    s = result["summary"]
    print(f"files read: {used}   unreadable: {len(skipped)}")
    for name, why in skipped:
        print(f"  skipped {name}: {why}")
    for k, v in s.items():
        print(f"{k}: {v}")
    ok = s["input_people"] == s["NEW"] + s["REVIEW_SAME_NAME"] + s["SKIP_IN_SHEET"] == len(result["people"])
    print("reconciles:", ok, "| PASTE rows:", len(paste),
          "| NO_COMPANY rows:", len(no_company), "| wrote", out)
    return 0 if ok and len(paste) + len(no_company) == s["NEW"] else 1


if __name__ == "__main__":
    sys.exit(main())
