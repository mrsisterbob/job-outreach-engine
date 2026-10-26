"""One master list from the LinkedIn alumni-tool files, checked against a Carmen Warm export.

Text and lists in, a classified list out. This module reads no files and writes nothing; the
runner in scripts/build_alumni_master.py does the I/O. It never prints row contents (they are
other people's data).

A person is identified by their LinkedIn handle (the slug in linkedin.com/in/<slug>). The sheet
side is indexed by EVERY handle found in ANY cell of a row, because the real sheet holds the same
person under a Hope Connection link whose `userProfile=` is their handle, and under rows whose
Name cell holds the URL. A URL-column-only match missed 80 of those on 2026-10-26.

Three outcomes, and every input person lands in exactly one:
  SKIP_IN_SHEET     the handle is already somewhere in the sheet
  REVIEW_SAME_NAME  not in the sheet by handle, but an exact name match is (could be the same
                    person under another URL, could be two people): never auto-pasted
  NEW               neither; written as a Carmen Warm PEOPLE row
"""
import csv
import io
import re
import urllib.parse
import uuid

import linkedin_import as li

NEW = "NEW"
SKIP_IN_SHEET = "SKIP_IN_SHEET"
REVIEW_SAME_NAME = "REVIEW_SAME_NAME"

# LinkedIn sidebar modules the alumni page scrape picked up: the same three profiles land in
# every file, with the module title in the Name cell.
JUNK_NAMES = frozenset({
    "pages people also viewed", "people also follow", "connections that work here",
})

# Search terms the files were named after, not employers. A person matched by one of these still
# gets the filter listed in the note, but it never becomes the Company cell.
GENERIC_FILTERS = frozenset({
    "federal", "united states", "company", "state", "student", "self employed", "freelance",
    "hope college",
})

SHEET_COLUMNS = 12
_HANDLE_RE = re.compile(r"linkedin\.com/in/([^/?#\s\"']+)", re.I)
_HOPE_RE = re.compile(r"userprofile=([^&\s\"']+)", re.I)
_LABEL_RE = re.compile(r"^hope_alumni_(.+?)_\d{4}-\d{2}-\d{2}", re.I)
_DEGREE_RE = re.compile(r"degree connection|^\W*\d(st|nd|rd)\+?\W*$", re.I)

_NAME_FIELDS = ("name", "full name")
_HEADLINE_FIELDS = ("title", "headline")
_FILTER_FIELDS = ("company", "company filter")
_URL_FIELDS = ("profile_url", "linkedin url", "profile url")


class AlumniFormatError(ValueError):
    """The file is not one of the alumni exports (no recognizable header)."""


def handles_in(text):
    """Every LinkedIn handle named in `text`: /in/<slug> links and Hope Connection userProfile=."""
    found = set()
    for pattern in (_HANDLE_RE, _HOPE_RE):
        for m in pattern.finditer(urllib.parse.unquote(str(text or ""))):
            found.add(m.group(1).lower())
    return found


def profile_handle(url):
    """The one handle in a profile URL, or ''."""
    hs = _HANDLE_RE.findall(str(url or ""))
    return hs[0].lower() if hs else ""


def name_key(name):
    """Lowercase letters and spaces only, credentials after a comma and (maiden) names dropped."""
    n = re.sub(r"\(.*?\)", " ", str(name or "").lower())
    n = n.split(",")[0]
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", n)).strip()


def clean_headline(raw):
    """Drop the connection-degree badges the page put in front of the headline."""
    parts = [p.strip() for p in str(raw or "").split(" | ")]
    keep = [p for p in parts if p and not _DEGREE_RE.search(p) and "�" not in p]
    return " | ".join(keep)


def employer_from_headline(headline):
    """The text after the last ' at ' or ' @ ' in the first headline segment, '' when absent."""
    first = str(headline or "").split(" | ")[0]
    m = re.split(r"\s+(?:at|@)\s+", first)
    return m[-1].strip(" ,.") if len(m) > 1 else ""


def file_label(source):
    """The company the file was searched under, from hope_alumni_<Company>_<date>; '' otherwise."""
    m = _LABEL_RE.match(str(source or ""))
    if not m:
        return ""
    # The saver turned "&" into "_" and "." into "_" (Johnson___Johnson, C_H__Robinson); a plain
    # replace("_", " ") left "Johnson   Johnson" as a second company next to "Johnson & Johnson".
    label = m.group(1).replace("___", " & ")
    label = re.sub(r"\b([A-Z])_([A-Z])__", r"\1.\2. ", label)
    return label.replace("_", " ").strip()


def company_is_headline_tail(company, headline):
    """True when the Company cell is just a ' | ' piece of the headline. Some saved files carry
    the tail of the headline there ('... | Leading with Purpose'), which is a tagline, not an
    employer. A real 'Analyst at Ford' company is not a standalone piece, so it passes."""
    c = str(company or "").strip().lower()
    return bool(c) and any(c == seg.strip().lower() and not re.search(r"\s(?:at|@)\s", seg)
                           for seg in str(headline or "").split(" | "))


def _field(header, wanted):
    for i, h in enumerate(header):
        if re.sub(r"\s+", " ", h.strip().lower().lstrip("﻿")) in wanted:
            return i
    return None


