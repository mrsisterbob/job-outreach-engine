"""Score a board source's answers against Kevin's hand-labelled truth for the 60-company sample.

Pure scoring plus small CSV readers. No network, no CRM; the only file written is the labelling
template (write_truth_template).

File shapes (all CSV, one header row; Excel's BOM is accepted):
  SAMPLE  warm_sample.COLUMNS (TASK-221), read with warm_sample.read_sample.
  TRUTH   company_key,system,token  (extra columns such as name,tier,domain are allowed).
          system is a board_system_classifier.SYSTEMS value or the word `none` (checked, no public
          board). token is the slug, or the Workday tenant/wdN/site triple, blank for none.
          A blank system means "not labelled yet": the row is ignored and counted.
  RESULT  company_key,system,token, one row per company the source answered; an absent row (or a
          row with a blank or `none` system and no URL) is "no answer". Other column names are
          mapped with Columns (the CLI's --company-col, --domain-col, --system-col, --token-col,
          --url-col). Several rows for one company: the first row that names a board wins.

URL rule (board_from_url), system by board_system_classifier.classify_board_system, token by:
  workday          the tenant/wdN/site triple (workday_board.board_triple_from_url)
  greenhouse       first path segment (boards.greenhouse.io/acme -> acme); an embed URL
                   (boards.greenhouse.io/embed/job_board?for=acme) uses the for= value, blank if absent
  lever            first path segment (jobs.lever.co/acme)
  ashby            first path segment (jobs.ashbyhq.com/acme)
  smartrecruiters  first path segment (jobs.smartrecruiters.com/Acme)
  workable         first path segment (apply.workable.com/acme)
  bamboohr         the subdomain (acme.bamboohr.com)
  any other vendor system with a URL: token is blank (no rule is verified for it).
A URL the classifier cannot place (unknown / own_site) is no answer.
"""
import csv
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import warm_export_reader as wer
from board_system_classifier import SYSTEMS, classify_board_system
from warm_sample import TIERS, _inside
from workday_board import board_triple_from_url

REPO_ROOT = Path(__file__).resolve().parent
TRUTH_COLUMNS = ["company_key", "name", "tier", "domain", "system", "token"]
HIT, FALSE_HIT, MISS, CORRECT_NEGATIVE = "HIT", "FALSE HIT", "MISS", "CORRECT NEGATIVE"
VERDICTS = (HIT, FALSE_HIT, MISS, CORRECT_NEGATIVE)
_PATH_TOKEN = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"}


class ColumnError(ValueError):
    """A named column is not in the file's header. Message names the header that was found."""


class Columns:
    """Result-file column names. Any subset of company/domain and of system+token/url."""
    def __init__(self, company="company_key", domain=None, system="system", token="token", url=None):
        self.company, self.domain, self.system, self.token, self.url = company, domain, system, token, url

    def named(self):
        return [c for c in (self.company, self.domain, self.system, self.token, self.url) if c]


def _clean(v):
    return (v or "").strip()


def board_from_url(url):
    """(system, token) for a board URL, or ("", "") when no board can be named."""
    url = _clean(url)
    if not url:
        return "", ""
    system = classify_board_system(url if "://" in url else "https://" + url, "")
    if system in ("unknown", "own_site"):
        return "", ""
    if system == "workday":
        return system, board_triple_from_url(url)
    parts = urlsplit(url if "://" in url else "https://" + url)
    if system in _PATH_TOKEN:
        segs = [s for s in parts.path.split("/") if s]
        if system == "greenhouse" and segs and segs[0].lower() == "embed":
            return system, (parse_qs(parts.query).get("for") or [""])[0]
        return system, segs[0] if segs else ""
    if system == "bamboohr":
        host = (parts.hostname or "").lower()
        sub = host[: -len("bamboohr.com")].rstrip(".")
        return system, sub.split(".")[-1] if sub else ""
    return system, ""


def _read_csv(path, wanted, who):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        missing = [c for c in wanted if c not in header]
        if missing:
            raise ColumnError(f"{who}: column(s) {missing} not in the file's header {header}")
        return list(reader)


def read_truth(path):
    """({company_key: (system, token)}, not_labelled). Labelled `none` rows keep ("none", "")."""
    rows = _read_csv(path, ["company_key", "system", "token"], "truth file")
    labels, blank = {}, 0
    for r in rows:
        system = _clean(r["system"]).lower()
        if not system:
            blank += 1
            continue
        if system != "none" and system not in SYSTEMS:
            raise ValueError(f"truth file: unknown system {system!r} for {r['company_key']!r}")
        labels[_clean(r["company_key"])] = (system, "" if system == "none" else _clean(r["token"]))
    return labels, blank


def _domain_of(cell):
    t = _clean(cell).lower()
    if "://" in t:
        t = urlsplit(t).hostname or ""
    t = t.split("/", 1)[0]
    return t[4:] if t.startswith("www.") else t


