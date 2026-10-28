"""Offline logic for measuring a public job-board endpoint reply (pure: no I/O, no network).

Turns one raw reply into four facts: HTTP status, whether it parses, how many postings it
returned, and how many carry a title, a location and an apply link. Field paths are
hypotheses; a result keeps only counts and key NAMES, never posting text, names or links.
"""
import json
import re
from collections import namedtuple

from board_system_classifier import VENDOR_MARKERS  # noqa: F401  (vendor names live there)

UNFETCHED_SYSTEMS = ("icims", "taleo", "successfactors", "oracle", "workable", "bamboohr", "adp", "ukg")

EndpointSpec = namedtuple(
    "EndpointSpec",
    "system method url_template body list_path title_paths location_paths apply_paths source_note",
)

_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PLACEHOLDER_RE = re.compile(r"\{(\d+)\}")
_DICT_TEXT_KEYS = ("name", "label", "text", "city")
_NO_BOARD = "not measured (no board supplied)"

RESULT_KEYS = (
    "system", "outcome", "http_status", "parses", "posting_count", "with_title",
    "with_location", "with_apply_link", "with_all_three", "top_level_keys",
    "first_posting_keys", "source_note",
)


def _check_parts(parts):
    for part in parts:
        if not part:
            raise ValueError("empty part in board reference")
        if not _PART_RE.fullmatch(part):
            raise ValueError("part %r has characters outside [A-Za-z0-9._-]" % part)
        if ".." in part or not part.strip("."):
            raise ValueError("part %r is a dot sequence" % part)


def parse_board_arg(text):
    """'icims=acme/jobs' -> ('icims', 'acme/jobs'). ValueError with a reason on any bad form."""
    if not isinstance(text, str) or "=" not in text:
        raise ValueError("expected system=ref (no '=' found)")
    system, _, ref = text.partition("=")
    if system not in UNFETCHED_SYSTEMS:
        raise ValueError("system %r is not one of %s" % (system, ", ".join(UNFETCHED_SYSTEMS)))
    if not ref:
        raise ValueError("empty board reference")
    _check_parts(ref.split("/"))
    return system, ref


def build_url(spec, ref):
    """Fill the spec's {0}, {1} placeholders with the parts of ref. ValueError on mismatch / non-https."""
    parts = ref.split("/") if isinstance(ref, str) and ref else []
    _check_parts(parts)
    slots = set(_PLACEHOLDER_RE.findall(spec.url_template))
    if len(parts) != len(slots):
        raise ValueError("%s needs %d part(s), got %d" % (spec.system, len(slots), len(parts)))
    try:
        url = spec.url_template.format(*parts)
    except (IndexError, KeyError) as exc:
        raise ValueError("template placeholders are not {0}..{n}: %s" % type(exc).__name__)
    if not url.startswith("https://"):
        raise ValueError("built URL is not https")
    return url


def resolve_path(obj, path):
    """Value at path (dict keys / list indexes) or None when any step is missing or mistyped."""
    cur = obj
    try:
        for step in path:
            if isinstance(step, int) and not isinstance(step, bool):
                if not isinstance(cur, list) or not -len(cur) <= step < len(cur):
                    return None
                cur = cur[step]
            else:
                if not isinstance(cur, dict) or step not in cur:
                    return None
                cur = cur[step]
    except Exception:
        return None
    return cur


def _text_of(value):
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list) and value:
        return value[0].strip() or None if isinstance(value[0], str) else None
    if isinstance(value, dict):
        for key in _DICT_TEXT_KEYS:
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
    return None


def first_value(obj, paths):
    """(value, index of the winning path) for the first path giving non-empty text, else (None, None)."""
    for i, path in enumerate(paths or ()):
        got = _text_of(resolve_path(obj, path))
        if got is not None:
            return got, i
    return None, None


def _result(system, outcome, source_note="", http_status=None, parses=None, **over):
    out = {
        "system": system, "outcome": outcome, "http_status": http_status, "parses": parses,
        "posting_count": None, "with_title": None, "with_location": None,
        "with_apply_link": None, "with_all_three": None, "top_level_keys": [],
        "first_posting_keys": [], "source_note": source_note,
    }
    out.update(over)
    return out


def no_answer_result(spec, error_text):
    return _result(spec.system, "no_answer", spec.source_note, error_text=str(error_text)[:120])


def no_public_endpoint_result(system, reason):
    return _result(system, "no_public_endpoint", "", error_text=str(reason)[:120])


def not_measured_result(system):
    return _result(system, "not_measured")


def measure_reply(spec, status, body_text):
    """One raw reply -> the plain result dict (counts and key names only)."""
    note = spec.source_note
    if status is None:
        return _result(spec.system, "no_answer", note)
    if status != 200:
        return _result(spec.system, "http_error", note, http_status=status)
    try:
        data = json.loads(body_text)
    except (TypeError, ValueError, RecursionError):
        return _result(spec.system, "not_json", note, http_status=status, parses=False)
    top = [str(k)[:60] for k in list(data)[:10]] if isinstance(data, dict) else []
    postings = resolve_path(data, spec.list_path)
    if not isinstance(postings, list):
        return _result(spec.system, "json_no_list", note, http_status=status, parses=True, top_level_keys=top)
    n_title = n_loc = n_apply = n_all = 0
    for p in postings:
        t = first_value(p, spec.title_paths)[0] is not None
        loc = first_value(p, spec.location_paths)[0] is not None
        a = first_value(p, spec.apply_paths)[0] is not None
        n_title += t
        n_loc += loc
        n_apply += a
        n_all += t and loc and a
    first_keys = [str(k)[:60] for k in list(postings[0])[:15]] if postings and isinstance(postings[0], dict) else []
    return _result(
        spec.system, "ok" if postings else "ok_empty", note, http_status=status, parses=True,
        posting_count=len(postings), with_title=n_title, with_location=n_loc,
        with_apply_link=n_apply, with_all_three=n_all, top_level_keys=top,
        first_posting_keys=first_keys,
    )


def _frac(n, total):
    return "-" if n is None or total is None else "%d/%d" % (n, total)


def render_report(results):
    """Lines in UNFETCHED_SYSTEMS order, then one plain-words summary line."""
    by_sys = {r["system"]: r for r in results or () if isinstance(r, dict)}
    lines = []
    with_postings = silent = 0
    for system in UNFETCHED_SYSTEMS:
        r = by_sys.get(system)
        if r is None or r["outcome"] == "not_measured":
            lines.append("%s: %s" % (system, _NO_BOARD))
            continue
        n = r["posting_count"]
        lines.append(
            "%s: %s | http %s | parses %s | postings %s | title %s | location %s | apply %s | all three %s"
            % (system, r["outcome"], r["http_status"], r["parses"], n,
               _frac(r["with_title"], n), _frac(r["with_location"], n),
               _frac(r["with_apply_link"], n), _frac(r["with_all_three"], n))
        )
        if r["source_note"]:
            lines.append("    shape read from: %s" % r["source_note"])
        if r.get("with_all_three"):
            with_postings += 1
        if r["outcome"] in ("no_answer", "no_public_endpoint"):
            silent += 1
    lines.append(
        "Measured on one board per system: %d of %d returned at least one posting with a title, a "
        "location and an apply link; %d did not answer or have no public endpoint."
        % (with_postings, len(UNFETCHED_SYSTEMS), silent)
    )
    return lines
