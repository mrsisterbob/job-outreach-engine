"""Read-only: how does a Workday tenant's public CXS jobs endpoint behave when paged 20 at a time?

  python scripts/measure_workday_fetch.py --board tenant/wdN/site [--board ...] [--max-requests N] [--pause S]

Per tenant it prints counts only (never posting text): total, page size honoured (limit 20/21/50),
`total` on pages 1/2/3/last, pages and distinct externalPath values reached by offset += 20, the
answer at offsets 1,980 / 2,000 / 2,020 / 2,500 (only when the board's total reaches that offset),
and what `searchText` does for three WORKDAY_SEARCH_TERMS.

Request shape matches main.fetch_workday_jobs / probe_workday_board_total (main.py:13539-13700):
POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs with JSON
{appliedFacets, limit, offset, searchText}. The header values and search terms are copied from
main.py:13539-13540 (importing main has logging side effects); nothing is written anywhere.
A request that fails or times out is "no answer" and never counts as an empty page.
"""
import argparse
import sys
import time

import requests

# Copied from main.py:13539-13540 so what is measured is what production sends.
WORKDAY_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
WORKDAY_SEARCH_TERMS = ("operations analyst", "business operations", "financial analyst", "operations specialist")

PAGE = 20
MAX_PAGES = 120
PER_TENANT_REQUESTS = 130
CAP_OFFSETS = (1980, 2000, 2020, 2500)
TIMEOUT = 15


class BudgetExceeded(Exception):
    """The request budget (per tenant or whole run) is spent."""


def parse_board(text):
    """'tenant/wdN/site' -> (tenant, wd, site); ValueError when malformed (same shape check as
    main.valid_workday_triple)."""
    parts = str(text or "").strip().split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"malformed --board {text!r}: expected tenant/wdN/site")
    return tuple(parts)


def make_post(board, pause=0.5, session=None):
    """Return post(limit, offset, search) -> {status, json, text, error}. status is None for no answer."""
    tenant, wd, site = board
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": WORKDAY_USER_AGENT}
    http = session or requests

    def post(limit, offset, search=""):
        time.sleep(pause)
        try:
            res = http.post(url, json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": search},
                            headers=headers, timeout=TIMEOUT)
        except Exception as e:
            return {"status": None, "json": None, "text": "", "error": type(e).__name__}
        body = None
        try:
            body = res.json()
        except Exception:
            pass
        return {"status": res.status_code, "json": body if isinstance(body, dict) else None,
                "text": (res.text or "")[:120], "error": ""}
    return post


def budgeted(post, limit_count, shared=None):
    """Wrap post so at most `limit_count` calls go out; `shared` is a one-item list holding the
    run-wide remaining count. Raises BudgetExceeded instead of sending."""
    state = {"n": 0}

    def wrapped(limit, offset, search=""):
        if state["n"] >= limit_count or (shared is not None and shared[0] <= 0):
            raise BudgetExceeded()
        state["n"] += 1
        if shared is not None:
            shared[0] -= 1
        return post(limit, offset, search)
    wrapped.state = state
    return wrapped


def _postings(ans):
    body = ans.get("json") or {}
    p = body.get("jobPostings")
    return p if isinstance(p, list) else []


def _total(ans):
    t = (ans.get("json") or {}).get("total")
    return t if isinstance(t, int) and not isinstance(t, bool) else None


def page_all(post, search="", max_pages=MAX_PAGES):
    """Page by offset += 20. Ends on: 'empty' (HTTP 200, zero postings), 'error' (non-200),
    'no answer', 'max_pages' or 'budget'. Returns counts plus per-offset status/total."""
    out = {"pages": 0, "collected": 0, "paths": set(), "titles": {}, "duplicates": 0, "totals": [],
           "end": "", "stop_offset": None, "stop_status": None, "stop_text": "", "by_offset": {}}
    offset = 0
    while True:
        if out["pages"] >= max_pages:
            out["end"], out["stop_offset"] = "max_pages", offset
            return out
        try:
            ans = post(PAGE, offset, search)
        except BudgetExceeded:
            out["end"], out["stop_offset"] = "budget", offset
            return out
        out["by_offset"][offset] = {"status": ans["status"], "n": len(_postings(ans)), "total": _total(ans),
                                    "text": ans["text"]}
        if ans["status"] is None:
            out["end"], out["stop_offset"] = "no answer", offset
            return out
        if ans["status"] != 200:
            out.update(end="error", stop_offset=offset, stop_status=ans["status"], stop_text=ans["text"])
            return out
        posts = _postings(ans)
        if not posts:
            out["end"], out["stop_offset"] = "empty", offset
            out["totals"].append((offset, _total(ans)))
            return out
        out["pages"] += 1
        out["totals"].append((offset, _total(ans)))
        for p in posts:
            path = p.get("externalPath")
            out["collected"] += 1
            if path in out["paths"]:
                out["duplicates"] += 1
            else:
                out["paths"].add(path)
                out["titles"][path] = str(p.get("title") or "")
        offset += PAGE


