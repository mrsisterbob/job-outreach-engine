"""Per-stage counts plus a small capped sample of what /w kept or dropped.

Pure logic: no I/O, no clock, no imports from main.py. The counters in /w say how
many roles a filter dropped; this keeps a few titles so diversity can be judged.
"""
import html

STAGES = (
    ("pulled", "Pulled (reached the gates)"),
    ("title_filter", "Dropped by title filter"),
    ("region", "Dropped by region gate"),
    ("remote", "Dropped as pure Remote"),
    ("michigan_screen", "Dropped by Michigan screen"),
    ("ai_screen", "Rejected by AI screen"),
    ("card", "Kept as a card"),
)
STAGE_SAMPLE_CAP = 10  # DESIGN CHOICE, not a measurement
STAGE_PER_COMPANY_CAP = 2  # DESIGN CHOICE
FIELD_MAX = 80
REPORT_MAX = 3500
_STAGE_NAMES = tuple(s for s, _ in STAGES)


def _clean(value):
    if value is None:
        return ""
    return str(value)[:FIELD_MAX]


class StageSampler:
    """Counts every note per stage; keeps samples only under both caps."""

    def __init__(self):
        self._total = {s: 0 for s in _STAGE_NAMES}
        self._samples = {s: [] for s in _STAGE_NAMES}

    def note(self, stage, company, title, detail=""):
        if stage not in self._total:
            return
        self._total[stage] += 1
        samples = self._samples[stage]
        if len(samples) >= STAGE_SAMPLE_CAP:
            return
        company_s = _clean(company)
        if sum(1 for x in samples if x["company"] == company_s) >= STAGE_PER_COMPANY_CAP:
            return
        samples.append({"company": company_s, "title": _clean(title), "detail": _clean(detail)})

    def snapshot(self):
        return {
            s: {"total": self._total[s], "samples": [dict(x) for x in self._samples[s]]}
            for s in _STAGE_NAMES
        }


def snapshot_is_empty(snapshot):
    """True when every stage total is 0 (or the snapshot is unusable)."""
    try:
        return all(int(snapshot[s]["total"]) == 0 for s in _STAGE_NAMES)
    except Exception:
        return True


def _sample_line(x):
    line = "%s - %s" % (html.escape(str(x["company"])), html.escape(str(x["title"])))
    if x.get("detail"):
        line += " (%s)" % html.escape(str(x["detail"]))
    return line


def _render(snapshot, run_label, shown, trimmed):
    out = ["<b>%s</b>" % html.escape(str(run_label))]
    for stage, label in STAGES:
        total = int(snapshot[stage]["total"])
        out.append("")
        if total == 0:
            out.append("<b>%s</b>: 0" % html.escape(label))
            continue
        out.append("<b>%s</b>: %d" % (html.escape(label), total))
        for x in snapshot[stage]["samples"][: shown[stage]]:
            out.append(_sample_line(x))
    if trimmed:
        out.append("")
        out.append("(samples trimmed)")
    return "\n".join(out)


def format_stage_report(snapshot, run_label):
    """Telegram HTML report, at most REPORT_MAX chars. 'no stage data' if malformed."""
    try:
        shown = {s: len(snapshot[s]["samples"]) for s in _STAGE_NAMES}
        text = _render(snapshot, run_label, shown, False)
        if len(text) <= REPORT_MAX:
            return text
        while True:
            text = _render(snapshot, run_label, shown, True)
            if len(text) <= REPORT_MAX:
                return text
            biggest = max(_STAGE_NAMES, key=lambda s: (shown[s], int(snapshot[s]["total"])))
            if shown[biggest] == 0:
                return text[:REPORT_MAX]
            shown[biggest] -= 1
    except Exception:
        return "no stage data"
