"""One-off, per-posting resume: the engine's Typst resume with hand-approved text swapped in.

Usage:  python scripts/custom_resume.py custom_resumes/specs/<role>.json

The spec is JSON, written after Kevin's interview answers (see memory: custom-resume-workflow):
  company   str   - name printed in the PDF filename and passed to the renderer
  track     str   - engine track letter (a-h)
  tone      str   - "conservative" | "tech"
  replace   list  - [{"old": exact text in the rendered Typst, "new": replacement}]; each `old`
                    must match EXACTLY ONCE or the run aborts (so a stale anchor fails loudly)
  insert_before list - [{"anchor": exact text, "text": Typst to insert before it}]
  jd_terms  list  - JD keywords to report present/absent (substring, case-insensitive)
  out_dir   str   - where the PDF and preview PNG go (default: custom_resumes/out)

It never writes to the banks or the CRM and is not wired into the send path: the PDF is attached
by hand. Gates run on the compiled PDF text: real page count, banned words, keyword coverage.
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
import resume_engine  # noqa: E402
from render_gate import banned_hits  # noqa: E402
from pipeline_utils import negative_language_hits  # noqa: E402


def _once(markup: str, anchor: str) -> None:
    n = markup.count(anchor)
    if n != 1:
        raise SystemExit(f"anchor matched {n} times (need exactly 1): {anchor[:80]!r}")


def build_markup(spec: dict) -> str:
    markup = resume_engine.render_typst_markup(
        spec["company"], spec.get("track", "a"), None, spec.get("tone", "conservative"), job_text="")
    for r in spec.get("replace", []):
        _once(markup, r["old"])
        markup = markup.replace(r["old"], r["new"])
    for ins in spec.get("insert_before", []):
        _once(markup, ins["anchor"])
        markup = markup.replace(ins["anchor"], ins["text"] + ins["anchor"])
    return markup


def main(spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    markup = build_markup(spec)
    out_dir = Path(spec.get("out_dir") or ROOT / "custom_resumes" / "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", spec["company"]).strip("_")
    pdf = typst.compile(markup.encode("utf-8"))
    pdf_path = out_dir / f"Kevin_Miller_Resume_{slug}.pdf"
    pdf_path.write_bytes(pdf)
    png = typst.compile(markup.encode("utf-8"), format="png", ppi=110)
    (out_dir / f"preview_{slug}.png").write_bytes(png if isinstance(png, bytes) else png[0])

    reader = PdfReader(io.BytesIO(pdf))
    text = "\n".join(p.extract_text() for p in reader.pages)
    low = text.lower()
    terms = spec.get("jd_terms", [])
    print(f"pdf      : {pdf_path}")
    print(f"pages    : {len(reader.pages)}  {'OK' if len(reader.pages) == 1 else 'FAIL - cut a line'}")
    print(f"banned   : {banned_hits(text, resume_engine.load_evidence_bank().get('banned_words', []))}")
    print(f"negative : {negative_language_hits(text)}  (pre-existing 'no cost'/'Limited Power of Attorney' are fine)")
    if terms:
        have = [t for t in terms if t.lower() in low]
        print(f"jd terms : {len(have)}/{len(terms)} present")
        print(f"  absent : {[t for t in terms if t.lower() not in low]}")
    if len(reader.pages) != 1:
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
