"""scope_guard.py PreToolUse hook, driven as a subprocess with the real payload on stdin."""

import json
import os
import subprocess
import sys

import pytest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude", "hooks", "scope_guard.py")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".queue" / "active").mkdir(parents=True)
    (tmp_path / ".queue" / "active" / "TASK-7-x.md").write_text(
        "---\nid: TASK-7\nscope: new_mod.py, test_new_mod.py\nverify: true\n---\nbody\nscope: main.py\n",
        encoding="utf-8")
    (tmp_path / ".worktrees" / "TASK-7").mkdir(parents=True)
    (tmp_path / ".worktrees" / "TASK-71").mkdir(parents=True)
    return tmp_path


def run(proj, stdin=None, payload=None, tool="Edit"):
    if stdin is None:
        stdin = json.dumps(payload)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(proj))
    r = subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True, text=True, env=env)
    return r


def pl(proj, path, tool="Edit", key="file_path"):
    return {"tool_name": tool, "tool_input": {key: str(path)}, "cwd": str(proj)}


def test_in_scope_allowed(proj):
    r = run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "new_mod.py"))
    assert r.returncode == 0 and r.stdout == ""


def test_out_of_scope_denied(proj):
    r = run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "main.py"))
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "TASK-7" in out["permissionDecisionReason"] and "main.py" in out["permissionDecisionReason"]
    assert "new_mod.py" in out["permissionDecisionReason"]
    log = (proj / ".queue" / "guard.log").read_text()
    assert "TASK-7" in log and "main.py" in log


def test_notebook_path_denied(proj):
    r = run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "n.ipynb", "NotebookEdit", "notebook_path"))
    assert "deny" in r.stdout


def test_no_active_task_allows(proj):
    os.remove(proj / ".queue" / "active" / "TASK-7-x.md")
    r = run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "main.py"))
    assert r.returncode == 0 and r.stdout == ""


def test_main_folder_path_allowed(proj):
    r = run(proj, payload=pl(proj, proj / "main.py"))
    assert r.returncode == 0 and r.stdout == ""


def test_task_file_log_append_allowed(proj):
    r = run(proj, payload=pl(proj, proj / ".queue" / "active" / "TASK-7-x.md"))
    assert r.returncode == 0 and r.stdout == ""


def test_dotdot_escape_denied(proj):
    p = str(proj / ".worktrees" / "TASK-7" / ".." / ".." / "main.py")
    # resolves to the main folder, which is allowed; escape sideways into another file in the worktree
    p2 = str(proj / ".worktrees" / "TASK-7" / "sub" / ".." / "main.py")
    r = run(proj, payload=pl(proj, p2))
    assert "deny" in r.stdout
    r = run(proj, payload=pl(proj, p))
    assert r.stdout == ""  # lands in the main folder, outside any worktree
    p3 = str(proj / ".worktrees" / "TASK-7" / ".." / "TASK-7" / "evil.py")
    assert "deny" in run(proj, payload=pl(proj, p3)).stdout


@pytest.mark.parametrize("stdin", ["not json {", "", "[]", '{"tool_input": null}'])
def test_garbage_stdin_fails_open(proj, stdin):
    r = run(proj, stdin=stdin)
    assert r.returncode == 0 and r.stdout == ""


def test_task_without_scope_fails_open(proj):
    (proj / ".queue" / "active" / "TASK-7-x.md").write_text("---\nid: TASK-7\n---\nbody\n", encoding="utf-8")
    r = run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "main.py"))
    assert r.returncode == 0 and r.stdout == ""


def test_no_prefix_confusion(proj):
    r = run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-71" / "main.py"))
    assert r.returncode == 0 and r.stdout == ""


def test_kill_switch_allows(proj):
    (proj / ".queue" / "guards.off").write_text("")
    r = run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "main.py"))
    assert r.returncode == 0 and r.stdout == ""


def test_unwritable_log_does_not_change_decision(proj):
    (proj / ".queue" / "guard.log").mkdir()
    r = run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "main.py"))
    assert "deny" in r.stdout


def run_env(proj, payload, with_env=True):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    if with_env:
        env["CLAUDE_PROJECT_DIR"] = str(proj)
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)


def assert_deny(r):
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def assert_allow(r):
    assert r.returncode == 0 and r.stdout == ""


def test_bom_task_file_still_denies(proj):
    (proj / ".queue" / "active" / "TASK-7-x.md").write_bytes(
        b"\xef\xbb\xbf---\nid: TASK-7\nscope: new_mod.py\n---\nbody\n")
    assert_deny(run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "main.py")))
    assert_allow(run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "new_mod.py")))


@pytest.mark.skipif(os.name != "nt", reason="trailing dot/space segments are a Windows path quirk")
def test_trailing_dot_and_space_segments_denied(proj):
    base = str(proj)
    assert_deny(run(proj, payload=pl(proj, base + "/.worktrees./TASK-7/main.py")))
    assert_deny(run(proj, payload=pl(proj, base + "/.worktrees/TASK-7 /main.py")))
    assert_deny(run(proj, payload=pl(proj, base + "/.worktrees/TASK-7/main.py.")))


def test_cwd_fallback_without_project_dir(proj):
    wt = proj / ".worktrees" / "TASK-7"
    deny_p = {"tool_name": "Edit", "tool_input": {"file_path": str(wt / "main.py")}, "cwd": str(wt)}
    ok_p = {"tool_name": "Edit", "tool_input": {"file_path": str(wt / "new_mod.py")}, "cwd": str(wt)}
    assert_deny(run_env(proj, deny_p, with_env=False))
    assert_allow(run_env(proj, ok_p, with_env=False))


def test_relative_path_joined_to_cwd(proj):
    wt = proj / ".worktrees" / "TASK-7"
    assert_deny(run_env(proj, {"tool_name": "Write", "tool_input": {"file_path": "main.py"}, "cwd": str(wt)}))
    assert_allow(run_env(proj, {"tool_name": "Write", "tool_input": {"file_path": "new_mod.py"}, "cwd": str(wt)}))


def test_case_and_dot_slash_forms(proj):
    base = str(proj)
    assert_deny(run(proj, payload=pl(proj, base + "/.worktrees/./TASK-7/./main.py")))
    assert_deny(run(proj, payload=pl(proj, base + "/.WORKTREES/task-7/MAIN.PY")) ) if os.name == "nt" else None
    assert_allow(run(proj, payload=pl(proj, base + "/.worktrees/./TASK-7/./new_mod.py")))
    if os.name == "nt":
        assert_allow(run(proj, payload=pl(proj, base + "/.WORKTREES/task-7/NEW_MOD.PY")))


def test_notebook_and_dotdot_strict(proj):
    assert_deny(run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "n.ipynb", "NotebookEdit", "notebook_path")))
    assert_deny(run(proj, payload=pl(proj, str(proj / ".worktrees" / "TASK-7" / "sub" / ".." / "main.py"))))


def test_kill_switch_writes_no_log(proj):
    (proj / ".queue" / "guards.off").write_text("")
    assert_allow(run(proj, payload=pl(proj, proj / ".worktrees" / "TASK-7" / "main.py")))
    assert not (proj / ".queue" / "guard.log").exists()
