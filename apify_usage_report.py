"""Pure helpers for reading an Apify account-usage reply (no I/O, no network, no token).

Nothing here assumes a field name: field_paths() walks whatever the reply holds, money_candidates()
guesses which numeric fields might be dollars from their names, and scrub() makes a copy that is
safe to commit. A candidate is not confirmed until it matches the Apify console Billing page.
"""
import re

MONEY_WORDS = ("usd", "usage", "cost", "spent", "credit")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


def _type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list[%d]" % len(v)
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def field_paths(body, max_depth=5):
    """Sorted [(path, type_name, value_or_None)]. Value kept only for int, float and bool.

    Dotted paths; a list is reported as `path` (type list[N], N = item count) and by its FIRST item
    under `path[]`. The root is "$".
    """
    out = []

    def walk(v, path, depth):
        keep = v if isinstance(v, (bool, int, float)) else None
        out.append((path, _type_name(v), keep))
        if depth >= max_depth:
            return
        if isinstance(v, dict):
            for k in v:
                walk(v[k], "%s.%s" % (path, k) if path != "$" else str(k), depth + 1)
        elif isinstance(v, list) and v:
            walk(v[0], path + "[]", depth + 1)

    walk(body, "$", 0)
    return sorted(out, key=lambda t: t[0])


def money_candidates(paths):
    """Numeric paths whose last segment names usd/usage/cost/spent/credit. Candidates, not confirmed."""
    res = []
    for path, tname, value in paths:
        if tname not in ("int", "float"):
            continue
        last = path.replace("[]", "").rsplit(".", 1)[-1].lower()
        if any(w in last for w in MONEY_WORDS):
            res.append((path, tname, value))
    return res


def _scrub_str(s):
    if _ISO.match(s):
        return s
    if len(s) <= 24 and "@" not in s and "/" not in s:
        return s
    return "<str:%d>" % len(s)


def scrub(body):
    """Copy safe to commit: numbers, bools, nulls, dict keys, lists kept; free text replaced."""
    if isinstance(body, dict):
        return {k: scrub(v) for k, v in body.items()}
    if isinstance(body, list):
        return [scrub(v) for v in body]
    if isinstance(body, str):
        return _scrub_str(body)
    return body


def render_report(url_to_result):
    """Printable lines. A result is a dict: status (int or None), error (class name or None),
    json (bool), body (parsed JSON when json is true)."""
    lines = []
    for url, r in url_to_result.items():
        lines.append("== %s" % url)
        if r.get("error"):
            lines.append("  request failed: %s" % r["error"])
            continue
        lines.append("  HTTP status: %s" % r.get("status"))
        if not r.get("json"):
            lines.append("  body is not JSON")
            continue
        paths = field_paths(r.get("body"))
        for path, tname, value in paths:
            lines.append("  %s  %s%s" % (path, tname, "" if value is None else " = %r" % (value,)))
        cands = money_candidates(paths)
        if cands:
            lines.append("  money candidates (candidate, not confirmed):")
            for path, _t, value in cands:
                lines.append("    %s = %r" % (path, value))
        else:
            lines.append("  no numeric field name looks like a dollar figure")
    return lines
