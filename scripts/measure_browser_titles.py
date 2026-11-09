"""Measure how much browser time a window-title keyword list would move out of "Browsing".

  python scripts/measure_browser_titles.py [--week current|last] [--keywords FILE] [--top N]
                                           [--show-titles N] [--aw-url URL]

Read-only: one query to the local ActivityWatch, a report on the terminal, nothing saved or sent.
By default only aggregate hours and single words are printed; whole titles appear only with
--show-titles. Exit: 0 report printed, 1 ActivityWatch unreachable or unreadable, 2 bad usage.
"""
import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import activity_snapshot as act  # noqa: E402
import push_actual_snapshot as pas  # noqa: E402
import local_http  # noqa: E402

MAX_BUCKETS = 20
MAX_KEYWORDS = 60
KW_MIN, KW_MAX = 3, 40
NAME_MAX = 40
TITLE_CUT = 60
_WORD_RE = re.compile(r"[a-z]{4,}")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

STOPWORDS = frozenset({
    "microsoft", "edge", "personal", "work", "profile", "google", "chrome", "firefox", "mozilla",
    "browser", "window", "windows", "tab", "tabs", "page", "home", "new", "this", "that", "with",
    "from", "your", "have", "more", "about", "into", "than", "then", "they", "will", "what",
    "when", "which", "were", "been", "also", "just", "only", "does", "https", "http", "www",
})

KW_BAD = ("Keyword file refused: it must be a JSON object of at most 20 buckets and 60 keywords "
          "in total, each keyword 3 to 40 characters. Nothing was fetched.")
UNDER_HALF_NOTE = ("Title keywords explain less than half of this browser time. Window titles carry "
                   "page names, not site names. A browser extension that records the page address "
                   "(aw-watcher-web) would give a domain to classify by.")
FINAL_LINE = ("Nothing was saved or sent. These hours come from your own PC; paste only what "
              "you are comfortable sharing.")


def _aw_post_default(url, **kw):
    return local_http.local_post(url, **kw)


def aw_url_ok(url):
    """Same-machine http only (the engine URL check with loopback hosts); any whitespace or
    control character is refused outright because urlparse silently drops newlines and tabs."""
    if not isinstance(url, str) or _CTRL_RE.search(url) or any(c.isspace() for c in url):
        return False
    return pas.url_ok(url, http_hosts=("localhost", "127.0.0.1", "::1"), allow_https=False)


def _collapse(s):
    return " ".join(s.split())


