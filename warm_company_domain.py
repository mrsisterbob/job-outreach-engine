"""Pure logic, no I/O: pick a company's corporate web domain from its contacts' email addresses."""
from collections import Counter

# Free-mail, ISP and school-style domains that say nothing about the employer.
NON_CORPORATE_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "charter.net",
    "cox.net", "earthlink.net", "mail.com", "gmx.com", "zoho.com",
})


def email_domain(email):
    """Lowercase domain of a well-formed email, else ''. Never raises."""
    if not isinstance(email, str) or email.count("@") != 1:
        return ""
    domain = email.strip().lower().rsplit("@", 1)[1].strip(".")
    if "." not in domain or " " in domain or not all(domain.split(".")):
        return ""
    return domain


def is_corporate_domain(domain):
    if not domain or domain in NON_CORPORATE_DOMAINS:
        return False
    return not (domain.endswith(".edu") or ".edu." in domain or domain.endswith(".gov"))


def corporate_domain(emails):
    """The corporate domain most of `emails` share ('' when none). Ties go to the domain seen first."""
    counts = Counter()
    order = {}
    for email in emails or []:
        d = email_domain(email)
        if is_corporate_domain(d):
            counts[d] += 1
            order.setdefault(d, len(order))
    if not counts:
        return ""
    return max(counts, key=lambda d: (counts[d], -order[d]))


def guess_domain(slug):
    """Last-resort guess, counted separately from evidence by callers."""
    slug = str(slug or "").strip().lower()
    return f"{slug}.com" if slug else ""
