"""Shared helpers for the queue PreToolUse guards (scope_guard.py and friends).

Every helper fails open: a guard that crashes must look like an allow, never a wedge.
Kill switch: create .queue/guards.off in the main folder and every guard allows everything.
"""

import json
import os
import re
import sys
import time

WORKTREES = ".worktrees"
TASK_DIR_RE = re.compile(r"^(TASK-\d+)$", re.IGNORECASE)


def norm(path):
    """Absolute, slash-normalised, case-folded path with .. and . resolved."""
    p = os.path.abspath(path).replace("\\", "/")
    segs = []
    for seg in p.split("/"):
        if seg not in (".", ".."):
            seg = seg.rstrip(". ") or seg
        segs.append(seg)
    # Windows opens "a./b" as "a/b"; re-resolve after stripping so ".." still collapses.
    return os.path.normcase(os.path.abspath("/".join(segs))).replace("\\", "/")


def cut_at_worktrees(path):
    """Main folder implied by a path that sits under .worktrees, else None."""
    parts = path.replace("\\", "/").split("/")
    for i, seg in enumerate(parts):
        if seg.lower() == WORKTREES and i > 0:
            return "/".join(parts[:i])
    return None


def main_folders(payload):
    """Candidate main folders: CLAUDE_PROJECT_DIR first, then payload cwd cut at .worktrees."""
    out = []
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        out.append(cut_at_worktrees(env) or env)
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        cut = cut_at_worktrees(cwd)
        if cut:
            out.append(cut)
    return out


def guards_off(main):
    return os.path.exists(os.path.join(main, ".queue", "guards.off"))


def active_task_files(main, task_id):
    """Task files in .queue/active/ for this id. Trailing hyphen: TASK-7 never matches TASK-71."""
    d = os.path.join(main, ".queue", "active")
    prefix = task_id.upper() + "-"
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return [os.path.join(d, n) for n in sorted(names)
            if n.upper().startswith(prefix) and n.lower().endswith(".md")]


def read_scope(task_file):
    """List of scope entries from the first front-matter block, or None if absent."""
    try:
        with open(task_file, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except Exception:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.lower().startswith("scope:"):
            items = [s.strip().strip("`'\"") for s in line.split(":", 1)[1].split(",")]
            items = [s for s in items if s]
            return items or None
    return None


def scope_matches(file_norm, scope, worktree_root, main):
    """Whole-file match. Entries are repo-relative (resolved inside the worktree) or absolute.
    A trailing slash means nothing: the entry is matched literally, so it matches no file."""
    wt = norm(worktree_root)
    mn = norm(main)
    for entry in scope:
        e = entry.replace("\\", "/")
        if os.path.isabs(e) or re.match(r"^[A-Za-z]:/", e):
            target = norm(e)
            if target.startswith(mn + "/") and not target.startswith(mn + "/" + WORKTREES + "/"):
                if file_norm == wt + target[len(mn):]:
                    return True
            if file_norm == target:
                return True
        elif file_norm == norm(os.path.join(worktree_root, e)):
            return True
    return False


def log_deny(main, tool, path, task_id):
    try:
        with open(os.path.join(main, ".queue", "guard.log"), "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), tool, path, task_id))
    except Exception:
        pass


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))


def run_failing_open(fn):
    """Run a guard's main(payload); any exception, bad stdin or non-dict payload is an allow."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return
        fn(payload)
    except BaseException:
        return
