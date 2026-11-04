"""One /w run's per-company facts, and the text built from them. Pure: no I/O, no clock (the caller passes
run_id / started_at), no `import main`, no `import build_info` (the build arrives as a plain dict).

RunRecorder collects, per company and per source, the name that was SENT, every name that came BACK, and the
rows kept and dropped. snapshot() is JSON-safe. compose_short() writes the short Telegram message (12 lines,
1,400 characters at most); render_company / render_overview / render_full / chunk answer on demand.

`headline`, `money_line` and `attention` given to compose_short arrive as ready Telegram HTML. Company names
and returned names that THIS module prints are HTML-escaped here.
"""
from __future__ import annotations

import html
import re

VERSION = 1
MAX_COMPANIES = 400  # DESIGN CHOICE, not a measurement
MAX_RETURNED_NAMES = 5  # DESIGN CHOICE, not a measurement: per company and source; the rest become "+N other names"
MAX_DROPPED = 5  # DESIGN CHOICE, not a measurement: per call; the true count is kept in dropped_count
MAX_PROBLEMS = 20  # DESIGN CHOICE, not a measurement
NAME_MAX = 80  # DESIGN CHOICE, not a measurement
SHORT_MAX_LINES = 12  # DESIGN CHOICE, not a measurement
SHORT_MAX_CHARS = 1400  # DESIGN CHOICE, not a measurement
SHORT_MAX_ATTENTION = 4  # DESIGN CHOICE, not a measurement
CHUNK_LIMIT = 3500  # below Telegram's 4,096-character limit, with room for a header

DETAIL_HINT = "Detail: /wrun · /wrun &lt;company&gt; · /wrun full"   # escaped: the message is sent as Telegram HTML
_SOURCE_LABELS = {"hirebase": "Hirebase", "apify": "Apify", "resolver": "ATS resolver"}
_PREFIXES = (("gh_", "greenhouse"), ("lever_", "lever"), ("ashby_", "ashby"), ("wd_", "workday"),
             ("hirebase_", "hirebase"), ("apify_", "apify"))
_TAG = re.compile(r"<[^>]*>")
_PARTIAL_ENTITY = re.compile(r"&[#\w]*$")


def source_from_job_id(job_id):
    """greenhouse / lever / ashby / workday / hirebase / apify by job_id prefix (the ones _collect_posting uses);
    any other non-empty string is jsearch; empty or non-string is unknown."""
    if not isinstance(job_id, str) or not job_id:
        return "unknown"
    for prefix, name in _PREFIXES:
        if job_id.startswith(prefix):
            return name
    return "jsearch"


def _clean(value):
    return "" if value is None else str(value)[:NAME_MAX]


def _esc(value):
    return html.escape(str(value or ""), quote=False)


def _norm(name):
    return re.sub(r"[^a-z0-9]+", "", str(name or "").casefold())


def _label(source):
    return _SOURCE_LABELS.get(source) or str(source or "unknown").title()


def _new_source():
    return {"sent": "", "status": "", "statuses": [], "returned": 0, "kept": 0, "dropped": [], "dropped_count": 0,
            "postings": 0, "returned_names": {}, "other_names": 0, "error": "", "_folded": set()}


