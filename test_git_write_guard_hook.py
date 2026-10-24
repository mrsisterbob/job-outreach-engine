"""git_write_guard.py PreToolUse hook, driven as a subprocess with the Bash payload on stdin."""

import json
import os
import subprocess
import sys

import pytest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude", "hooks",
                    "git_write_guard.py")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".queue" / "active").mkdir(parents=True)
    (tmp_path / ".queue" / "active" / "TASK-7-x.md").write_text("---\nid: TASK-7\n---\n",
                                                                  encoding="utf-8")
    return tmp_path


def run(proj, command=None, agent=True, stdin=None):
    if stdin is None:
        p = {"session_id": "s", "transcript_path": "t", "cwd": str(proj), "permission_mode": "default",
             "hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": command, "description": "d"}, "tool_use_id": "u"}
        if agent:
            p.update(agent_id="a1", agent_type="general-purpose")
        stdin = json.dumps(p)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(proj))
    return subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True, text=True, env=env)


def denied(r):
    assert r.returncode == 0
    return bool(r.stdout) and json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


DENY = ['git commit -m "x"', "git reset --hard HEAD~1", "git checkout -- f.py", "git push origin main",
        "git rebase main", "git merge --no-ff task/TASK-7", "git stash", "git branch -D x",
        "git branch newbranch", "git switch main", "git restore f", "git clean -fd",
        "git cherry-pick abc", "git revert abc", "git pull", "git stash pop"]
ALLOW = ["git status", "git diff", "git log --oneline", "git show HEAD", "git branch --list",
         "git branch", "git branch -a", "git stash list", "git stash show", "git merge-base a b",
         "git add -A", "git worktree list", "git ls-files", "ls -la", "python -m pytest -q",
         "echo git commit"]


@pytest.mark.parametrize("cmd", DENY)
def test_denied_from_subagent(proj, cmd):
    assert denied(run(proj, cmd))


@pytest.mark.parametrize("cmd", ALLOW)
def test_allowed_from_subagent(proj, cmd):
    r = run(proj, cmd)
    assert r.returncode == 0 and r.stdout == ""


def test_chained_quoted_ampersands(proj):
    assert denied(run(proj, 'cd wt && git commit -m "a && b"'))
    r = run(proj, 'git log --grep "x && git commit" && git status')
    assert r.stdout == ""
    assert denied(run(proj, 'git status; git push\ngit diff'))


def test_global_options(proj):
    assert denied(run(proj, "git -C .worktrees/TASK-7 commit -m x"))
    assert denied(run(proj, "git -c user.name=a --no-pager commit -m x"))
    assert run(proj, "git -C .worktrees/TASK-7 status").stdout == ""


def test_bash_c_wrapper(proj):
    assert denied(run(proj, "bash -c \"git commit -m 'a && b'\""))
    assert denied(run(proj, "sh -c 'cd x && git reset --hard'"))
    assert run(proj, 'bash -c "git status"').stdout == ""


def test_worker_without_agent_id_allowed_and_logged(proj):
    r = run(proj, "git -C .worktrees/TASK-7 commit -m x", agent=False)
    assert r.returncode == 0 and r.stdout == ""
    log = (proj / ".queue" / "guard.log").read_text()
    assert "agent_id=no" in log and "commit" in log


def test_deny_logged_and_message(proj):
    r = run(proj, "git commit -m x")
    reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "guards.off" in reason and "worker's own merge" in reason and "git commit" in reason
    assert "agent_id=yes" in (proj / ".queue" / "guard.log").read_text()


@pytest.mark.parametrize("cmd", DENY)
def test_empty_active_allows(proj, cmd):
    os.remove(proj / ".queue" / "active" / "TASK-7-x.md")
    r = run(proj, cmd)
    assert r.returncode == 0 and r.stdout == ""


def test_garbage_stdin(proj):
    for s in ("", "not json", "[1]", "null"):
        r = run(proj, stdin=s)
        assert r.returncode == 0 and r.stdout == ""


def test_kill_switch(proj):
    (proj / ".queue" / "guards.off").write_text("")
    r = run(proj, "git commit -m x")
    assert r.returncode == 0 and r.stdout == ""


def test_non_bash_tool_untouched(proj):
    p = {"tool_name": "Edit", "tool_input": {"command": "git commit"}, "agent_id": "a", "cwd": str(proj)}
    assert run(proj, stdin=json.dumps(p)).stdout == ""


@pytest.mark.parametrize("cmd", [
    "(git commit -m x)", "{ git commit -m x; }", "if true; then git commit -m x; fi",
    "! git commit", "time git commit -m x", "for f in a; do git push; done",
    "bash -lc \"git commit -m x\"", "bash -ec 'git reset --hard'", "sh -xc 'git push'",
    '"C:\\Program Files\\Git\\bin\\git.exe" commit -m x', "C:\\Git\\bin\\git.exe commit -m x",
    "C:\\Git\\bin\\GIT.EXE push",
    "git branch --contains HEAD newbranch", "git branch -v newbranch", "git branch -vD x",
    "git branch -dv x", "git branch -M x", "git branch --delete x", "git branch --set-upstream-to=o/x",
    "cat <<'EOF' > f\nhi\nEOF\ngit commit -m x",
])
def test_parser_holes_denied(proj, cmd):
    assert denied(run(proj, cmd))


@pytest.mark.parametrize("cmd", [
    "git branch --contains HEAD", "git branch -v", "git branch -vv", "git branch --list 'task/*'",
    "git branch -a --sort=-committerdate", "git branch --merged main", "git branch --show-current",
    "(git status)", "if true; then git diff; fi", "bash -lc 'git status'",
    '"C:\\Program Files\\Git\\bin\\git.exe" status',
    "cat >> log.md <<'EOF'\ngit commit -m x\ngit push\nEOF",
    "cat >> log.md <<EOF\ngit reset --hard\nEOF\ngit status",
    "cat <<-EOF\n\tgit push\n\tEOF",
])
def test_parser_allows(proj, cmd):
    r = run(proj, cmd)
    assert r.returncode == 0 and r.stdout == ""


def test_no_log_when_no_agent_and_empty_active(proj):
    os.remove(proj / ".queue" / "active" / "TASK-7-x.md")
    r = run(proj, "git commit -m x", agent=False)
    assert r.returncode == 0 and r.stdout == ""
    assert not (proj / ".queue" / "guard.log").exists()
