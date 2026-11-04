"""Pure census of one ATS-resolver actor reply: which keys came back, how often, what types.

No I/O, no clock. Prints key names, counts and short values only, never a description or a whole row.
"""

NAMED_KEYS = ("atsPlatform", "boardToken", "boardUrl")
NAME_HINTS = ("company", "name", "domain")
CUT = 80
MAX_VALUES = 10


def _cut(value):
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= CUT else text[:CUT] + "..."


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _empty(note):
    return {"row_count": 0, "keys": {}, "ats_platforms": {}, "board_tokens": {}, "board_urls": {}, "board_token_rows": 0,
            "board_url_rows": 0, "blank_token_rows": 0, "sample": {}, "note": note}


def census(rows):
    """Census dict for a reply. Anything that is not a non-empty list of dicts gets a note, never a crash."""
    if not isinstance(rows, list):
        return _empty("reply is not a list (got %s)" % type(rows).__name__)
    if not rows:
        return _empty("reply is an empty list: zero rows")
    if not all(isinstance(r, dict) for r in rows):
        return _empty("reply list holds non-dict items; no census")
    keys = {}
    platforms = {}
    tokens = {}
    urls = {}
    token_rows = url_rows = blank_rows = 0
    for row in rows:
        for k, v in row.items():
            slot = keys.setdefault(k, {"present": 0, "types": set()})
            slot["present"] += 1
            slot["types"].add(type(v).__name__)
        if "atsPlatform" in row:
            label = _cut(row["atsPlatform"])
            platforms[label] = platforms.get(label, 0) + 1
        if "boardToken" in row:
            if _blank(row["boardToken"]):
                blank_rows += 1
            else:
                token_rows += 1
                label = _cut(row["boardToken"])
                tokens[label] = tokens.get(label, 0) + 1
        if "boardUrl" in row and not _blank(row["boardUrl"]):
            url_rows += 1
            label = _cut(row["boardUrl"])
            urls[label] = urls.get(label, 0) + 1
    sample = {}
    for k, v in rows[0].items():
        low = str(k).lower()
        if k in NAMED_KEYS or any(h in low for h in NAME_HINTS):
            sample[k] = _cut(v)
    return {
        "row_count": len(rows),
        "keys": {k: {"present": s["present"], "types": sorted(s["types"])} for k, s in keys.items()},
        "ats_platforms": platforms,
        "board_tokens": tokens,
        "board_urls": urls,
        "board_token_rows": token_rows,
        "board_url_rows": url_rows,
        "blank_token_rows": blank_rows,
        "sample": sample,
        "note": "",
    }


def _tally_lines(tally):
    ordered = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    lines = ["  %s: %d" % (v, n) for v, n in ordered[:MAX_VALUES]]
    if len(ordered) > MAX_VALUES:
        lines.append("  +%d more" % (len(ordered) - MAX_VALUES))
    return lines


def render_census(c):
    """Printable lines for a census dict."""
    lines = ["rows: %d" % c["row_count"]]
    if c.get("note"):
        lines.append("note: " + c["note"])
        return lines
    lines.append("keys (present in N rows, types):")
    for k, s in c["keys"].items():
        lines.append("  %s: %d rows, %s" % (_cut(k), s["present"], "/".join(s["types"])))
    lines.append("atsPlatform values:")
    lines.extend(_tally_lines(c["ats_platforms"]))
    lines.append("boardToken rows (non-blank): %d, blank: %d" % (c["board_token_rows"], c["blank_token_rows"]))
    lines.append("boardToken values:")
    lines.extend(_tally_lines(c["board_tokens"]))
    lines.append("boardUrl rows (non-blank): %d" % c["board_url_rows"])
    lines.append("boardUrl values:")
    lines.extend(_tally_lines(c["board_urls"]))
    lines.append("first row sample (values cut at %d chars):" % CUT)
    for k, v in c["sample"].items():
        lines.append("  %s = %s" % (_cut(k), v))
    return lines
