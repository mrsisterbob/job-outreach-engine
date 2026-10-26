"""Pure helpers for the /w Michigan-only rule (no I/O).

The Michigan test itself lives in main.passes_strict_filter(check_geography=True); this module only
holds the Remote guard that rule cannot express ("Remote - Michigan" contains "michigan")."""
import re

_REMOTE = re.compile(r"\bremote\b", re.I)
_ONSITE_OR_HYBRID = re.compile(r"\bhybrid\b|\bon[\s-]?site\b", re.I)

# Plain-words labels for the role-screen reasons the location rule produces.
REASON_WORDS = {
    "out_of_state": "outside Michigan",
    "city_allowlist": "no Michigan location found",
}


def is_pure_remote(role):
    """True when the role is Remote with no on-site/hybrid wording. Never raises."""
    try:
        if not isinstance(role, dict):
            return False
        city = role.get("job_city")
        text = city if isinstance(city, str) else ""
        if _ONSITE_OR_HYBRID.search(text):
            return False
        return bool(role.get("job_is_remote")) or bool(_REMOTE.search(text))
    except Exception:
        return False


def reason_words(reason):
    """Plain-words label for a screen reason code (unknown codes pass through)."""
    return REASON_WORDS.get(reason, reason)
