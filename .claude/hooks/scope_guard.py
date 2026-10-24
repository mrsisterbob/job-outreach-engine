"""PreToolUse hook (Edit|Write|NotebookEdit): a write inside .worktrees/TASK-n/ must be in
the scope: line of .queue/active/TASK-n-*.md in the main folder.

Keyed on PATH, not on "a task is active": Kevin and the planner edit the main folder while a
worker runs, so everything outside an active task's worktree is allowed. Known gap: a build
agent that writes a main-folder file instead of a worktree file is not caught.

Payload fields used (from this session's tool schemas): tool_name, tool_input.file_path
(Edit, Write), tool_input.notebook_path (NotebookEdit), cwd.
Deny: JSON on stdout, exit 0. Allow: no output, exit 0. Kill switch: .queue/guards.off.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import queue_guard_common as q  # noqa: E402


def decide(payload):
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    raw = ti.get("file_path") or ti.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    if not os.path.isabs(raw):
        raw = os.path.join(cwd, raw)
    file_norm = q.norm(raw)

    for main in q.main_folders(payload):
        mn = q.norm(main)
        prefix = mn + "/" + q.WORKTREES + "/"
        if not file_norm.startswith(prefix):
            continue
        seg = file_norm[len(prefix):].split("/")[0]
        if not q.TASK_DIR_RE.match(seg):
            continue
        task_id = seg.upper()
        if q.guards_off(main):
            return
        files = q.active_task_files(main, task_id)
        if not files:
            return
        scope = q.read_scope(files[0])
        if not scope:
            return
        wt_root = os.path.join(main, q.WORKTREES, seg)
        if q.scope_matches(file_norm, scope, wt_root, main):
            return
        q.log_deny(main, tool, raw, task_id)
        q.deny("%s scope guard: refused %s. This task's scope is: %s. Do not edit it; "
               "append the block to the task's ## Log and reply blocked - scope. "
               "Kill switch: create .queue/guards.off in the main folder (delete it to re-arm)."
               % (task_id, raw, ", ".join(scope)))
        return


if __name__ == "__main__":
    q.run_failing_open(decide)
