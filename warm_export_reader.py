"""Read the two CRM TSV exports (Carmen Warm, alumni PASTE_new) into per-company counts.

Privacy: the files hold names, emails and LinkedIn links. This module keeps, per company, only a
display name (the company cell), a contact count, and the set of email DOMAINS. It never returns,
prints or logs a name, a full email, a URL or a note. The file-reading helper takes paths as
arguments; nothing read here is written anywhere.

Layout (measured 2026-10-26): exactly 12 tab-separated columns, no header. Column 3 (index 2) is
the company, column 12 repeats it (fallback), column 4 (index 3) is the email.
"""
import re

COLUMNS = 12
COMPANY_COL = 2
EMAIL_COL = 3
COMPANY_FALLBACK_COL = 11
MIN_CONTACTS = 5


def _key(name, normalizer):
    base = normalizer(name) if normalizer else str(name).lower()
    return re.sub(r"[^a-z0-9]", "", str(base).lower())


def _email_domain(cell):
    cell = (cell or "").strip().lower()
    if cell.count("@") != 1:
        return ""
    domain = cell.rsplit("@", 1)[1].strip(".")
    return domain if "." in domain and " " not in domain else ""


def parse_rows(text, source="", normalizer=None):
    """Parse TSV text. Returns (companies, stats).

    companies: {key: {"name": display, "count": n, "domains": [domain, ...], "by_source": {source: n}}}
    stats: {"rows": parsed rows, "bad_rows": wrong column count (skipped), "no_company": blank
            company in both columns (skipped)}
    """
    companies = {}
    stats = {"rows": 0, "bad_rows": 0, "no_company": 0}
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) != COLUMNS:
            stats["bad_rows"] += 1
            continue
        name = cols[COMPANY_COL].strip() or cols[COMPANY_FALLBACK_COL].strip()
        key = _key(name, normalizer) if name else ""
        if not key:
            stats["no_company"] += 1
            continue
        stats["rows"] += 1
        c = companies.setdefault(key, {"name": name, "count": 0, "domains": [], "by_source": {}})
        c["count"] += 1
        c["by_source"][source] = c["by_source"].get(source, 0) + 1
        d = _email_domain(cols[EMAIL_COL])
        if d:
            c["domains"].append(d)
    return companies, stats


def combine(parsed_list):
    """Merge several (companies, stats) results into one."""
    out, stats = {}, {"rows": 0, "bad_rows": 0, "no_company": 0}
    for companies, st in parsed_list:
        for k in stats:
            stats[k] += st.get(k, 0)
        for key, c in companies.items():
            o = out.setdefault(key, {"name": c["name"], "count": 0, "domains": [], "by_source": {}})
            o["count"] += c["count"]
            o["domains"].extend(c["domains"])
            for s, n in c["by_source"].items():
                o["by_source"][s] = o["by_source"].get(s, 0) + n
    return out, stats


def companies_at_least(companies, minimum=MIN_CONTACTS):
    """The companies with `minimum` or more contacts (exactly 5 is in, 4 is out)."""
    return {k: v for k, v in companies.items() if v["count"] >= minimum}


def read_files(paths, normalizer=None):
    """Read each TSV path (label = file's position) and combine. A missing/blank file adds nothing."""
    parsed = []
    for i, p in enumerate(paths):
        try:
            with open(p, encoding="utf-8", errors="replace", newline="") as f:
                text = f.read()
        except OSError:
            text = ""
        parsed.append(parse_rows(text, source=f"file{i + 1}", normalizer=normalizer))
    return combine(parsed)
