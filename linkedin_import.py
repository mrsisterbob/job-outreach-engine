"""Pure planning for a LinkedIn connections import into Carmen Warm.

Text and lists in, a reviewed ImportPlan out. This module reads no files, makes no network or
database call, and writes nothing: Kevin reads the plan before any row exists. It never logs or
prints row contents (they are other people's data).

COLUMN NAMES ARE UNVERIFIED against a real export. The header is found by scanning for the
first line holding both "First Name" and "Last Name", columns are matched by NAME through the
alias table below (never by position), unknown columns are ignored, and a header that lacks a
name column raises ConnectionsFormatError listing the columns that WERE found, so a wrong
assumption fails loudly instead of producing a clean zero.

EMAIL CHECK IS NOT DONE HERE. A surviving row that has an email is still an `add` candidate but
is also listed in `emails_to_check` and flagged `needs_email_check`. The authoritative
de-duplication for emails is the server-side `find_contact_by_email` action in Code.gs, which
searches every tab (people_only=1 limits it to the PEOPLE tabs). That is I/O, so the later write
step must call it for each listed email BEFORE writing and drop the hits.

Landing tab is Carmen Warm (PEOPLE schema). The job title goes in `note`, never in `priority`:
column E of a PEOPLE row is "Context / Priority" and get_warm_crm_contacts parses it as a priority.
`last_contact` and `next_followup` are deliberately NOT set: the follow-up sequencer derives its
rung from the date gap, so the write step must stamp today and the first ladder rung, not the
(possibly years-old) Connected On date.
"""
import csv
import io
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime

import pipeline_utils as pu

# ---- skip reasons ----
NO_NAME = "no_name"
NO_COMPANY = "no_company"
NOT_A_COMPANY = "not_a_company"
DUPLICATE_IN_FILE = "duplicate_in_file"
ALREADY_IN_SHEET_URL = "already_in_sheet_url"
ALREADY_IN_SHEET_EMAIL = "already_in_sheet_email"
ALREADY_IN_SHEET_NAME_COMPANY = "already_in_sheet_name_company"
ALREADY_IN_SHEET_NAME_ONLY = "already_in_sheet_name_only_blank_company"

ALL_REASONS = (
    NO_NAME, NO_COMPANY, NOT_A_COMPANY, DUPLICATE_IN_FILE,
    ALREADY_IN_SHEET_URL, ALREADY_IN_SHEET_EMAIL, ALREADY_IN_SHEET_NAME_COMPANY,
    ALREADY_IN_SHEET_NAME_ONLY,
)

# Same status the /w quick-add writes to Carmen Warm (main.py quick_add, "Warm Lead").
WARM_STATUS = "Warm Lead"

# Whole-value non-employers. A bare word is rejected only when it is the ENTIRE company
# ("Student"), never as a prefix: "Independent Bank Group" and "Stealth BioTherapeutics" are real.
NON_EMPLOYER_PHRASES = frozenset({
    "self employed", "selfemployed", "self", "freelance", "freelancer", "retired", "student",
    "unemployed", "stealth", "stealth startup", "stealth mode", "confidential", "independent",
    "independent consultant", "consultant", "n a", "na", "none", "looking for opportunities",
    "between jobs", "open to work",
})

# Multi-word openings that are never an employer name, so "Freelance Designer" is rejected.
NON_EMPLOYER_PREFIXES = ("freelance", "freelancer", "self employed", "selfemployed", "independent consultant")

# canonical key -> accepted header names (compared lowercased, whitespace-collapsed)
COLUMN_ALIASES = {
    "first_name": ("first name", "firstname", "first"),
    "last_name": ("last name", "lastname", "last", "surname"),
    "name": ("name", "full name", "fullname"),
    "url": ("url", "profile url", "linkedin url", "linkedin profile", "profile", "linkedin"),
    "email": ("email address", "email", "e-mail", "e-mail address"),
    "company": ("company", "organization", "organisation", "company name", "employer"),
    "position": ("position", "title", "job title", "headline"),
    "connected_on": ("connected on", "connected", "connection date", "date connected"),
}

_DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y")


class ConnectionsFormatError(ValueError):
    """The export is not shaped as expected; the message lists the columns that were found."""


