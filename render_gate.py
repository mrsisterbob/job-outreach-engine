"""Pure render gate: detects banned / AI-tell words in resume text. No I/O, never mutates text."""
import re

_STEM_PATTERNS = (
    r"\bensur\w*", r"\bstreamlin\w*", r"\bend[- ]to[- ]end\b", r"\borchestrat\w*",
    r"\butiliz\w*", r"\bleverag\w*", r"\bspearhead\w*",
)


def banned_hits(text: str, banned_words=None) -> list:
    """Returns the sorted unique banned words/phrases found in text (lowercased); empty if clean."""
    patterns = list(_STEM_PATTERNS)
    for w in banned_words or []:
        w = str(w).strip()
        if w:
            patterns.append(r"\b" + re.escape(w) + r"\w*")
    hits = set()
    for p in patterns:
        for m in re.finditer(p, str(text or ""), flags=re.IGNORECASE):
            hits.add(m.group(0).lower())
    return sorted(hits)