def measure_tenant(post, terms=WORKDAY_SEARCH_TERMS[:3], cap_offsets=CAP_OFFSETS, max_pages=MAX_PAGES):
    """Run every measurement for one tenant through `post`. Returns a plain dict of numbers."""
    r = {"total": None, "total_error": "", "limits": {}, "paging": None, "cap": {}, "search": {}, "budget_hit": False}
    try:
        first = post(1, 0, "")
        r["total"] = _total(first) if first["status"] == 200 else None
        if r["total"] is None:
            r["total_error"] = "no answer" if first["status"] is None else f"HTTP {first['status']}"
            return r
        for lim in (20, 21, 50):
            a = post(lim, 0, "")
            r["limits"][lim] = {"status": a["status"], "n": len(_postings(a)),
                                "text": "" if a["status"] == 200 else a["text"]}
        pg = page_all(post, "", max_pages)
        r["paging"] = pg
        if pg["end"] == "budget":
            r["budget_hit"] = True
            return r
        for off in cap_offsets:
            if r["total"] < off:
                r["cap"][off] = "skipped (total below offset)"
                continue
            seen = pg["by_offset"].get(off)
            if seen is None:
                a = post(PAGE, off, "")
                seen = {"status": a["status"], "n": len(_postings(a)), "total": _total(a), "text": a["text"]}
            r["cap"][off] = seen
        whole = pg["end"] == "empty" and len(pg["paths"]) >= r["total"]
        for term in terms:
            sp = page_all(post, term, max_pages)
            row = {"server_total": next((t for _, t in sp["totals"] if t is not None), None),
                   "paged": sp["collected"], "distinct": len(sp["paths"]), "end": sp["end"],
                   "local_substring": None, "search_only": None}
            if whole:
                row["local_substring"] = sum(1 for t in pg["titles"].values() if term.lower() in t.lower())
            if r["total"] >= 2000:
                row["search_only"] = len(sp["paths"] - pg["paths"])
            r["search"][term] = row
        if pg["end"] == "budget" or any(v["end"] == "budget" for v in r["search"].values()):
            r["budget_hit"] = True
    except BudgetExceeded:
        r["budget_hit"] = True
    return r


def format_tenant(name, r):
    lines = [f"== {name} =="]
    if r["total"] is None:
        lines.append(f"total: unavailable ({r['total_error'] or 'budget'})")
        return "\n".join(lines)
    lines.append(f"total (limit 1): {r['total']}")
    for lim, v in r["limits"].items():
        lines.append(f"limit {lim}: HTTP {v['status']} postings={v['n']}" + (f" body={v['text']!r}" if v["text"] else ""))
    pg = r["paging"]
    if pg:
        tots = ", ".join(f"off{o}={t}" for o, t in (pg["totals"][:3] + pg["totals"][-1:]))
        lines.append(f"total across pages: {tots}")
        lines.append(f"paging: pages={pg['pages']} collected={pg['collected']} distinct={len(pg['paths'])} "
                     f"duplicates={pg['duplicates']} end={pg['end']} stop_offset={pg['stop_offset']} "
                     f"stop_status={pg['stop_status']}" + (f" body={pg['stop_text']!r}" if pg["stop_text"] else ""))
    for off, v in r["cap"].items():
        lines.append(f"offset {off}: " + (v if isinstance(v, str) else
                     f"HTTP {v['status']} postings={v['n']} total={v['total']}" + (f" body={v['text']!r}" if v["status"] != 200 else "")))
    for term, v in r["search"].items():
        lines.append(f"search {term!r}: server_total={v['server_total']} paged={v['paged']} distinct={v['distinct']} "
                     f"end={v['end']} local_substring={v['local_substring']} search_only_vs_unfiltered={v['search_only']}")
    if r["budget_hit"]:
        lines.append("request budget reached: later measurements missing")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--board", action="append", default=[], help="tenant/wdN/site (repeat)")
    ap.add_argument("--max-requests", type=int, default=400)
    ap.add_argument("--pause", type=float, default=0.5)
    args = ap.parse_args(argv)
    try:
        boards = [parse_board(b) for b in args.board]
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not boards:
        print("give at least one --board tenant/wdN/site", file=sys.stderr)
        return 2
    shared = [args.max_requests]
    results = []
    for b in boards:
        post = budgeted(make_post(b, args.pause), PER_TENANT_REQUESTS, shared)
        r = measure_tenant(post)
        print(format_tenant("/".join(b), r))
        results.append(r)
    if not any((r["total"] or 0) >= 2000 for r in results):
        print("cap not reached in any tenant tested - unmeasured")
    print(f"requests used: {args.max_requests - shared[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
