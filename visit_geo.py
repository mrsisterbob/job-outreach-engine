"""Coarse visitor geo lookup: pick the provider, build the request, parse the reply.

Pure logic: no I/O, no logging, no disk. The caller (not this module) does the
requests.get, with timeout=TIMEOUT_SECONDS.

Provider: ipwho.is (ipwhois.io free endpoint). HTTPS works, no key, commercial use
allowed on the free tier, 1,000 requests/day per client IP (provider docs, not
billed or load-tested by us). The raw IP appears only in the URL lookup_url returns.
Field names below come from visit_geo_sample.json, a real reply for 8.8.8.8.
"""
import ipaddress
import re
from typing import Any, NamedTuple, Optional

TIMEOUT_SECONDS = 3
_BASE = "https://ipwho.is/"
_FIELDS = "success,message,city,region,connection"

# Field names as seen in the saved sample.
KEY_OK = "success"
KEY_CITY = "city"
KEY_REGION = "region"
KEY_CONN = "connection"
KEY_ORG = "org"
KEY_ISP = "isp"


class Geo(NamedTuple):
    place: str
    org: str


UNKNOWN = Geo("", "")

# HEURISTIC, not a measurement: consumer/mobile ISPs and hosting/cloud providers.
# Matched case-insensitively as regexes. Every pattern is word-bounded (\b) and the
# ambiguous names are anchored to the ISP form only: "Spectrum" alone or "Spectrum
# Business", "Charter Communications", "Cox Communications", "Google LLC" alone.
# So "Spectrum Health" and "Cox Automotive" stay companies. A miss shows an ISP name
# as the company.
NON_COMPANY_PATTERNS = (
    r"\bcomcast\b", r"\bxfinity\b", r"\bat&t\b", r"\batt\b", r"\bverizon\b",
    r"\bcharter communications\b", r"^spectrum( business)?$", r"\bt-mobile\b",
    r"\bfrontier communications\b", r"\bcox communications\b", r"\bcenturylink\b",
    r"\blumen\b", r"\blevel 3\b", r"\bgoogle fiber\b", r"\bmediacom\b",
    r"\bamazon\b", r"\baws\b", r"\bgoogle cloud\b", r"^google( llc| inc\.?)?$",
    r"\bmicrosoft\b", r"\bazure\b", r"\bdigital ?ocean\b", r"\bovh\b",
    r"\bhetzner\b", r"\bcloudflare\b", r"\blinode\b", r"\bakamai\b",
    r"\boracle\b", r"\balibaba\b", r"\bvultr\b", r"\bleaseweb\b", r"\bm247\b",
    r"\bhosting\b", r"\bdata ?center\b", r"\bvpn\b",
)
_NON_COMPANY_RE = re.compile("|".join(NON_COMPANY_PATTERNS), re.IGNORECASE)


def lookup_url(ip: Any) -> Optional[str]:
    """URL for requests.get, or None unless ip is a globally routable address."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except (ValueError, TypeError):
        return None
    if not addr.is_global or addr.is_multicast:
        return None
    return f"{_BASE}{addr.compressed}?fields={_FIELDS}"


def company_org(name: Any) -> str:
    """The name, or "" for a consumer/mobile ISP or hosting/cloud provider."""
    if not isinstance(name, str):
        return ""
    name = name.strip()
    if not name or _NON_COMPANY_RE.search(name):
        return ""
    return name


def parse_reply(status_code: Any, body: Any) -> Geo:
    """Turn a provider reply into Geo(place, org). Any failure is Geo("", "")."""
    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        return UNKNOWN
    if not isinstance(body, dict) or body.get(KEY_OK) is not True:
        return UNKNOWN
    city = body.get(KEY_CITY)
    region = body.get(KEY_REGION)
    city = city.strip() if isinstance(city, str) else ""
    region = region.strip() if isinstance(region, str) else ""
    if not city and not region:
        return UNKNOWN
    place = f"{city}, {region}" if city and region else (city or region)
    conn = body.get(KEY_CONN)
    org = ""
    if isinstance(conn, dict):
        raw = conn.get(KEY_ORG)
        if isinstance(raw, str) and raw.strip():
            org = company_org(raw)  # a blocked org never falls through to isp
        else:
            org = company_org(conn.get(KEY_ISP))
    return Geo(place, org)