def _parse_z(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_title_query():
    q = list(act.AW_QUERY)
    assert q[3] == 'ev = merge_events_by_keys(win, ["app"]);', "AW_QUERY changed shape"
    q[3] = 'ev = merge_events_by_keys(win, ["app","title"]);'
    return q


def load_keywords(path):
    """-> list of (bucket, [keywords]) in file order, or None when the file is refused.
    Never echoes the file's contents."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict) or not raw or len(raw) > MAX_BUCKETS:
        return None
    out, total = [], 0
    for name, kws in raw.items():
        if not isinstance(name, str) or not 1 <= len(_collapse(name)) <= NAME_MAX:
            return None
        if not isinstance(kws, list):
            return None
        clean = []
        for k in kws:
            if not isinstance(k, str):
                return None
            k = _collapse(k).lower()
            if not KW_MIN <= len(k) <= KW_MAX:
                return None
            clean.append(k)
        total += len(clean)
        if total > MAX_KEYWORDS:
            return None
        out.append((_collapse(name), clean))
    return out


def browser_titles(result):
    """AW reply -> (rows, unreadable) with rows = {title: seconds}. Returns None when the reply
    is not a list of event lists. Only app, title and duration are read."""
    if not isinstance(result, list) or not all(isinstance(i, list) for i in result):
        return None
    rows, bad = {}, 0
    for inner in result:
        for ev in inner:
            data = ev.get("data") if isinstance(ev, dict) else None
            app = data.get("app") if isinstance(data, dict) else None
            title = data.get("title") if isinstance(data, dict) else None
            dur = ev.get("duration") if isinstance(ev, dict) else None
            if (not isinstance(app, str) or not isinstance(title, str) or isinstance(dur, bool)
                    or not isinstance(dur, (int, float)) or not math.isfinite(dur) or dur < 0):
                bad += 1
                continue
            a = app.strip().lower()
            if a in act.IGNORED_APPS or act.DEFAULT_APP_BUCKETS.get(a) != "Browsing":
                continue
            t = _collapse(title)
            rows[t] = min(rows.get(t, 0.0) + float(dur), act._MAX_SECONDS)
    return rows, bad


def top_words(rows):
    secs = {}
    for t, s in rows.items():
        for w in set(_WORD_RE.findall(t.lower())) - STOPWORDS:
            secs[w] = secs.get(w, 0.0) + s
    return sorted(secs.items(), key=lambda kv: (-kv[1], kv[0]))


def match_buckets(rows, keywords):
    """First match wins. -> ({bucket: seconds}, unmatched seconds)."""
    per = {name: 0.0 for name, _ in keywords}
    left = 0.0
    for t, s in rows.items():
        low = t.lower()
        for name, kws in keywords:
            if any(k in low for k in kws):
                per[name] += s
                break
        else:
            left += s
    return per, left


def _h(sec):
    return f"{sec / 3600:.2f}"


def main(argv=None, aw_post=None, now=None, out=print):
    ap = argparse.ArgumentParser(description="Browser time by window-title keyword (read-only).")
    ap.add_argument("--week", choices=["current", "last"], default="current")
    ap.add_argument("--keywords")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--show-titles", type=int, default=0)
    ap.add_argument("--aw-url", default="http://localhost:5600")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    top_n = min(max(args.top, 1), 100)
    show_n = min(max(args.show_titles, 0), 50)

    if not aw_url_ok(args.aw_url):
        out("Refusing --aw-url: only http://localhost, 127.0.0.1 or [::1] is accepted. "
            "Nothing was fetched.")
        return 2
    keywords = None
    if args.keywords is not None:
        keywords = load_keywords(args.keywords)
        if keywords is None:
            out(KW_BAD)
            return 2
    aw = args.aw_url.rstrip("/")
    aw_post = aw_post or _aw_post_default

    now = now or datetime.now(timezone.utc)
    bounds = act.week_bounds(now)
    if args.week == "last":
        bounds = act.week_bounds(_parse_z(bounds["start"]) - timedelta(hours=1))
    body = {"timeperiods": [f"{bounds['start']}/{bounds['end']}"], "query": build_title_query()}

    try:
        resp = aw_post(aw + "/api/0/query/", json=body, timeout=30)
        status = resp.status_code
        result = resp.json() if status == 200 else None
    except Exception as e:
        if isinstance(e, ConnectionError) or type(e).__name__ == "ConnectionError":
            out(f"ActivityWatch is not running at {aw}.")
        else:
            out(f"ActivityWatch query failed: {type(e).__name__}")
        return 1
    if status != 200:
        out(f"ActivityWatch answered HTTP {status} to the query.")
        return 1
    parsed = browser_titles(result)
    if parsed is None:
        out("Unexpected reply from ActivityWatch; nothing was counted.")
        return 1
    if not any(result):
        out("ActivityWatch returned no events for this week.")
        out(FINAL_LINE)
        return 0
    rows, bad = parsed

    total = sum(rows.values())
    empty = rows.get("", 0.0)
    out(f"Week of {bounds['week_of']} ({args.week})")
    out(f"Browser time: {_h(total)} h across {len([t for t in rows if t])} distinct titles")
    out(f"Empty title: {_h(empty)} h")
    out(f"Unreadable events skipped: {bad}")
    out("")
    out("Top words (a word is a clue, not a rule):")
    for w, s in top_words(rows)[:top_n]:
        share = (s / total * 100) if total > 0 else 0.0
        out(f"{w} | {_h(s)} h | {share:.0f}%")

    if keywords is not None:
        per, left = match_buckets(rows, keywords)
        explained = total - left
        out("")
        for name, s in per.items():
            out(f"{_CTRL_RE.sub(' ', name)}: {_h(s)} h")
        pct = (explained / total * 100) if total > 0 else 0.0
        out(f"explained: {_h(explained)} h of {_h(total)} h ({pct:.0f}%)")
        out(f"still Browsing: {_h(left)} h")
        if total > 0 and round(pct) < 50:
            out(UNDER_HALF_NOTE)

    if show_n:
        out("")
        out(f"Top {show_n} exact titles by hours:")
        ranked = sorted(((t, s) for t, s in rows.items() if t), key=lambda kv: (-kv[1], kv[0]))
        for t, s in ranked[:show_n]:
            out(f"{_h(s)} h | {_CTRL_RE.sub(' ', t)[:TITLE_CUT]}")

    out("")
    out(FINAL_LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