class RunRecorder:
    def __init__(self, run_id, started_at, build=None):
        self.run_id, self.started_at = _clean(run_id), _clean(started_at)
        self.build = {k: _clean((build or {}).get(k)) for k in ("commit", "short", "source")} if isinstance(build, dict) else \
            {"commit": "", "short": "", "source": "unknown"}
        self.counts, self.problems, self._companies = {}, [], {}
        self.truncated = False

    def _company(self, name):
        name = _clean(name).strip()
        if not name:
            return None
        row = self._companies.get(name)
        if row is None:
            if len(self._companies) >= MAX_COMPANIES:
                self.truncated = True
                return None
            row = self._companies[name] = {"sources": {}, "cards": 0, "digest": 0, "rejected": 0}
        return row

    def _src(self, company, source):
        row = self._company(company)
        if row is None:
            return None
        return row["sources"].setdefault(_clean(source) or "unknown", _new_source())

    def _name_seen(self, entry, name, n=1):
        names = entry["returned_names"]
        name = _clean(name)
        if not name:
            return
        if name in names or len(names) < MAX_RETURNED_NAMES:
            names[name] = names.get(name, 0) + n
        else:
            entry["_folded"].add(name)
            entry["other_names"] = len(entry["_folded"])
            self.truncated = True

    def note_source_call(self, source, sent_name, *, status, returned=0, kept=0, dropped=(), error=""):
        try:
            entry = self._src(sent_name, source)
            if entry is None:
                return
            entry["sent"] = _clean(sent_name)
            entry["status"] = _clean(status)
            if entry["status"] and entry["status"] not in entry["statuses"]:
                entry["statuses"].append(entry["status"])
            entry["returned"] += max(0, int(returned or 0))
            entry["kept"] += max(0, int(kept or 0))
            for pair in list(dropped or ()):
                name, reason = (pair[0], pair[1]) if isinstance(pair, (tuple, list)) and len(pair) >= 2 else (pair, "")
                entry["dropped_count"] += 1
                self._name_seen(entry, name)
                if len(entry["dropped"]) < MAX_DROPPED:
                    entry["dropped"].append([_clean(name), _clean(reason)])
                else:
                    self.truncated = True
            if error:
                entry["error"] = _clean(error)
        except Exception:
            pass

    def note_posting(self, source, company, returned_name):
        try:
            entry = self._src(company, source)
            if entry is None:
                return
            entry["postings"] += 1
            self._name_seen(entry, returned_name)
        except Exception:
            pass

    def note_outcome(self, company, cards=0, digest=0, rejected=0):
        try:
            row = self._company(company)
            if row is None:
                return
            row["cards"] += max(0, int(cards or 0))
            row["digest"] += max(0, int(digest or 0))
            row["rejected"] += max(0, int(rejected or 0))
        except Exception:
            pass

    def note_problem(self, text):
        try:
            if len(self.problems) >= MAX_PROBLEMS:
                self.truncated = True
            elif text:
                self.problems.append(_clean(text))
        except Exception:
            pass

    def set_counts(self, **kw):
        for key, value in kw.items():
            try:
                self.counts[str(key)] = int(value)
            except (TypeError, ValueError):
                continue

    def snapshot(self):
        companies = []
        for name in sorted(self._companies):
            row = self._companies[name]
            sources = {s: {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                           for k, v in e.items() if not k.startswith("_")} for s, e in row["sources"].items()}
            companies.append({"name": name, "sources": sources, "cards": row["cards"], "digest": row["digest"],
                              "rejected": row["rejected"]})
        return {"version": VERSION, "run_id": self.run_id, "started_at": self.started_at, "build": dict(self.build),
                "counts": dict(self.counts), "problems": list(self.problems), "companies": companies,
                "truncated": bool(self.truncated)}


def _cut(text, limit):
    """Cut to `limit` characters ending in "..." without leaving a broken tag or entity: tags are stripped from a
    line that has to be cut (a cut line must not carry an unclosed tag)."""
    if len(text) <= limit:
        return text
    plain = _TAG.sub("", text)
    plain = plain if len(plain) <= limit else _PARTIAL_ENTITY.sub("", plain[:max(0, limit - 3)]) + "..."
    return plain


def _build_line(snapshot):
    build = snapshot.get("build") or {}
    short = build.get("short") or ""
    return f"Build {_esc(short)} ({_esc(build.get('source') or 'unknown')})" if short else "Build unknown"


