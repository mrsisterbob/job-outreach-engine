"""Stored pageviews -> a source-by-destination report. Pure: no I/O, no `import main`, no clock (the caller passes `now`).

Input rows are `(visited_at, visitor_id, path, referrer, bot)` exactly as `site_visits` stores them: visited_at UTC text
`YYYY-MM-DD HH:MM:SS`, referrer a bare host or None, bot None for a person (else self / health / crawler / probe /
preview / preview:<App>).

DECISIONS, stated once:
* A visitor is a VISITOR-DAY: key (local day, visitor_id). The id is salted per day on purpose, so the same person on
  three days is three visitors. Ids are never linked, printed or stored in the report.
* The SOURCE of a visitor-day is the referrer of its EARLIEST view that day (ties: input order). Its PAGES are the set
  of pages it viewed at any time that day, the landing page included ("viewed", not "went on to": order is not used).
* Whether a visitor-day is a person or a bot is also decided by that earliest view.
* Contact form submissions are not pageviews: they come from `lead_times` and are shown as one separate line, never
  attributed to a source (lead rows carry no visitor id).
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from pipeline_utils import is_job_market_referrer, is_site_page  # read-only reuse
from run_report import chunk

RETENTION_DAYS = 90  # mirrors ANALYTICS_RETENTION_DAYS in main.py
MAX_DAYS = RETENTION_DAYS
WEEK_AFTER_DAYS = 14  # DESIGN CHOICE, not a measurement: longer windows are shown per week
MAX_MESSAGES = 3  # DESIGN CHOICE, not a measurement
MESSAGE_LIMIT = 3500  # below Telegram's 4,096-character limit, with room
TOP_HOSTS = 3

HUMAN_CATEGORIES = ("direct", "linkedin", "search", "mail_chat", "other_sites")
BOT_CATEGORIES = ("self", "preview", "crawler", "probe", "health")
CATEGORIES = HUMAN_CATEGORIES + BOT_CATEGORIES
CATEGORY_LABELS = {"direct": "Direct", "linkedin": "LinkedIn", "search": "Search", "mail_chat": "Email and chat",
                   "other_sites": "Other sites", "self": "You (self)", "preview": "Link previews",
                   "crawler": "Crawlers", "probe": "Probes", "health": "Health checks"}
PAGE_COLUMNS = ("home", "resume", "job_engine", "docfiler", "other")
PAGE_LABELS = {"home": "Home", "resume": "Resume", "job_engine": "Job Engine", "docfiler": "Docfiler", "other": "Other"}

# Host lists: a guess, not a measurement. A host in none of them lands in other_sites, which prints its top hosts so a
# misfiled one is visible.
_LINKEDIN_ALIASES = frozenset({"lnkd.in", "com.linkedin.android"})
_SEARCH_HOSTS = frozenset({"bing.com", "duckduckgo.com", "search.yahoo.com", "yahoo.com", "ecosia.org",
                           "search.brave.com", "baidu.com", "kagi.com", "startpage.com"})
_SEARCH_PATTERNS = (re.compile(r"^(www\.)?google\.[a-z.]+$"), re.compile(r"^(www\.)?yandex\.[a-z.]+$"))
_MAIL_CHAT_HOSTS = frozenset({"mail.google.com", "com.google.android.gm", "outlook.live.com", "outlook.office.com",
                              "outlook.office365.com", "mail.yahoo.com", "mail.proton.me", "web.whatsapp.com", "t.me",
                              "web.telegram.org", "app.slack.com", "slack.com", "teams.microsoft.com", "discord.com",
                              "messenger.com"})

CANNOT_KNOW = (
    "Direct also holds visits that arrived from your own pages: the stored referrer drops same-site hosts, so "
    "own-site navigation and a typed address look the same.",
    "Direct also holds visits from apps that send no referrer: email apps, most messaging apps, PDFs, and browsers "
    "with referrer stripping.",
    "Visitors are counted per day: the id changes every day on purpose, so the same person on three days counts "
    "three times and a returning visitor cannot be told from a new one.",
    "The referrer is only a host: no search words, no LinkedIn post, no email subject.",
    "A bot that sends a normal browser identity is counted as a person; a person on an odd browser may be filed as a bot.",
    "Viewed means opened at some point that day; the order of pages and time spent are not used.",
    "Link-preview fetches show that a link was pasted into an app, not that anyone opened it.",
    "Contact form submissions carry no visitor id, so they cannot be placed under a source.",
    f"Nothing older than the retention window exists ({RETENTION_DAYS} days by default).",
)


def _esc(value):
    return html.escape(str(value or ""), quote=False)


def _parse_utc(text):
    """Aware-UTC datetime from `YYYY-MM-DD HH:MM:SS` (or ISO), else None."""
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _zone(tz_name):
    """(tzinfo, fallback_flag): the named zone, or UTC when the zone database cannot supply it."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name), False
    except Exception:
        return timezone.utc, True


