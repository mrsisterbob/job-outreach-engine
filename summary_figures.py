"""Figures quoted in a resume summary, and whether a rendered bullet carries each one.

Pure logic, no I/O. A headline number on the page with no bullet behind it is a claim a
reader cannot check; resume_engine uses this to swap a supporting bullet in, or to print the
summary without the figure.
"""
import re

_COUNT_RE = re.compile(r"\d[\d,]*\+")
_FROM_TO_RE = re.compile(r"(\d+)\s+minutes?\s+to\s+(\d+)", re.I)
_RANGE_HOUR_RE = re.compile(r"(\d+)-to-(\d+)-hour", re.I)


def summary_figures(summary: str) -> list:
    """Quoted figures in a summary, in order: counts ("2,000+"), "60 minutes to 20", "1-to-2-hour"."""
    text = str(summary or "")
    found = []
    for m in sorted(
        [(m.start(), m.group(0)) for rx in (_COUNT_RE, _FROM_TO_RE, _RANGE_HOUR_RE) for m in rx.finditer(text)]
    ):
        if m[1] not in found:
            found.append(m[1])
    return found


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower())


def figure_supported_by(figure: str, bullet: str) -> bool:
    """True when this one bullet carries the same figure."""
    text = _norm(bullet)
    fig = _norm(figure)
    m = _FROM_TO_RE.fullmatch(fig)
    if m:
        a, b = m.group(1), m.group(2)
        return any(re.search(p, text) for p in (
            rf"\b{a} minutes? to {b}\b",
            rf"\b{a} to {b} minutes?\b",
            rf"\b{b} minutes? from {a}\b",
        ))
    m = _RANGE_HOUR_RE.fullmatch(fig)
    if m:
        a, b = m.group(1), m.group(2)
        return bool(re.search(rf"\b{a}(?:-| )to(?:-| ){b}[- ]hour", text))
    return fig in text


def figure_supported(figure: str, bullets: list) -> bool:
    """True when any rendered bullet carries the figure."""
    return any(figure_supported_by(figure, b) for b in bullets or [])
