"""Read-only: do eight ATS systems answer a free, keyless call, and what does a reply carry?

  python scripts/measure_ats_endpoints.py --board workable=<slug> --board bamboohr=<slug> ... [--record DIR]

Boards come from the command line only (no defaults, no guessed tenants). At most ONE request per
system, GET or POST exactly as ENDPOINT_SPECS says, no redirect followed, one at a time. A run with no
--board makes zero requests and exits 2. Prints status / parses / posting count / title, location and
apply-link coverage; never posting text. Nothing is written unless --record DIR (outside the repo).
Endpoint shapes come from the README of github.com/danielhagever/job-board-apis (read 2026-10-31) and
are UNTESTED by us; that is what this measurement is for.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ats_endpoint_report as aer  # noqa: E402

UA = "job-outreach-measure/1.0 (read-only careers board survey; contact kjmiller406@gmail.com)"
TIMEOUT = 8
PAUSE = 0.7
MAX_BYTES = 1_000_000
README = "github.com/danielhagever/job-board-apis README"

# system -> EndpointSpec or None (None prints as "no public endpoint"; reason in the comment).
ENDPOINT_SPECS = {
    # README: iCIMS is under "Systems with no JSON endpoint"; only a server-rendered HTML page.
    "icims": None,
    # README: JSON search at /careersection/rest/jobboard/searchjobs "needs a portal id read from the
    # page first"; no method, body or field names documented, so not callable from a board ref.
    "taleo": None,
    # README: no JSON endpoint; the list is an HTML table (jobs.sap.com answered curl with 403).
    "successfactors": None,
    # README "At a glance": GET {host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions, reads
    # .items[0].requisitionList. Title/location/apply field names are NOT documented: hypotheses.
    "oracle": aer.EndpointSpec(
        "oracle", "GET",
        "https://{0}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        "?onlyData=true&expand=requisitionList.secondaryLocations",
        None, ("items", 0, "requisitionList"), (("Title",),), (("PrimaryLocation",),), (),
        README + ' "At a glance": "GET {host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"'
        " with ?onlyData=true&expand=requisitionList.secondaryLocations, list at .items[0].requisitionList"
        " (all from the README). NOT from the README: the field names Title and PrimaryLocation are our"
        " hypotheses, and no apply-link field is documented, so apply link is reported as not available"),
    # README: GET apply.workable.com/api/v1/widget/accounts/{token}?details=true. Disagrees on the
    # query string; we keep the URL measured 200 + name + jobs on 2026-10-26 (TASK-171) without it.
    "workable": aer.EndpointSpec(
        "workable", "GET", "https://apply.workable.com/api/v1/widget/accounts/{0}",
        None, ("jobs",), (("title",),), (("city",), ("country",)), (("url",), ("shortlink",)),
        README + ' "At a glance": "GET apply.workable.com/api/v1/widget/accounts/{token}?details=true"'
        " (kept the 2026-10-26 measured URL without ?details=true; field names are hypotheses)"),
    # README: GET {token}.bamboohr.com/careers/list, list key result, title jobOpeningName; location and
    # apply link are not documented (hypotheses). Matches the 2026-10-26 measured URL.
    "bamboohr": aer.EndpointSpec(
        "bamboohr", "GET", "https://{0}.bamboohr.com/careers/list",
        None, ("result",), (("jobOpeningName",),), (("location",),), (("jobUrl",), ("url",)),
        README + ' "At a glance": "GET {token}.bamboohr.com/careers/list"'
        " (title jobOpeningName documented; location/apply names are hypotheses)"),
    # README: GET myjobs.adp.com/public/staffing/v1/job-requisitions needs an `orgoid` header, else 400;
    # EndpointSpec carries no headers and a board ref is not a header, so not callable here.
    "adp": None,
    # README: UKG is not mentioned at all.
    "ukg": None,
}


def _default_get(url, **kw):
    import requests
    return requests.get(url, **kw)


def _default_post(url, **kw):
    import requests
    return requests.post(url, **kw)


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _record(directory, system, status, body):
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    for stale in (".json", ".txt", ".truncated"):
        (d / (system + stale)).unlink(missing_ok=True)
    text = body if isinstance(body, str) else ""
    raw = text.encode("utf-8", "replace")
    cut = len(raw) > MAX_BYTES
    ext = "txt"
    if cut:
        text = raw[:MAX_BYTES].decode("utf-8", "ignore")
        (d / (system + ".truncated")).write_text("cut at 1 MB\n", encoding="utf-8")
    elif status == 200:
        try:
            json.loads(text)
            ext = "json"
        except ValueError:
            pass
    (d / ("%s.%s" % (system, ext))).write_text(text, encoding="utf-8")


def _lines_for(system, r):
    """Plain lines for one measured system (reuses render_report's line, never rewrites it)."""
    idx = aer.UNFETCHED_SYSTEMS.index(system)
    rep = aer.render_report([r])
    lines = [rep[idx]]
    if r["source_note"]:
        lines.append(rep[idx + 1])
    if r["outcome"] == "no_answer":
        lines.append("    cause: %s" % r.get("error_text", "unknown"))
    if system == "oracle" and not ENDPOINT_SPECS["oracle"].apply_paths:
        lines.append("    apply link: not available (README documents no apply-link field)")
    return lines


def run(argv, get=None, post=None, pause=PAUSE, out=print, root=ROOT):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--board", action="append", default=[], metavar="system=ref")
    ap.add_argument("--max-requests", type=int, default=8)
    ap.add_argument("--record", default=None, metavar="DIR")
    args = ap.parse_args(argv)
    get = get or _default_get
    post = post or _default_post

    boards = {}
    for text in args.board:
        try:
            system, ref = aer.parse_board_arg(text)
        except ValueError as exc:
            out("bad --board %r: %s" % (text, exc))
            return 2
        if system in boards:
            out("duplicate --board for %s: give one board per system" % system)
            return 2
        boards[system] = ref
    if args.record and _inside(args.record, root):
        out("refused: --record %s is inside the repo; pick a folder outside it" % args.record)
        return 2

    results, requests_made, skipped, record_errors = {}, 0, 0, []
    for system in aer.UNFETCHED_SYSTEMS:
        if system not in boards:
            continue
        spec = ENDPOINT_SPECS.get(system)
        if spec is None:
            results[system] = aer.no_public_endpoint_result(system, "no keyless endpoint documented")
            continue
        if requests_made >= args.max_requests:
            skipped += 1
            results[system] = None
            continue
        try:
            url = aer.build_url(spec, boards[system])
        except ValueError as exc:
            results[system] = aer.no_answer_result(spec, "bad board ref: %s" % exc)
            continue
        if pause:
            time.sleep(pause)
        requests_made += 1
        kw = {"timeout": TIMEOUT, "headers": {"User-Agent": UA}, "allow_redirects": False}
        try:
            if spec.method == "POST":
                resp = post(url, data=spec.body, **kw)
            else:
                resp = get(url, **kw)
            status, body = resp.status_code, resp.text
        except Exception as exc:
            results[system] = aer.no_answer_result(spec, type(exc).__name__)
            continue
        results[system] = aer.measure_reply(spec, status, body)
        if args.record:
            try:
                _record(args.record, system, status, body)
            except (OSError, ValueError) as exc:
                record_errors.append("record failed for %s: %s" % (system, type(exc).__name__))

    for system in aer.UNFETCHED_SYSTEMS:
        if system not in boards:
            out("%s: not measured (no board supplied)" % system)
        elif system in results and results[system] is None:
            out("%s: not measured (skipped: --max-requests %d reached)" % (system, args.max_requests))
        elif results[system]["outcome"] == "no_public_endpoint":
            out("%s: no public endpoint (%s)" % (system, results[system].get("error_text", "")))
        else:
            for line in _lines_for(system, results[system]):
                out(line)
    if not boards:
        out("No --board supplied: 0 requests made.")
        return 2
    got = [r for r in results.values() if r and r["outcome"] not in ("no_answer", "no_public_endpoint")]
    failed = sum(1 for r in results.values() if r and r["outcome"] == "no_answer")
    nopub = sum(1 for r in results.values() if r and r["outcome"] == "no_public_endpoint")
    out("%d of %d supplied boards got an answer; %d failed (no answer); %d skipped by --max-requests; "
        "%d have no public endpoint (%d requests made)"
        % (len(got), len(boards), failed, skipped, nopub, requests_made))
    for err in record_errors:
        out(err)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
