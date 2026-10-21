"""Federal-format variant of a custom resume: hours per week and pay on every job, 10pt text, 0.5in margins.

Usage:  python scripts/federal_resume.py custom_resumes/specs/<role>.json [hours_40_acres]

Takes the same spec as custom_resume.py, renders its markup, then applies the USAJOBS layout rules
(help panel on the resume step: 10pt body, 0.5in margins, at most two pages) and adds one detail line
under each job. Hours and pay are Kevin's own words (2026-10-21), kept in JOBS below. 40 Acres hours
are not on record yet: pass them as the second argument, or the PDF is written as a DRAFT.
Supervisor names and phones are never written to a file; Kevin types them into the USAJOBS builder.
"""
import io
import json
import re
import sys
from pathlib import Path

import typst
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import custom_resume  # noqa: E402
import resume_engine  # noqa: E402
from render_gate import banned_hits  # noqa: E402

DETAIL = 'text(size: 9pt, fill: rgb("#4B5563"))'
BULLET = chr(0x2022)

# header line as rendered -> (replacement header or None, detail line)
JOBS = {
    "*Wealth Operations Specialist Intern* | Signal Advisors": (
        None, "Hours per week: 40 | Pay: \\$20.00 per hour"),
    "*Operations Consultant* | 40 Acres": (
        "*Operations Consultant (Volunteer)* | 40 Acres", "Hours per week: {hours} | Pay: unpaid volunteer"),
    "*Financial Planning Intern* | Northwestern Mutual": (
        None, "Hours per week: 40 | Pay: commission only (independent contractor, 1099)"),
    "*Total Rewards Finance Intern* | ABC Technologies": (
        None, "Hours per week: 40 | Pay: \\$16.00 per hour"),
}


def build(spec: dict, hours_40_acres: str | None) -> str:
    markup = custom_resume.build_markup(spec)
    markup = markup.replace("margin: (x: 0.65in, top: 0.40in, bottom: 0.40in)", "margin: 0.5in")
    markup = markup.replace("size: 9.4pt", "size: 10pt")
    markup = markup.replace("Resume - " + spec["company"], "Federal Resume - " + spec["company"])
    markup = markup.replace("Secretary, Student Political Organization " + BULLET + " ", "")
    for header, (new_header, detail) in JOBS.items():
        lines = [l for l in markup.splitlines() if l.startswith(header)]
        if len(lines) != 1:
            raise SystemExit(f"header matched {len(lines)} times (need 1): {header}")
        line = lines[0]
        shown = new_header + line[len(header):] if new_header else line
        text = detail.replace("{hours}", hours_40_acres or "?? (draft)")
        markup = markup.replace(line, shown + " " + chr(92) + "\n#" + DETAIL + "[" + text + "]")
    return markup


def main(spec_path: str, hours: str | None) -> None:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    markup = build(spec, hours)
    out_dir = Path(spec.get("out_dir") or ROOT / "custom_resumes" / "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", spec["company"]).strip("_")
    tag = "" if hours else "_DRAFT"
    pdf = typst.compile(markup.encode("utf-8"))
    pdf_path = out_dir / f"Kevin_Miller_Federal_Resume_{slug}{tag}.pdf"
    pdf_path.write_bytes(pdf)
    pngs = typst.compile(markup.encode("utf-8"), format="png", ppi=110)
    for i, png in enumerate(pngs if isinstance(pngs, list) else [pngs], 1):
        (out_dir / f"preview_federal_{slug}_p{i}.png").write_bytes(png)
    reader = PdfReader(io.BytesIO(pdf))
    text = "\n".join(p.extract_text() for p in reader.pages)
    print(f"pdf      : {pdf_path}")
    print(f"pages    : {len(reader.pages)}  {'OK' if len(reader.pages) <= 2 else 'FAIL - over two pages'}")
    print(f"banned   : {banned_hits(text, resume_engine.load_evidence_bank().get('banned_words', []))}")
    print(f"40 Acres hours: {hours or 'NOT SET - draft'}")
    if len(reader.pages) > 2:
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)