def _sources_line(snapshot):
    counts, per = snapshot.get("counts") or {}, {}
    for c in snapshot.get("companies") or []:
        for s, e in (c.get("sources") or {}).items():
            if e.get("sent"):
                agg = per.setdefault(s, [0, 0])
                agg[0] += 1
                agg[1] += int(e.get("returned") or 0)
    parts = []
    if counts.get("boards_total") is not None:
        parts.append(f"Boards {counts.get('boards_scanned', 0)} of {counts['boards_total']}")
    if "hirebase" in per:
        parts.append(f"Hirebase {per['hirebase'][0]} companies, {per['hirebase'][1]} jobs")
    if "apify" in per:
        parts.append(f"Apify {per['apify'][0]} companies, {per['apify'][1]} rows")
    if "resolver" in per:
        parts.append(f"ATS resolver {per['resolver'][0]} companies")
    return " · ".join(parts)


def compose_short(snapshot, *, headline, money_line="", attention=()):
    """The short Telegram message: at most 12 lines and 1,400 characters, ALWAYS. Order: headline, screened line,
    sources line, up to 4 attention lines (+ "(+N more in /wrun)"), money line, build line, detail hint. Over the
    caps, attention lines go first, then long lines are cut; the build line and the detail hint stay."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    counts = snapshot.get("counts") or {}
    head = [ln for ln in str(headline or "").splitlines() if ln.strip()][:3]
    mid = []
    if counts.get("screened") is not None:
        mid.append(f"Screened {counts['screened']}: {counts.get('cards', 0)} cards, {counts.get('digest', 0)} digest, "
                   f"{counts.get('below_70', 0)} below 70")
    sources = _sources_line(snapshot)
    if sources:
        mid.append(sources)
    attn = [str(a) for a in (attention or ()) if str(a).strip()]
    tail = ([str(money_line)] if money_line else [])
    protected = [_build_line(snapshot), DETAIL_HINT]

    def assemble(n_attn):
        shown = attn[:n_attn]
        extra = len(attn) - len(shown)
        return head + mid + shown + ([f"(+{extra} more in /wrun)"] if extra else []) + tail + protected

    n = min(SHORT_MAX_ATTENTION, len(attn))
    lines = assemble(n)
    while (len(lines) > SHORT_MAX_LINES or len("\n".join(lines)) > SHORT_MAX_CHARS) and n > 0:
        n -= 1
        lines = assemble(n)
    # Still too long: cut the longest cuttable line until it fits (the last two lines are the protected ones).
    guard = 0
    while len("\n".join(lines)) > SHORT_MAX_CHARS and guard < 50:
        guard += 1
        idx = max(range(len(lines) - 2), key=lambda i: len(lines[i]), default=None)
        if idx is None or len(lines[idx]) <= 20:
            break
        over = len("\n".join(lines)) - SHORT_MAX_CHARS
        lines[idx] = _cut(lines[idx], max(20, len(lines[idx]) - over))
    while len(lines) > SHORT_MAX_LINES and len(lines) > 2:   # a hostile headline/money line: drop from the top
        lines.pop(0 if len(lines) - 2 > 0 and lines[0] in head else len(lines) - 3)
    return "\n".join(lines)


def _find_company(snapshot, query):
    q = str(query or "").strip().casefold()
    companies = snapshot.get("companies") or []
    exact = [c for c in companies if str(c["name"]).casefold() == q]
    if exact:
        return exact, None
    if not q:
        return [], None
    prefix = [c for c in companies if str(c["name"]).casefold().startswith(q)]
    found = prefix or [c for c in companies if q in str(c["name"]).casefold()]
    return found, None


def render_company(snapshot, query):
    """Text for ONE company: case-insensitive exact name, else a unique prefix/substring; several matches list the
    candidates (10 at most); none says so. Every name is escaped."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    found, _ = _find_company(snapshot, query)
    if not found:
        return f'No company matching "{_esc(query)}" in run {_esc(snapshot.get("run_id"))}'
    if len(found) > 1:
        names = ", ".join(_esc(c["name"]) for c in found[:10])
        more = f" (+{len(found) - 10} more)" if len(found) > 10 else ""
        return f'Several companies match "{_esc(query)}": {names}{more}. Send a longer name.'
    c = found[0]
    lines = [f"<b>{_esc(c['name'])}</b> (run {_esc(snapshot.get('run_id'))})"]
    sources = c.get("sources") or {}
    if not sources:
        lines.append("no source returned anything for this company this run")
    for s, e in sources.items():
        names = e.get("returned_names") or {}
        kept_names = ", ".join(f'"{_esc(n)}" ×{k}' for n, k in names.items()) or "nothing"
        if e.get("other_names"):
            kept_names += f", +{e['other_names']} other names"
        if e.get("sent"):
            line = (f'{_label(s)}: sent "{_esc(e["sent"])}" -> returned {kept_names} · kept {e.get("kept", 0)} '
                    f'· dropped {e.get("dropped_count", 0)}')
            if e.get("status") and e["status"] != "ok":
                line += f" · {_esc(e['status'])}"
            if e.get("error"):
                line += f" · error: {_esc(e['error'])}"
        else:
            line = f"{_label(s)}: {e.get('postings', 0)} postings, returned {kept_names}"
        lines.append(line)
        tally = {}
        for name, reason in e.get("dropped") or []:
            tally[(name, reason)] = tally.get((name, reason), 0) + 1
        if tally:
            lines.append("   dropped: " + ", ".join(f'"{_esc(n)}" ({_esc(r or "dropped")}) ×{k}' for (n, r), k in tally.items()))
    if c.get("cards") or c.get("digest") or c.get("rejected"):
        lines.append(f"cards {c.get('cards', 0)} · digest {c.get('digest', 0)} · rejected {c.get('rejected', 0)}")
    return "\n".join(lines)


