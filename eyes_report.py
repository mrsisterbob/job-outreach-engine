"""Stored site visits -> the short "who just looked" view behind /eyes. Pure: no I/O, no `import main`, no clock
(the caller passes `now`).

Input rows are `(visited_at, visitor_id, path, referrer, bot, label)`: visited_at UTC text `YYYY-MM-DD HH:MM:SS`,
referrer a bare host or None, bot None for a person, label the tagged-link contact label or None. Neither the
visitor id nor the raw tag is ever put in the output.

Visitor ids are salted per day, so "visitors" in the 7d window counts a returning person once per day.
"""
from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timedelta, timezone

from run_report import chunk

MESSAGE_LIMIT = 3500
TOP_N = 5
RECENT_N = 10
TAGGED_N = 10
WINDOWS = (("24h", timedelta(hours=24)), ("7d", timedelta(days=7)))
MAX_TEXT = 80


def _parse(visited_at):
    try:
        return datetime.strptime(visited_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _top(counter, n=TOP_N):
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def build_eyes(rows, now):
    """Report dict: windows {"24h"/"7d": visitors, views, pages, sources, tagged, excluded_views}, recent, skipped_rows."""
    parsed, skipped = [], 0
    for row in rows or []:
        try:
            visited_at, vid, path, referrer, bot, label = row
        except (TypeError, ValueError):
            skipped += 1
            continue
        when = _parse(visited_at)
        if when is None:
            skipped += 1
            continue
        parsed.append((when, vid, path or "/", (referrer or "").strip() or None, bot, label))
    windows = {}
    for name, span in WINDOWS:
        start = now - span
        inside = [r for r in parsed if r[0] >= start]
        human = [r for r in inside if r[4] is None]
        page_people, source_people = {}, {}
        tagged = Counter()
        for when, vid, path, ref, _bot, label in human:
            page_people.setdefault(path, set()).add(vid)
            source_people.setdefault(ref or "direct", set()).add(vid)
            if label:
                tagged[label] += 1
        windows[name] = {
            "visitors": len({r[1] for r in human}),
            "views": len(human),
            "pages": _top({k: len(v) for k, v in page_people.items()}),
            "sources": _top({k: len(v) for k, v in source_people.items()}),
            "tagged": dict(sorted(tagged.items(), key=lambda kv: (-kv[1], kv[0]))),
            "excluded_views": len(inside) - len(human),
        }
    humans = sorted((r for r in parsed if r[4] is None), key=lambda r: r[0], reverse=True)[:RECENT_N]
    recent = [{"visited_at": r[0].strftime("%Y-%m-%d %H:%M:%S"), "path": r[2], "referrer": r[3], "label": r[5]}
              for r in humans]
    return {"windows": windows, "recent": recent, "skipped_rows": skipped}


def _e(text):
    text = str(text)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT - 3] + "..."
    return html.escape(text)


def _ranked(pairs):
    return ", ".join(f"{_e(k)} ({v})" for k, v in pairs) if pairs else "none"


def render_eyes(report, when_text_fn):
    """A list of Telegram HTML strings, each under MESSAGE_LIMIT characters."""
    lines = ["<b>Who looked at montelattice.com</b>"]
    for name, _span in WINDOWS:
        w = report["windows"][name]
        lines.append("")
        label = "Last 24 hours" if name == "24h" else "Last 7 days"
        if w["views"] == 0:
            lines.append(f"<b>{label}</b>: no visits")
        else:
            vw, pv = w["visitors"], w["views"]
            lines.append(f"<b>{label}</b>: {vw} {'visitor' if vw == 1 else 'visitors'}, "
                         f"{pv} {'pageview' if pv == 1 else 'pageviews'}")
            lines.append("Pages: " + _ranked(w["pages"]))
            lines.append("From: " + _ranked(w["sources"]))
        if w["tagged"]:
            items = list(w["tagged"].items())
            text = "Tagged links: " + ", ".join(f"{_e(k)} ({v})" for k, v in items[:TAGGED_N])
            if len(items) > TAGGED_N:
                text += f", and {len(items) - TAGGED_N} more"
            lines.append(text)
        else:
            lines.append("Tagged links: no visits")
        if w["excluded_views"]:
            lines.append(f"{w['excluded_views']} bot or own-device views not counted")
    lines.append("")
    lines.append("<b>Most recent visits</b>")
    if report["recent"]:
        for r in report["recent"]:
            bit = f"{_e(when_text_fn(r['visited_at']))} - {_e(r['path'])} from {_e(r['referrer'] or 'direct')}"
            if r["label"]:
                bit += f" [{_e(r['label'])}]"
            lines.append(bit)
    else:
        lines.append("no visits")
    lines.append("")
    lines.append("7d counts a returning person once per day.")
    if report["skipped_rows"]:
        lines.append(f"{report['skipped_rows']} rows with an unreadable time were skipped.")
    return chunk("\n".join(lines), MESSAGE_LIMIT)