def _collapse(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _header_key(value):
    return _collapse(str(value or "").lstrip("﻿")).lower()


def _find_header_index(lines):
    """Index of the first line containing both 'first name' and 'last name'; else a fallback
    to the first line that looks like a header at all; else None."""
    for i, line in enumerate(lines):
        low = line.lower()
        if "first name" in low and "last name" in low:
            return i
    name_words = {a for key in ("first_name", "last_name", "name") for a in COLUMN_ALIASES[key]}
    for i, line in enumerate(lines):
        cells = next(csv.reader([line]), [])
        if any(_header_key(c) in name_words for c in cells):
            return i
    for i, line in enumerate(lines):
        if not line.strip().lower().startswith("notes") and line.count(",") >= 2:
            return i
    return None


def parse_connections_csv(text):
    """Parse the export text into a list of dicts with canonical keys (first_name, last_name,
    name, url, email, company, position, connected_on); a key is present only if its column is."""
    text = str(text or "").lstrip("﻿")
    lines = text.splitlines()
    idx = _find_header_index(lines)
    if idx is None:
        raise ConnectionsFormatError("no header row found; columns found: (none)")
    body = "\n".join(lines[idx:])
    reader = csv.reader(io.StringIO(body, newline=""))
    header = next(reader, [])
    found = [_collapse(h.lstrip("﻿")) for h in header]
    lookup = {}
    for col, raw in enumerate(header):
        key = _header_key(raw)
        for canon, aliases in COLUMN_ALIASES.items():
            if key in aliases and canon not in lookup.values():
                lookup[col] = canon
                break
    canons = set(lookup.values())
    if "company" not in canons:
        raise ConnectionsFormatError(
            "no company column could be mapped (looked for: "
            + ", ".join(COLUMN_ALIASES["company"]) + "); columns found: " + ", ".join(found))
    if not (("first_name" in canons and "last_name" in canons) or "name" in canons):
        raise ConnectionsFormatError(
            "missing a first and last name column (or a full name column); columns found: "
            + ", ".join(found))
    rows = []
    for cells in reader:
        if not any(_collapse(c) for c in cells):
            continue
        row = {}
        for col, canon in lookup.items():
            row[canon] = cells[col] if col < len(cells) else ""
        rows.append(row)
    return rows


def canonical_linkedin_url(url):
    """https://www.linkedin.com/in/<slug>, lowercase, no query, fragment or trailing slash;
    "" when the value is not a LinkedIn /in/ profile."""
    text = _collapse(url)
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:  # e.g. "https://[abc": a bad cell means no URL, never an aborted plan
        return ""
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    match = re.match(r"^/in/([^/]+)", urllib.parse.unquote(parsed.path))
    if not match:
        return ""
    slug = match.group(1).strip().lower()
    return f"https://www.linkedin.com/in/{slug}" if slug else ""


def _iso_date(value):
    text = _collapse(value)
    if not text:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def clean_connection(raw):
    """One parsed row -> {name, company, title, url, email, connected_on}. Idempotent."""
    raw = raw or {}
    first = _collapse(raw.get("first_name"))
    last = _collapse(raw.get("last_name"))
    name = _collapse(f"{first} {last}") or _collapse(raw.get("name"))
    email = _collapse(raw.get("email")).lower()
    if "@" not in email or " " in email:
        email = ""
    return {
        "name": name,
        "company": _collapse(raw.get("company")),
        "title": _collapse(raw.get("position") or raw.get("title")),
        "url": canonical_linkedin_url(raw.get("url")),
        "email": email,
        "connected_on": _iso_date(raw.get("connected_on")),
    }


def _is_non_employer(company):
    flat = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
    if flat in NON_EMPLOYER_PHRASES:
        return True
    return any(flat.startswith(p + " ") for p in NON_EMPLOYER_PREFIXES)


def to_warm_row(clean):
    """Cleaned connection -> Carmen Warm named-object fields (Code.gs FIELD_ALIASES PEOPLE)."""
    title, company, joined = clean.get("title", ""), clean.get("company", ""), clean.get("connected_on", "")
    parts = [f"{title} at {company}" if title else f"Works at {company}"]
    if joined:
        parts.append(f"Connected {joined}")
    note = ". ".join(parts) + " (LinkedIn import)"
    return {
        "name": clean.get("name", ""),
        "company": company,
        "email": clean.get("email", ""),
        "priority": "",
        "status": WARM_STATUS,
        "source": clean.get("url", ""),
        "note": note,
    }


@dataclass
class ImportPlan:
    add: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    emails_to_check: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _alnum_only(value):
    """Drop punctuation the injected normalizer leaves behind ("acme," -> "acme")."""
    return _collapse("".join(ch for ch in str(value or "") if ch.isalnum() or ch == " ").lower())


def _name_company_key(name, company, normalize_company):
    n = _collapse(name).lower()
    c = _alnum_only(normalize_company(company))
    return (n, c) if n and c else None


def plan_import(rows, existing, normalize_company):
    """Clean, de-duplicate and map `rows`. `existing` is a list of PEOPLE-row dicts (name,
    company, email, source_link, sheet_uuid). `normalize_company` is injected (the real call
    passes main.normalize_company_for_match). Nothing is written."""
    sheet_urls, sheet_emails, sheet_names, sheet_name_only = set(), set(), set(), set()
    for rec in existing or []:
        # A messy sheet row can hold the profile URL in the NAME column.
        name_url = canonical_linkedin_url(rec.get("name"))
        url = canonical_linkedin_url(rec.get("source_link") or rec.get("url"))
        for u in (url, name_url):
            if u:
                sheet_urls.add(u)
        email = _collapse(rec.get("email")).lower()
        if email:
            sheet_emails.add(email)
        if name_url or "linkedin.com/" in str(rec.get("name") or "").lower():
            continue  # the name cell is a URL, not a person
        key = _name_company_key(rec.get("name"), rec.get("company"), normalize_company)
        if key:
            sheet_names.add(key)
        elif _collapse(rec.get("name")) and not _alnum_only(normalize_company(rec.get("company"))):
            sheet_name_only.add(_collapse(rec.get("name")).lower())

    file_urls, file_emails, file_names = set(), set(), set()
    plan = ImportPlan()

    def skip(clean, reason):
        plan.skipped.append({
            "name": clean["name"], "company": clean["company"],
            "url": clean["url"], "reason": reason,
        })

    for raw in rows or []:
        clean = clean_connection(raw)
        if not clean["name"]:
            skip(clean, NO_NAME)
            continue
        if not clean["company"]:
            skip(clean, NO_COMPANY)
            continue
        if _is_non_employer(clean["company"]) or not pu.is_probable_company_name(clean["company"]):
            skip(clean, NOT_A_COMPANY)
            continue

        url, email = clean["url"], clean["email"]
        nc_key = _name_company_key(clean["name"], clean["company"], normalize_company)
        reason = None
        if url and url in sheet_urls:
            reason = ALREADY_IN_SHEET_URL
        elif url and url in file_urls:
            reason = DUPLICATE_IN_FILE
        elif email and email in sheet_emails:
            reason = ALREADY_IN_SHEET_EMAIL
        elif email and email in file_emails:
            reason = DUPLICATE_IN_FILE
        elif nc_key and nc_key in sheet_names:
            reason = ALREADY_IN_SHEET_NAME_COMPANY
        elif clean["name"].lower() in sheet_name_only:
            reason = ALREADY_IN_SHEET_NAME_ONLY
        elif nc_key and nc_key in file_names:
            reason = DUPLICATE_IN_FILE
        if reason:
            skip(clean, reason)
            continue

        if url:
            file_urls.add(url)
        if email:
            file_emails.add(email)
        if nc_key:
            file_names.add(nc_key)
        item = to_warm_row(clean)
        if email:
            item["needs_email_check"] = True
            plan.emails_to_check.append({
                "email": email, "name": clean["name"], "company": clean["company"],
                "needs_email_check": True,
            })
        plan.add.append(item)

    by_reason = {r: 0 for r in ALL_REASONS}
    for s in plan.skipped:
        by_reason[s["reason"]] += 1
    plan.summary = {
        "rows_in": len(rows or []),
        "total": len(plan.add) + len(plan.skipped),
        "added": len(plan.add),
        "skipped": len(plan.skipped),
        "by_reason": by_reason,
        "emails_to_check": len(plan.emails_to_check),
    }
    return plan
