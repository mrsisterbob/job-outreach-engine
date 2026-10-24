"""Turn a Workday job or careers URL into the ``tenant/wdN/site`` triple.

Pure logic: no I/O, no network.

``board_triple_from_url`` proves only that a URL has the right SHAPE. It does NOT prove the board
answers: the wildcard Workday hostname answers 422 for any tenant, so a status code cannot confirm a
guess either. Whoever stores a triple must verify it with a real fetch (TASK-143).

Returns "" for anything that is not a Workday address or cannot yield a trustworthy triple.
"""
import re
from urllib.parse import urlsplit

_HOST_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)\.(wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)
# Locale segment: xx-YY (en-US, fr-CA). Case-sensitive on purpose.
_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# Path words that are never a site name.
_NOT_A_SITE = {"job", "jobs", "wday", "login", "userhome", "details", "apply"}


def board_triple_from_url(url) -> str:
    """Return 'tenant/wdN/site' for a Workday job/careers/CXS URL, else ''."""
    if not url or not isinstance(url, str):
        return ""
    text = url.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").strip(".")
    except ValueError:
        return ""
    m = _HOST_RE.match(host)
    if not m:
        return ""
    tenant, wd = m.group(1).lower(), m.group(2).lower()
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        return ""

    # CXS API URL: /wday/cxs/{tenant}/{site}/jobs. Accepted only when it reads cleanly and the
    # tenant in the path agrees with the host.
    if segs[0].lower() == "wday":
        if len(segs) >= 4 and segs[1].lower() == "cxs" and segs[2].lower() == tenant:
            site = segs[3]
            if site.lower() not in _NOT_A_SITE:
                return f"{tenant}/{wd}/{site}"
        return ""

    # A leading xx-YY is a locale only when something follows it that is not the job marker;
    # `/xx-YY/job/...` is read as a site that looks like a locale; a lone `/xx-YY` returns ''.
    first = segs[0]
    if _LOCALE_RE.match(first) and len(segs) >= 2 and segs[1].lower() != "job":
        site = segs[1]
    elif _LOCALE_RE.match(first) and len(segs) == 1:
        return ""  # a lone locale-shaped segment is more likely a locale root than a site
    else:
        site = first
    if site.lower() in _NOT_A_SITE:
        return ""
    return f"{tenant}/{wd}/{site}"
