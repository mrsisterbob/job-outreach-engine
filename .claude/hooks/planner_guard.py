"""PreToolUse hook (Edit|Write|NotebookEdit): a session named `planner` may write inside the
project folder only under .queue/inbox/. Every other session is untouched.

Signal: payload transcript_path; /rename writes {"type":"custom-title","customTitle":...}
lines into it. The LAST such line wins. Fail open: no title, a title other than exactly
`planner`, or an unreadable transcript means no constraint.
Known soft spots: Bash redirects/heredocs are not covered; a subagent's transcript_path may
not be its parent's; paths outside the project folder (scratchpad, memory) are allowed.
Deny: JSON on stdout, exit 0. Allow: no output, exit 0. Kill switch: .queue/guards.off.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import queue_guard_common as q  # noqa: E402


def session_title(transcript_path):
    """Last customTitle in the transcript, or None."""
    title = None
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"custom-title"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("type") == "custom-title":
                title = obj.get("customTitle")
    return title


def strip_device_prefix(path):
    """Drop a leading device prefix (question-mark or dot form); UNC form maps to a plain UNC path."""
    for pre in ("\\?\\", "\\.\\", "//?/", "//./"):
        if path.startswith(pre):
            rest = path[len(pre):]
            if rest[:4].upper() == "UNC\\" or rest[:4].upper() == "UNC/":
                return "\\\\" + rest[4:]
            return rest
    return path


def decide(payload):
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    raw = ti.get("file_path") or ti.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return
    tp = payload.get("transcript_path")
    if not isinstance(tp, str) or not tp:
        return
    if session_title(tp) != "planner":
        return
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    raw = strip_device_prefix(raw)
    if not os.path.isabs(raw):
        raw = os.path.join(cwd, raw)
    file_norm = q.norm(os.path.realpath(raw))
    for main in q.main_folders(payload) or [cwd]:
        if q.guards_off(main):
            return
        main = os.path.realpath(strip_device_prefix(main))
        mn = q.norm(main)
        if not file_norm.startswith(mn + "/"):
            continue
        if file_norm.startswith(mn + "/.queue/inbox/"):
            return
        q.log_deny(main, tool, raw, "planner")
        q.deny("planner guard: refused %s. The planner session may write only under "
               ".queue/inbox/. Queue a task for the worker instead (/queue-add). "
               "Kill switch: create .queue/guards.off in the main folder (delete it to re-arm)."
               % raw)
        return


if __name__ == "__main__":
    q.run_failing_open(decide)
