"""Pure logic, no I/O: name the job-board system behind a careers page.

Rule: the final URL host decides first. Otherwise every vendor whose marker appears in a
script/iframe/link/anchor target is collected; exactly one vendor -> that vendor, several ->
"unknown" (a page that links out to many ATSes is not evidence for any one). With no vendor,
a page that looks like a job listing is "own_site", anything else "unknown".
"""
import re
from urllib.parse import urlparse

# vendor -> host substrings (checked against a URL host or a markup link target)
VENDOR_MARKERS = {
    "workday": ("myworkdayjobs.com", "myworkdaysite.com", ".wd1.", ".wd3.", ".wd5."),
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "icims": ("icims.com",),
    "taleo": ("taleo.net",),
    "successfactors": ("successfactors.com", "successfactors.eu", "sapsf.com"),
    "smartrecruiters": ("smartrecruiters.com",),
    "oracle": ("oraclecloud.com",),
    "workable": ("workable.com",),
    "bamboohr": ("bamboohr.com",),
    "adp": ("workforcenow.adp.com", "recruiting.adp.com", "adp.com/recruit"),
    "ukg": ("ultipro.com", "ukg.com", "recruiting.ultipro"),
}
SYSTEMS = tuple(VENDOR_MARKERS) + ("own_site", "unknown")

_LINK_RE = re.compile(r"""(?:src|href|action|data-src)\s*=\s*["']([^"']+)["']""", re.I)
_JOB_WORDS = re.compile(r"open (positions|roles)|current openings|job openings|apply now|view (all )?jobs|job search|search jobs", re.I)


def _vendor_for(text):
    low = text.lower()
    return [v for v, marks in VENDOR_MARKERS.items() if any(m in low for m in marks)]


def classify_board_system(final_url, html):
    """Return one of SYSTEMS. Never raises; non-string input is treated as empty."""
    url = final_url if isinstance(final_url, str) else ""
    page = html if isinstance(html, str) else ""
    host = (urlparse(url).netloc or "").lower()
    if host:
        hits = _vendor_for(host)
        if hits:
            return hits[0] if len(hits) == 1 else "unknown"
    found = []
    for target in _LINK_RE.findall(page):
        for v in _vendor_for(urlparse(target).netloc or target.split("/")[0]):
            if v not in found:
                found.append(v)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        return "unknown"
    if page and _JOB_WORDS.search(page):
        return "own_site"
    return "unknown"
