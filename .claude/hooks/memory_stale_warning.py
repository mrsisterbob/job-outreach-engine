"""SessionStart hook: warn about memory files whose optional `stale_after: YYYY-MM-DD`
frontmatter line has passed (strictly before today).

Reads only the first `---` block of each *.md in the memory folder (MEMORY.md skipped). The
field may be top-level or indented (under `metadata:`). Bad dates are skipped. Prints ONE JSON
object (systemMessage for Kevin, hookSpecificOutput.additionalContext for Claude) or nothing.
Memory folder override: env MEMORY_STALE_DIR. Fail open: any error exits 0 silently.
"""

import datetime
import json
import os
import re
import sys

DEFAULT_DIR = "C:/Users/kjmil/.claude/projects/C--Users-kjmil-job-outreach-engine/memory/"
LIMIT = 10000
FIELD = re.compile(r"^\s*stale_after:\s*[\"']?(\d{4}-\d{2}-\d{2})[\"']?\s*$")


def frontmatter_date(path):
    """The stale_after date in the first frontmatter block, or None."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        lines = f.read().splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        m = FIELD.match(line)
        if m:
            try:
                return datetime.date.fromisoformat(m.group(1))
            except ValueError:
                return None
    return None


def stale_files(folder, today):
    found = []
    for name in os.listdir(folder):
        if not name.endswith(".md") or name == "MEMORY.md":
            continue
        try:
            d = frontmatter_date(os.path.join(folder, name))
        except Exception:
            continue
        if d is not None and d < today:
            found.append((d, name[:-3]))
    return sorted(found)


def build_message(found):
    head = "Memory past stale_after (%d): " % len(found)
    parts = ["%s (%s)" % (n, d.isoformat()) for d, n in found]
    msg = head + ", ".join(parts)
    if len(msg) > LIMIT - 1500:
        msg = msg[: LIMIT - 1500]
    return msg


def main():
    try:
        sys.stdin.read()
        folder = os.environ.get("MEMORY_STALE_DIR") or DEFAULT_DIR
        if not os.path.isdir(folder):
            return
        found = stale_files(folder, datetime.date.today())
        if not found:
            return
        msg = build_message(found)
        out = {
            "systemMessage": msg,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": msg + ". Do not trust these memory files; verify before acting on them.",
            },
        }
        sys.stdout.write(json.dumps(out))
    except Exception:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