def chunk(text, limit=CHUNK_LIMIT):
    """Pieces of at most `limit` characters, split on line boundaries (so never inside an HTML tag). A single line
    longer than `limit` is cut at limit - 3 with "...". 3,500 sits below Telegram's 4,096-character limit with room.
    Joining the pieces with a newline gives the input back line for line."""
    text = "" if text is None else str(text)
    pieces, current = [], None
    for line in text.split("\n"):
        if len(line) > limit:
            line = _cut(line, limit)
        if current is None:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current += "\n" + line
        else:
            pieces.append(current)
            current = line
    pieces.append(current if current is not None else "")
    return pieces


def render_full(detail_text):
    return chunk(detail_text)


def render_overview(snapshot):
    """Message-sized strings: a header (run id, start, build) then one line per company, sorted by name:
    `Name — greenhouse 4 · apify 2 | sent≠returned: Haworth International`. The sent≠returned clause appears only when a
    returned name differs from the sent name after case folding and punctuation removal."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    short = (snapshot.get("build") or {}).get("short") or ""
    build_text = f"build {_esc(short)} ({_esc((snapshot.get('build') or {}).get('source') or 'unknown')})" if short else "build unknown"
    lines = [f"Run {_esc(snapshot.get('run_id'))} · started {_esc(snapshot.get('started_at'))} · {build_text}"]
    for c in sorted(snapshot.get("companies") or [], key=lambda c: str(c["name"]).casefold()):
        sources = c.get("sources") or {}
        if not sources:
            lines.append(f"{_esc(c['name'])} — no source returned anything")
            continue
        parts, differ = [], []
        for s, e in sources.items():
            parts.append(f"{s} {e.get('postings') or e.get('kept') or 0}")
            if e.get("sent"):
                for n in (e.get("returned_names") or {}):
                    if _norm(n) != _norm(e["sent"]) and n not in differ:
                        differ.append(n)
        line = f"{_esc(c['name'])} — " + " · ".join(parts)
        if differ:
            line += " | sent≠returned: " + ", ".join(_esc(n) for n in differ)
        lines.append(line)
    return chunk("\n".join(lines))
