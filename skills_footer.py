"""Pure logic: surface banked skills a job posting names in a track's two skills-footer lines.

No I/O, no imports from main or resume_engine. Never edits or removes base-line items.
"""
import re

_ALIASES = {
    "looker studio": ("Data Studio",),
    "power bi": ("PowerBI",),
    "google ads": ("AdWords",),
    "meta ads": ("Facebook Ads",),
}

# Skills that belong on the analytics line; every other banked skill goes to the systems line.
_ANALYSIS = {s.lower() for s in (
    "Tableau", "Looker Studio", "Power BI", "XLOOKUP", "VLOOKUP", "Power Query", "Excel",
    "SQL", "Google Sheets", "Google Ads", "Meta Ads",
)}
_SYSTEMS_LABEL = ("system", "tool", "platform", "stack")
_ANALYTICS_LABEL = ("analy", "report", "data", "insight", "metric")


def _pattern(name):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _first_pos(text, skill):
    best = None
    for n in (skill,) + _ALIASES.get(skill.lower(), ()):
        m = _pattern(n).search(text)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best


def _line_index(labels, analysis):
    keys = _ANALYTICS_LABEL if analysis else _SYSTEMS_LABEL
    for i, lab in enumerate(labels):
        if any(k in lab.lower() for k in keys):
            return i
    return 0 if analysis else len(labels) - 1


def surface_banked_skills(job_text, banked_skills, base_lines, max_added=6, max_line_chars=None):
    """Return base_lines [(label, desc)] with banked skills named in job_text appended."""
    lines = [(l, d) for l, d in base_lines]
    if not job_text or not banked_skills or not lines or max_added <= 0:
        return lines
    present = " ".join(d for _, d in lines).lower()
    found = []
    for skill in banked_skills:
        if not skill or _pattern(skill).search(present):
            continue
        pos = _first_pos(job_text, skill)
        if pos is not None:
            found.append((pos, skill))
    found.sort(key=lambda t: t[0])
    labels = [l for l, _ in lines]
    descs = [d for _, d in lines]
    added = 0
    for _, skill in found:
        if added >= max_added:
            break
        i = _line_index(labels, skill.lower() in _ANALYSIS)
        base = descs[i].rstrip()
        trailing = "." if base.endswith(".") else ""
        body = base[:-1] if trailing else base
        cand = f"{body}, {skill}{trailing}"
        if max_line_chars is not None and len(f"{labels[i]}: {cand}") > max_line_chars:
            continue
        descs[i] = cand
        added += 1
    return list(zip(labels, descs))
