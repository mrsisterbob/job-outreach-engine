"""A fixed 60-company sample of the Carmen Warm export, for repeatable source tests.

Pure logic except write_sample/read_sample. Source is the Carmen Warm export ONLY (never the
alumni file). Privacy: only company name, contact count, domain and slug are ever produced; no
person name, email address, LinkedIn link or note leaves this module.

Tier rule (a design choice): employer-like companies only (is_probable_company_name and not
non_employer_kind), sorted by (-count, key).
  TOP  = the 20 heaviest.
  MID  = 20 of the companies with count >= 2 that are not in TOP, at evenly spaced ranks
         (i * len(band) // 20) through that band.
  TAIL = the first 20 of the count == 1 companies ordered by sha1(key): stable, no random.
A band with fewer than 20 companies yields all of them; it is never padded from another tier.
"""
import csv
import hashlib
import os
import tempfile
from pathlib import Path

import warm_export_reader as wer
from pipeline_utils import ats_slug_guess, is_probable_company_name
from warm_company_domain import corporate_domain, guess_domain
from warm_company_kind import non_employer_kind

PER_TIER = 20
TIERS = ("top", "mid", "tail")
COLUMNS = ["company_key", "name", "tier", "contacts", "domain", "domain_source", "slug"]
REPO_ROOT = Path(__file__).resolve().parent
_COUNTS_PREFIX = "# tiers "


def employer_like(companies):
    """{key: entry} for the companies that look like employers."""
    return {k: v for k, v in companies.items()
            if is_probable_company_name(v["name"]) and not non_employer_kind(v["name"])}


def _row(key, entry, tier):
    slug = ats_slug_guess(entry["name"])
    domain = corporate_domain([f"x@{d}" for d in entry["domains"]])
    source = "email"
    if not domain:
        domain, source = guess_domain(slug), "guess"
    return {"company_key": key, "name": entry["name"], "tier": tier, "contacts": entry["count"],
            "domain": domain, "domain_source": source, "slug": slug}


def build_sample(companies):
    """(rows, report) from a parse_rows/read_files companies dict.

    report: {"tiers": {tier: {"band": n, "picked": n, "min": c, "max": c, "sum": c}},
             "short": [tier, ...], "total": all companies, "employer_like": n,
             "email": n, "guess": n}
    """
    pool = employer_like(companies)
    ranked = sorted(pool.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    top = ranked[:PER_TIER]
    top_keys = {k for k, _ in top}
    mid_band = [kv for kv in ranked if kv[1]["count"] >= 2 and kv[0] not in top_keys]
    if len(mid_band) <= PER_TIER:
        mid = list(mid_band)
    else:
        mid = [mid_band[i * len(mid_band) // PER_TIER] for i in range(PER_TIER)]
    tail_band = sorted((kv for kv in ranked if kv[1]["count"] == 1),
                       key=lambda kv: hashlib.sha1(kv[0].encode("utf-8")).hexdigest())
    tail = tail_band[:PER_TIER]
    picks = {"top": (top, len(ranked)), "mid": (mid, len(mid_band)), "tail": (tail, len(tail_band))}
    rows, tiers, short = [], {}, []
    for tier in TIERS:
        chosen, band = picks[tier]
        counts = [e["count"] for _, e in chosen]
        tiers[tier] = {"band": band, "picked": len(chosen), "min": min(counts, default=0),
                       "max": max(counts, default=0), "sum": sum(counts)}
        if len(chosen) < PER_TIER:
            short.append(tier)
        rows.extend(_row(k, e, tier) for k, e in chosen)
    report = {"tiers": tiers, "short": short, "total": len(companies), "employer_like": len(pool),
              "email": sum(1 for r in rows if r["domain_source"] == "email"),
              "guess": sum(1 for r in rows if r["domain_source"] == "guess")}
    return rows, report


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def write_sample(rows, path, force=False, repo_root=REPO_ROOT):
    """Write the CSV atomically. Returns (ok, reason); reason is '' on success."""
    if _inside(path, repo_root):
        return False, f"refused: {path} is inside the repo; the sample must live outside it"
    out = Path(path)
    if out.exists() and not force:
        return False, f"refused: {path} already exists (the sample is fixed; --force to replace)"
    out.parent.mkdir(parents=True, exist_ok=True)
    tier_counts = {t: sum(1 for r in rows if r["tier"] == t) for t in TIERS}
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(_COUNTS_PREFIX + " ".join(f"{t}={n}" for t, n in tier_counts.items()) + "\n")
            w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({c: r[c] for c in COLUMNS})
        os.replace(tmp, out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True, ""


def read_sample(path):
    """Rows in file order. Raises ValueError on a missing column, an unknown tier, or tier row
    counts that differ from the file's own '# tiers' line. Cells are returned as written."""
    with open(path, encoding="utf-8", newline="") as f:
        first = f.readline().rstrip("\n")
        if not first.startswith(_COUNTS_PREFIX):
            raise ValueError("sample file has no '# tiers' counts line")
        try:
            declared = {k: int(v) for k, v in (p.split("=") for p in first[len(_COUNTS_PREFIX):].split())}
        except ValueError as e:
            raise ValueError("unreadable '# tiers' counts line") from e
        reader = csv.DictReader(f)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"sample file is missing columns: {missing}")
        rows = list(reader)
    seen = {t: 0 for t in TIERS}
    for r in rows:
        if r["tier"] not in seen:
            raise ValueError(f"unknown tier {r['tier']!r}")
        seen[r["tier"]] += 1
    if seen != {t: declared.get(t) for t in TIERS}:
        raise ValueError(f"tier row counts {seen} differ from the file's own counts {declared}")
    return rows


def format_report(report):
    """Printable lines. Counts only, never a company name."""
    lines = []
    if report["short"]:
        lines.append("SHORT: " + ", ".join(
            f"{t} band holds {report['tiers'][t]['band']} (fewer than {PER_TIER}, all taken, not padded)"
            for t in report["short"]))
    lines.append(f"Carmen Warm only: {report['total']} companies, {report['employer_like']} employer-like")
    for t in TIERS:
        s = report["tiers"][t]
        lines.append(f"{t}: band {s['band']}, picked {s['picked']}, contacts {s['min']}-{s['max']}, sum {s['sum']}")
    lines.append(f"domain_source: email {report['email']}, guess {report['guess']}")
    return lines


def run(warm_path, out_path, force=False, normalizer=None, repo_root=REPO_ROOT):
    """The CLI body. Returns (exit_code, printable_lines)."""
    companies, stats = wer.read_files([warm_path], normalizer=normalizer)
    rows, report = build_sample(companies)
    lines = format_report(report)
    ok, reason = write_sample(rows, out_path, force=force, repo_root=repo_root)
    if not ok:
        return 1, [reason] + lines
    return 0, lines + [f"wrote {len(rows)} rows to {out_path}"]