def _page_column(path):
    p = path.strip() if isinstance(path, str) else ""
    p = p.split("?", 1)[0].split("#", 1)[0] or "/"
    if len(p) > 1:
        p = p.rstrip("/") or "/"
    if p == "/":
        return "home"
    if path_is_resume(p):
        return "resume"
    if p == "/job-engine":
        return "job_engine"
    if p == "/docfiler":
        return "docfiler"
    return "other"


def path_is_resume(p):
    return p.startswith("/resume/") or p == "/resume"


def _host_category(host):
    host = str(host or "").strip().lower()
    if not host:
        return "direct"
    if host == "linkedin.com" or host.endswith(".linkedin.com") or host in _LINKEDIN_ALIASES:
        return "linkedin"
    if host in _SEARCH_HOSTS or any(host.endswith("." + h) for h in _SEARCH_HOSTS) or any(p.match(host) for p in _SEARCH_PATTERNS):
        return "search"
    if host in _MAIL_CHAT_HOSTS:
        return "mail_chat"
    return "other_sites"


def _bot_category(bot):
    """(category, app) for a stored bot flag; an unknown string counts as a crawler."""
    b = str(bot or "").strip()
    if b == "self" or b == "health" or b == "probe" or b == "crawler":
        return b, ""
    if b == "preview" or b.startswith("preview:"):
        return "preview", b.partition(":")[2].strip()
    return "crawler", ""


def _empty_category():
    return {"visitors": 0, "hits": 0, "pages": {c: 0 for c in PAGE_COLUMNS}, "per_day": [], "top_hosts": [], "apps": {}}