def parse_alumni_text(text, source=""):
    """Rows of one alumni file as dicts (name, headline, filter, url, handle, source). Raises
    AlumniFormatError when the first line is not a header naming a name and a URL column. The
    delimiter is read off the header line: the saved files are tab- or comma-separated."""
    text = str(text or "").lstrip("﻿")
    lines = text.splitlines()
    if not lines:
        raise AlumniFormatError("empty file")
    delim = "\t" if lines[0].count("\t") >= 2 else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    header = next(reader)
    i_name, i_url = _field(header, _NAME_FIELDS), _field(header, _URL_FIELDS)
    if i_name is None or i_url is None:
        raise AlumniFormatError("header has no name+URL columns: " + " | ".join(header)[:120])
    i_head, i_filter = _field(header, _HEADLINE_FIELDS), _field(header, _FILTER_FIELDS)

    def cell(row, i):
        return row[i].strip() if i is not None and i < len(row) else ""

    out = []
    for row in reader:
        name = cell(row, i_name)
        if not name or name.lower() in JUNK_NAMES:
            continue
        url = cell(row, i_url)
        handle = profile_handle(url)
        if not handle:
            continue
        headline = clean_headline(cell(row, i_head))
        flt = cell(row, i_filter)
        if company_is_headline_tail(flt, headline):
            flt = file_label(source)
        out.append({
            "name": name, "headline": headline,
            "filter": flt, "url": li.canonical_linkedin_url(url) or url,
            "handle": handle, "source": source,
        })
    return out


def merge_people(records):
    """One person per handle: every filter they matched and every file they came from, in the
    order first seen; the longest headline wins."""
    people = {}
    for r in records:
        p = people.get(r["handle"])
        if p is None:
            p = people[r["handle"]] = {
                "handle": r["handle"], "name": r["name"], "url": r["url"],
                "headline": "", "filters": [], "sources": [],
            }
        if len(r["headline"]) > len(p["headline"]):
            p["headline"] = r["headline"]
        if r["filter"] and r["filter"] not in p["filters"]:
            p["filters"].append(r["filter"])
        if r["source"] and r["source"] not in p["sources"]:
            p["sources"].append(r["source"])
    return people


def index_sheet(rows):
    """(handles, names) over sheet rows. `handles` holds every handle in any cell of any row;
    `names` maps name_key -> list of that row's Company cell."""
    handles, names = set(), {}
    for r in rows:
        handles |= handles_in(" ".join(r))
        key = name_key(r[1]) if len(r) > 1 else ""
        if key and "linkedin.com/" not in str(r[1]).lower():
            names.setdefault(key, []).append(r[2] if len(r) > 2 else "")
    return handles, names


def classify(person, handles, names):
    if person["handle"] in handles:
        return SKIP_IN_SHEET
    key = name_key(person["name"])
    if key and key in names:
        return REVIEW_SAME_NAME
    return NEW


def _usable_company(name):
    """A real employer name: not a search term, not the school (a 'Hope College studying ...' cell
    is an about-me fragment), and not mojibake."""
    low = str(name or "").strip().lower()
    return bool(low) and low not in GENERIC_FILTERS and "hope college" not in low and "�" not in low


def pick_company(person):
    """First usable filter; else the usable employer named in the headline; else ''."""
    for f in person["filters"]:
        if _usable_company(f):
            return f
    emp = employer_from_headline(person["headline"])
    return emp if _usable_company(emp) else ""


def _note(person, today_iso):
    bits = ["LinkedIn alumni"]
    if person["filters"]:
        bits.append("employer filter: " + "; ".join(person["filters"]))
    if person["headline"]:
        bits.append(person["headline"])
    return f"[{today_iso}] " + " | ".join(bits)


def to_sheet_row(person, today_iso, today_long, new_uuid=None):
    """The 12-column Carmen Warm row: date, name, company, email, priority, status, blank, URL,
    note, UUID, blank, company again (the helper column the sheet carries)."""
    company = pick_company(person)
    row = [
        today_long, person["name"], company, "", "Medium", "Warm Lead", "", person["url"],
        _note(person, today_iso), new_uuid or str(uuid.uuid4()), "", company,
    ]
    return [re.sub(r"[\t\r\n]+", " ", c) for c in row]


def build_master(records, sheet_rows, today_iso, today_long):
    """Classify every person. Returns {"people": [(status, person)], "rows": {handle: sheet row
    for NEW people}, "summary": {...}}; summary["input_people"] always equals the sum of the three
    statuses, and records_in - input_people is the number of repeat appearances merged away."""
    handles, names = index_sheet(sheet_rows)
    people = merge_people(records)
    classified, rows = [], {}
    for p in people.values():
        status = classify(p, handles, names)
        classified.append((status, p))
        if status == NEW:
            rows[p["handle"]] = to_sheet_row(p, today_iso, today_long)
    counts = {s: sum(1 for st, _ in classified if st == s) for s in (NEW, REVIEW_SAME_NAME, SKIP_IN_SHEET)}
    summary = {
        "records_in": len(records), "input_people": len(people), **counts,
        "repeat_appearances_merged": len(records) - len(people),
        "new_without_company": sum(1 for h, r in rows.items() if not r[2]),
        "new_in_multiple_files": sum(1 for st, p in classified if st == NEW and len(p["sources"]) > 1),
    }
    return {"people": classified, "rows": rows, "summary": summary}
