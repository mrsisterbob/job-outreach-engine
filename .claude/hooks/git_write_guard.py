"""PreToolUse hook (Bash): the build subagent may not write git while a task is active.

Denies only when the payload carries agent_id (a tool call fired inside a subagent) AND
.queue/active/ holds a task file. A call with no agent_id is the worker session or Kevin and is
allowed: the worker's Finish runs commit/merge/worktree add while the task is still in active/.
UNCONFIRMED: nothing here has seen a real payload; if main-thread calls also carried agent_id
this would deny the worker's own merge. So every deny, and every git write allowed only because
agent_id was absent, is logged to .queue/guard.log with agent_id=yes/no.

Parsing is best-effort. Caught: chained commands (&&, ||, ;, |, &, newlines, quote-aware),
global options (-C, -c, --git-dir, --work-tree, ...), env/command/sudo prefixes, and one
level of  bash -c "git ..."  /  sh -c '...'  re-parsing. NOT caught: script files, $(...),
backticks, variables, aliases, python -c "subprocess...", eval, xargs.
Deny: JSON on stdout, exit 0. Allow: no output. Kill switch: .queue/guards.off.
"""

import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import queue_guard_common as q  # noqa: E402

ALWAYS_DENY = {"commit", "reset", "checkout", "push", "rebase", "merge", "switch", "restore",
               "cherry-pick", "revert", "clean", "pull"}
GLOBAL_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                   "--super-prefix", "--config-env"}
BRANCH_WRITE = {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "-f",
                "--force", "--set-upstream-to", "-u", "--unset-upstream", "--edit-description"}
BRANCH_READ = {"--list", "-l", "-a", "-r", "-v", "-vv", "--show-current", "--contains",
               "--all", "--remotes", "--verbose", "--merged", "--no-merged", "--points-at"}
PREFIXES = {"env", "command", "exec", "time", "nohup", "sudo", "nice"}
SHELLS = {"bash", "sh", "zsh", "dash", "bash.exe", "sh.exe"}
FIRST_DENY_NOTE = " If this is the worker's own merge, create .queue/guards.off in the main folder."


def split_segments(cmd):
    """Split on unquoted && || ; | & and newlines."""
    segs, cur, quote, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(cmd):
                i += 1
                cur.append(cmd[i])
            elif c == quote:
                quote = None
        elif c == "\\" and i + 1 < len(cmd):
            cur.append(c)
            i += 1
            cur.append(cmd[i])
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c in ";|&\n":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    segs.append("".join(cur))
    return [s for s in segs if s.strip()]


def classify(words):
    """Return the denied subcommand description for words after 'git', else None."""
    i = 0
    while i < len(words):
        w = words[i]
        if w in GLOBAL_WITH_ARG:
            i += 2
        elif w.startswith("-"):
            i += 1
        else:
            break
    if i >= len(words):
        return None
    sub, args = words[i], words[i + 1:]
    if sub in ALWAYS_DENY:
        return sub
    if sub == "stash":
        return None if args and args[0] in ("list", "show") else "stash"
    if sub == "branch":
        return branch_write(args)
    return None


BRANCH_LONG_WRITE = ("--delete", "--move", "--copy", "--force", "--set-upstream-to",
                     "--unset-upstream", "--edit-description", "--track", "--no-track")
BRANCH_VALUE_FLAGS = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
                      "--sort", "--format"}


def branch_write(args):
    """Description if `git branch <args>` writes, else None. Read-only = flags plus --list patterns."""
    listing, skip = False, False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            name = a.split("=")[0]
            if name in BRANCH_LONG_WRITE:
                return "branch " + " ".join(args)
            if name == "--list":
                listing = True
            if name in BRANCH_VALUE_FLAGS and "=" not in a:
                skip = True
        elif a.startswith("-") and len(a) > 1:
            if set(a[1:]) & set("dDmMcCfut"):
                return "branch " + " ".join(args)
            if "l" in a[1:]:
                listing = True
        elif not listing:
            return "branch " + " ".join(args)
    return None


HEREDOC_RE = re.compile(r"<<(?!<)-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
LEADERS = {"(", "{", "then", "do", "else", "elif", "if", "while", "until", "!", "time"}


def drop_heredoc_bodies(cmd):
    out, term = [], None
    for line in cmd.split("\n"):
        if term is not None:
            if line.strip() == term:
                term = None
            continue
        out.append(line)
        m = HEREDOC_RE.search(line)
        if m:
            term = m.group(2)
    return "\n".join(out)


SHELL_C_RE = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
PATH_BS_RE = re.compile(r"(?<=[A-Za-z0-9_.:])\\(?=[A-Za-z0-9_.])")


def find_git_write(cmd, depth=0):
    for seg in split_segments(drop_heredoc_bodies(cmd)):
        seg = PATH_BS_RE.sub("/", seg)
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        while words:
            w = words[0]
            if w in LEADERS or w in PREFIXES or ("=" in w and not w.startswith("-")):
                words = words[1:]
            elif len(w) > 1 and w[0] in "({":
                words[0] = w.lstrip("({")
            else:
                break
        if not words:
            continue
        head = os.path.basename(words[0].replace("\\", "/")).lower()
        if head in SHELLS and depth < 2:
            for k, w in enumerate(words):
                if SHELL_C_RE.match(w):
                    if k + 1 < len(words):
                        hit = find_git_write(words[k + 1], depth + 1)
                        if hit:
                            return hit
                    break
        elif head in ("git", "git.exe"):
            hit = classify(words[1:])
            if hit:
                return "git " + hit
    return None


def decide(payload):
    if payload.get("tool_name") != "Bash":
        return
    cmd = (payload.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str) or not cmd:
        return
    hit = find_git_write(cmd)
    if not hit:
        return
    has_agent = bool(payload.get("agent_id"))
    flat = " ".join(cmd.split())[:300]
    for main in q.main_folders(payload):
        if q.guards_off(main):
            return
        try:
            active = [n for n in os.listdir(os.path.join(main, ".queue", "active"))
                      if n.lower().endswith(".md")]
        except OSError:
            continue
        if not active:
            continue
        if not has_agent:
            q.log_deny(main, "git-write-allowed", flat, "agent_id=no")
            return
        q.log_deny(main, "git-write-denied", flat, "agent_id=yes")
        q.deny("git write guard: refused `%s` (%s). The build agent leaves changes uncommitted; "
               "the worker session commits and merges. Read-only git (status, diff, log, show) "
               "is fine. Kill switch: create .queue/guards.off in the main folder (delete it to "
               "re-arm).%s" % (flat, hit, FIRST_DENY_NOTE))
        return


if __name__ == "__main__":
    q.run_failing_open(decide)
