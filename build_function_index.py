"""Regenerates FUNCTION_INDEX.md: every function in the codebase with its one-line summary.

WHY THIS EXISTS. main.py grew 9,311 -> 13,318 lines in five days. At that size nobody - human or
agent - can hold the whole namespace in their head, and the failure mode is not that the file is
hard to read. It is that you cannot SEE what already exists, so you write it again.

That is not hypothetical: on 2026-09-23 a `/linkedin` command was built with a hand-rolled scan
over four CRM tabs, while Code.gs had already exposed a server-side `find_contact_by_email` that
searched every tab. The duplicate passed every test - duplicates never fail tests - and was only
caught by a later manual sweep. One grep against this index would have surfaced the existing
helper in seconds.

So this is a discoverability tool, not documentation. The workflow it is built for is:

    grep -i email FUNCTION_INDEX.md      # before writing anything that touches email
    grep -i "contact.*lookup" FUNCTION_INDEX.md

Run it before adding a subsystem, and after adding one so the index stays current. Like
update_readme_stats.py it is deliberately NOT wired into a hook or CI: this repo has no
CI-backed remote, and a stale index that nobody ran is worse than one you regenerate on purpose.
Re-running is safe any number of times - the file is rewritten from source every time.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
INDEX_PATH = ROOT / "FUNCTION_INDEX.md"

# Source files worth indexing. Tests are excluded: they are discoverable by the name of the thing
# they test, and including them would triple the index without making anything easier to find.
EXCLUDE_PREFIXES = ("test_",)
EXCLUDE_DIR_PARTS = ("tests", "__pycache__", "fixtures", "crypto_engine")


def tracked_python_files() -> list[Path]:
    """Every tracked .py file worth indexing, via git so build artifacts never appear."""
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    files = []
    for name in result.stdout.splitlines():
        path = ROOT / name
        if not path.is_file() or path.suffix != ".py":
            continue
        if path.name.startswith(EXCLUDE_PREFIXES):
            continue
        if any(part in EXCLUDE_DIR_PARTS for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def summarize(node: ast.AST) -> str:
    """The function's first docstring line, flattened to one line.

    First line only, on purpose: the index is for scanning, and a function whose summary needs a
    paragraph is one you should open rather than read here. A function with no docstring still
    gets a row - its NAME is the thing being searched for, and omitting it would hide exactly the
    small helpers most likely to be rewritten by accident.
    """
    doc = ast.get_docstring(node)
    if not doc:
        return "_(no docstring)_"
    first = doc.strip().split("\n", 1)[0].strip()
    # Escape the pipe so a summary containing one cannot break the Markdown table row.
    return first.replace("|", "\\|") or "_(no docstring)_"


def collect(path: Path) -> list[tuple[int, str, str]]:
    """(lineno, qualified_name, summary) for every function in one file, source order.

    Methods are qualified as "Class.method" so a name that only makes sense in context reads
    correctly in a flat list. Nested helpers are skipped: they are not reachable from elsewhere,
    so they can never be the thing you failed to find.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as e:
        print(f"  ! skipped {path.name}: {e}", file=sys.stderr)
        return []

    rows: list[tuple[int, str, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rows.append((node.lineno, node.name, summarize(node)))
        elif isinstance(node, ast.ClassDef):
            rows.append((node.lineno, node.name, summarize(node)))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    rows.append((sub.lineno, f"{node.name}.{sub.name}", summarize(sub)))
    return sorted(rows)


def collect_crm_actions() -> list[tuple[int, str, str]]:
    """(lineno, action, summary) for every CRM action Code.gs handles.

    Indexed even though Code.gs is not Python, because this is exactly where the duplicate that
    motivated this tool came from: `find_contact_by_email` already existed as a server-side
    all-tabs lookup, a Python-only index did not list it, and it got rewritten by hand. The
    Apps Script side is a real part of the API surface and has to be searchable with everything
    else.

    The summary is the first comment line under the handler when there is one - Code.gs documents
    its actions in `//` blocks rather than docstrings.
    """
    path = ROOT / "Code.gs"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rows: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        match = re.search(r'action\s*===\s*"([a-z_]+)"', line)
        if not match:
            continue
        # Look back a few lines for a describing comment, skipping numbered section banners.
        summary = "_(no comment)_"
        for back in range(1, 6):
            if i - back < 0:
                break
            candidate = lines[i - back].strip()
            if candidate.startswith("//"):
                text = candidate.lstrip("/").strip()
                text = re.sub(r"^\d+[a-z]?\.\s*", "", text)
                if text:
                    summary = text.replace("|", "\\|")
                    break
        rows.append((i + 1, match.group(1), summary))
    return sorted(set(rows))


def build_index() -> str:
    parts = [
        "# Function Index",
        "",
        "Auto-generated by `build_function_index.py` - **do not edit by hand.**",
        "",
        "Search this file BEFORE writing a new helper. It exists because a duplicate never fails",
        "a test, so the only thing that catches one is looking first:",
        "",
        "```bash",
        "grep -i email FUNCTION_INDEX.md",
        "grep -i \"contact\" FUNCTION_INDEX.md",
        "```",
        "",
    ]

    total = 0
    files = tracked_python_files()
    for path in files:
        rows = collect(path)
        if not rows:
            continue
        total += len(rows)
        line_count = sum(1 for _ in path.open(encoding="utf-8", errors="ignore"))
        parts.append(f"## `{path.relative_to(ROOT).as_posix()}` — {len(rows)} defs, {line_count:,} lines")
        parts.append("")
        parts.append("| Line | Name | Summary |")
        parts.append("| ---: | :--- | :--- |")
        for lineno, name, summary in rows:
            parts.append(f"| {lineno} | `{name}` | {summary} |")
        parts.append("")

    crm_rows = collect_crm_actions()
    if crm_rows:
        parts.append("## `Code.gs` — CRM actions (the Apps Script API surface)")
        parts.append("")
        parts.append("Called from Python via `crm_post({\"action\": ...})` / `crm_get(...)`. "
                     "Check here before writing a CRM read or write by hand - a server-side "
                     "action searches every tab, which a Python-side scan usually does not.")
        parts.append("")
        parts.append("| Line | Action | Summary |")
        parts.append("| ---: | :--- | :--- |")
        for lineno, action, summary in crm_rows:
            parts.append(f"| {lineno} | `{action}` | {summary} |")
        parts.append("")
        total += len(crm_rows)

    parts.append(f"---")
    parts.append("")
    parts.append(f"**{total} definitions across {len(files)} files + Code.gs.**")
    parts.append("")
    return "\n".join(parts)


def main() -> None:
    index = build_index()
    INDEX_PATH.write_text(index, encoding="utf-8")
    defs = index.count("\n| ") - index.count("| ---: |") * 2
    print(f"Wrote {INDEX_PATH.name}: {defs} definitions, {len(index.splitlines())} lines")


if __name__ == "__main__":
    main()