def read_result(path, cols, sample_rows, normalizer=None):
    """({company_key: (system, token)}, unjoined) for one source's file.

    Join: cols.company holds a name, turned into a key exactly as TASK-221 does; a name that is
    not a sample key falls back to cols.domain against the sample's domain column. unjoined counts
    rows that name a board but match no sample company. Raises ColumnError on a missing column."""
    rows = _read_csv(path, cols.named(), "result file")
    by_key = {r["company_key"] for r in sample_rows}
    by_domain = {_domain_of(r["domain"]): r["company_key"] for r in sample_rows if _clean(r["domain"])}
    answers, unjoined = {}, 0
    for r in rows:
        key = None
        if cols.company and _clean(r[cols.company]):
            k = wer._key(_clean(r[cols.company]), normalizer)
            key = k if k in by_key else None
        if key is None and cols.domain:
            key = by_domain.get(_domain_of(r[cols.domain]))
        system = _clean(r[cols.system]).lower() if cols.system else ""
        token = _clean(r[cols.token]) if cols.token else ""
        if cols.url and _clean(r[cols.url]):
            u_sys, u_tok = board_from_url(r[cols.url])
            system, token = (system or u_sys), (token or u_tok)
        if not system or system == "none":
            continue
        if key is None:
            unjoined += 1
        elif key not in answers:
            answers[key] = (system, token)
    return answers, unjoined


def verdict(truth, answer):
    """One verdict per labelled company. truth is (system, token); answer is (system, token) or None.

    truth board, same system+token (case-insensitive, trimmed) -> HIT
    truth board, different board                               -> FALSE HIT
    truth board, no answer                                     -> MISS
    truth none,  any board named                               -> FALSE HIT
    truth none,  no answer                                     -> CORRECT NEGATIVE
    """
    if truth[0] == "none":
        return CORRECT_NEGATIVE if answer is None else FALSE_HIT
    if answer is None:
        return MISS
    same = (answer[0].lower() == truth[0].lower()
            and answer[1].strip().lower() == truth[1].strip().lower())
    return HIT if same else FALSE_HIT


def score(sample_rows, truth, answers):
    """{"overall": {n, HIT, ...}, tier: {...}, "unlabelled": N}. Rates use n = labelled companies."""
    tier_of = {r["company_key"]: r["tier"] for r in sample_rows}
    blank = lambda: {"n": 0, **{v: 0 for v in VERDICTS}}  # noqa: E731
    out = {"overall": blank(), **{t: blank() for t in TIERS}}
    for key, label in truth.items():
        if key not in tier_of:
            continue
        v = verdict(label, answers.get(key))
        for bucket in (out["overall"], out[tier_of[key]]):
            bucket["n"] += 1
            bucket[v] += 1
    out["unlabelled"] = sum(1 for k in answers if k not in truth or k not in tier_of)
    return out


def _rate(count, n):
    return f"{100 * count / n:.0f}% (n={n})" if n else "n/a (n=0)"


def _per_hit(total, hits, unit):
    return f"{total / hits:.2f} {unit}" if hits else "n/a (0 hits)"


def format_report(scores, cost=None, seconds=None, not_labelled=0):
    """Printable lines. scores: {source: score(...)}; cost/seconds: {source: float} or None."""
    lines = [f"not labelled: {not_labelled}"]
    for scope in ("overall", *TIERS):
        lines.append(f"[{scope}]")
        lines.append(f"{'source':<20}{'n':>4}{'HIT':>5}{'FALSE HIT':>10}{'MISS':>6}{'CORRECT NEG':>12}  hit rate")
        for name, s in scores.items():
            b = s[scope]
            lines.append(f"{name:<20}{b['n']:>4}{b[HIT]:>5}{b[FALSE_HIT]:>10}{b[MISS]:>6}"
                         f"{b[CORRECT_NEGATIVE]:>12}  {_rate(b[HIT], b['n'])}")
    for name, s in scores.items():
        hits = s["overall"][HIT]
        lines.append(f"{name}: unlabelled answers: {s['unlabelled']}")
        if cost and name in cost:
            lines.append(f"{name}: dollars per correct board = {_per_hit(cost[name], hits, 'USD')}")
        if seconds and name in seconds:
            lines.append(f"{name}: wall-clock {seconds[name]:g} s; seconds per correct board = "
                         + (f"{seconds[name] / hits:.1f}" if hits else "n/a (0 hits)"))
    return lines


def write_truth_template(sample_rows, path, repo_root=REPO_ROOT):
    """Write the labelling CSV (system and token blank). Returns (ok, reason)."""
    if _inside(path, repo_root):
        return False, f"refused: {path} is inside the repo; the truth file must live outside it"
    out = Path(path)
    if out.exists():
        return False, f"refused: {path} already exists (never overwritten)"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRUTH_COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in sample_rows:
            w.writerow({**{c: r[c] for c in ("company_key", "name", "tier", "domain")},
                        "system": "", "token": ""})
    return True, ""
