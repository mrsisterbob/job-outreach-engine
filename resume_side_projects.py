"""Pure selection of which evidence_bank side_projects print on a track's resume.

No I/O, no imports from main or resume_engine. The renderer calls
side_projects_for_track() and prints whatever it returns.
"""

# track code -> side project names, in print order. Tracks not listed print none
# (a, c, e, f, g are finance-framed or operations tracks with no room).
# "Spotify playlist project" is deliberately in no map.
SIDE_PROJECT_FIT = {
    "d": ("Budget tracker", "Personal tools"),
    "b": ("Budget tracker", "Montelattice site"),
    "h": ("Montelattice site", "Dropshipping site for a friend's China-based business"),
}


def side_projects_for_track(side_projects, track, limit=2):
    """Return the side_projects entries (dicts, unchanged) that fit `track`, at most `limit`.

    Never raises on bad shapes: a non-list input, non-dict items, and entries whose
    bullets are missing or not a list are skipped.
    """
    if not isinstance(side_projects, list):
        return []
    try:
        names = SIDE_PROJECT_FIT.get(str(track).strip().lower(), ())
        limit = int(limit)
    except (TypeError, ValueError):
        return []
    by_name = {}
    for entry in side_projects:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("bullets"), list):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name not in by_name:
            by_name[name] = entry
    out = []
    for name in names:
        if len(out) >= limit:
            break
        if name in by_name:
            out.append(by_name[name])
    return out