def build_report(rows, now, days=7, lead_times=None, tz_name="America/Detroit"):
    """The report dict (JSON-safe, no visitor ids). `days` is clamped to 1-90. Bad rows are counted in skipped_rows and
    never raise. now: an aware datetime (the caller's clock)."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(MAX_DAYS, days))
    tz, tz_fallback = _zone(tz_name)
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    today = now.astimezone(tz).date()
    window = [today - timedelta(days=days - 1 - i) for i in range(days)]
    index = {d: i for i, d in enumerate(window)}

    skipped = 0
    parsed = []   # (local_day, order, visitor_id, page_column, referrer, bot)
    for order, row in enumerate(rows or []):
        try:
            visited_at, visitor_id, path, referrer, bot = row
            dt = _parse_utc(visited_at)
            if dt is None or not isinstance(visitor_id, str) or not visitor_id:
                raise ValueError("bad row")
        except (TypeError, ValueError):
            skipped += 1
            continue
        day = dt.astimezone(tz).date()
        if day not in index:
            continue   # outside the window: not a bad row, just not this report's
        parsed.append((day, dt, order, visitor_id, _page_column(path), referrer if isinstance(referrer, str) else "", bot))

    # Earliest view of each visitor-day decides its source (ties: input order).
    parsed.sort(key=lambda r: (r[0], r[1], r[2]))
    visitor_days = {}
    for day, _dt, _order, vid, page, referrer, bot in parsed:
        entry = visitor_days.get((day, vid))
        if entry is None:
            if bot:
                category, app = _bot_category(bot)
            else:
                category, app = _host_category(referrer), ""
            entry = visitor_days[(day, vid)] = {"category": category, "host": (referrer or "").lower(), "pages": set(), "hits": 0, "apps": {}}
        entry["pages"].add(page)
        entry["hits"] += 1
        if bot and _bot_category(bot)[0] == "preview":
            app = _bot_category(bot)[1]
            if app:
                entry["apps"][app] = entry["apps"].get(app, 0) + 1

    categories = {c: _empty_category() for c in CATEGORIES}
    for c in CATEGORIES:
        categories[c]["per_day"] = [0] * days
    hosts = {c: {} for c in CATEGORIES}
    for (day, _vid), entry in visitor_days.items():
        cat = categories[entry["category"]]
        cat["visitors"] += 1
        cat["hits"] += entry["hits"]
        cat["per_day"][index[day]] += 1
        for page in entry["pages"]:
            cat["pages"][page] += 1
        for app, n in entry["apps"].items():
            cat["apps"][app] = cat["apps"].get(app, 0) + n
        if entry["host"] and entry["category"] in ("linkedin", "search", "mail_chat", "other_sites"):
            hosts[entry["category"]][entry["host"]] = hosts[entry["category"]].get(entry["host"], 0) + 1
    for c in CATEGORIES:
        ranked = sorted(hosts[c].items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_HOSTS]
        categories[c]["top_hosts"] = [[h, n, bool(is_job_market_referrer(h))] for h, n in ranked]

    lead_count = None
    if lead_times is not None:
        lead_count = 0
        for ts in lead_times:
            dt = _parse_utc(ts)
            if dt is not None and dt.astimezone(tz).date() in index:
                lead_count += 1

    total = sum(categories[c]["visitors"] for c in CATEGORIES)
    return {"days": days, "window": [d.isoformat() for d in window], "tz_name": "UTC" if tz_fallback else tz_name,
            "tz_fallback": tz_fallback, "skipped_rows": skipped, "visitors_total": total, "hits_total": len(parsed),
            "lead_count": lead_count, "categories": categories, "empty": not parsed}


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _trend_line(per_day, days):
    if days <= WEEK_AFTER_DAYS:
        return "per day: " + " ".join(str(n) for n in per_day)
    sizes, left = [], days
    while left > 0:   # buckets of 7 ending today; the oldest one may be partial
        sizes.insert(0, min(7, left))
        left -= 7
    out, pos = [], 0
    for size in sizes:
        out.append(sum(per_day[pos:pos + size]))
        pos += size
    note = f"; oldest bucket is {sizes[0]} days" if sizes[0] != 7 else ""
    return "per week (7 days each, ending today" + note + "): " + " ".join(str(n) for n in out)


def _category_lines(name, data, report):
    label = CATEGORY_LABELS[name]
    if name in BOT_CATEGORIES:
        head = f"{label}: {_plural(data['visitors'], 'visitor-day')} · {_plural(data['hits'], 'hit')}"
        if name == "preview" and data["apps"]:
            head += " (" + ", ".join(f"{_esc(a)} {n}" for a, n in sorted(data["apps"].items(), key=lambda kv: (-kv[1], kv[0]))) + ")"
        lines = [head]
    else:
        pages = " · ".join(f"{PAGE_LABELS[c]} {data['pages'][c]}" for c in PAGE_COLUMNS)
        lines = [f"{label}: {_plural(data['visitors'], 'visitor')} · {pages}"]
        if data["top_hosts"]:
            lines.append("top hosts: " + ", ".join(("★" if starred else "") + f"{_esc(h)} {n}" for h, n, starred in data["top_hosts"]))
    lines.append(_trend_line(data["per_day"], report["days"]))
    return lines


def render_report(report):
    """A list of at most 3 message strings (3,500 characters each, split on line boundaries so never inside an HTML tag).
    Every render ends with the fixed what-this-cannot-know block."""
    days = report["days"]
    zone_text = "days in UTC" if report.get("tz_fallback") else f"days in {_esc(report['tz_name'])}"
    lines = [f"<b>montelattice.com, last {days} day{'' if days == 1 else 's'} (today is partial)</b>, {zone_text}"]
    if report["empty"]:
        lines.append(f"No pageviews stored for this window (pageviews are kept {RETENTION_DAYS} days).")
    else:
        lines.append(f"{_plural(report['visitors_total'], 'visitor-day')} in all (one person on three days counts three times).")
        empty = []
        for title, group in (("People", HUMAN_CATEGORIES), ("Bots and scanners", BOT_CATEGORIES)):
            lines.append(f"<b>{title}</b>")
            for name in group:
                data = report["categories"][name]
                if data["hits"] == 0:
                    empty.append(CATEGORY_LABELS[name])
                    continue
                lines += _category_lines(name, data, report)
        if empty:
            lines.append("none: " + ", ".join(empty))
    if report["lead_count"] is None:
        lines.append("Contact form submissions: unknown (could not be read)")
    else:
        lines.append(f"Contact form submissions: {report['lead_count']} (cannot be tied to a source)")
    if report["skipped_rows"]:
        lines.append(f"{report['skipped_rows']} unreadable rows skipped")
    lines.append("<b>What this cannot know</b>")
    lines += [f"{i}. {text}" for i, text in enumerate(CANNOT_KNOW, 1)]
    return chunk("\n".join(lines), MESSAGE_LIMIT)[:MAX_MESSAGES]
