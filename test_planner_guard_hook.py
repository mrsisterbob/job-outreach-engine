"""planner_guard.py PreToolUse hook, driven as a subprocess with the real payload on stdin."""

import json
import os
import subprocess
import sys

import pytest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude", "hooks", "planner_guard.py")


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    (p / ".queue" / "inbox").mkdir(parents=True)
    return p


def transcript(tmp_path, titles, name="t.jsonl"):
    lines = [json.dumps({"type": "user", "message": "hi"})]
    for t in titles:
        lines.append(json.dumps({"type": "custom-title", "customTitle": t, "sessionId": "s"}))
    f = tmp_path / name
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def run(proj, payload=None, stdin=None):
    if stdin is None:
        stdin = json.dumps(payload)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(proj))
    return subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True, text=True, env=env)


def pl(proj, path, tp, tool="Write", key="file_path"):
    d = {"tool_name": tool, "tool_input": {key: str(path)}, "cwd": str(proj)}
    if tp is not None:
        d["transcript_path"] = str(tp)
    return d


def assert_denied(r):
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out


def assert_allowed(r):
    assert r.returncode == 0 and r.stdout == ""


def test_planner_inbox_allowed(proj, tmp_path):
    t = transcript(tmp_path, ["planner"])
    assert_allowed(run(proj, pl(proj, proj / ".queue" / "inbox" / "TASK-9-x.md", t)))


def test_planner_main_py_denied(proj, tmp_path):
    t = transcript(tmp_path, ["planner"])
    out = assert_denied(run(proj, pl(proj, proj / "main.py", t)))
    assert "main.py" in out["permissionDecisionReason"]
    assert "main.py" in (proj / ".queue" / "guard.log").read_text()


def test_planner_notebook_worktree_and_claude_denied(proj, tmp_path):
    t = transcript(tmp_path, ["planner"])
    assert_denied(run(proj, pl(proj, proj / "a.ipynb", t, tool="NotebookEdit", key="notebook_path")))
    assert_denied(run(proj, pl(proj, proj / ".worktrees" / "TASK-1" / "x.py", t)))
    assert_denied(run(proj, pl(proj, proj / ".claude" / "settings.json", t)))


def test_planner_outside_project_allowed(proj, tmp_path):
    t = transcript(tmp_path, ["planner"])
    assert_allowed(run(proj, pl(proj, tmp_path / "scratch" / "n.txt", t)))


def test_worker_allowed(proj, tmp_path):
    t = transcript(tmp_path, ["worker"])
    assert_allowed(run(proj, pl(proj, proj / "main.py", t)))


def test_no_title_allowed(proj, tmp_path):
    t = transcript(tmp_path, [])
    assert_allowed(run(proj, pl(proj, proj / "main.py", t)))


def test_last_title_wins(proj, tmp_path):
    t1 = transcript(tmp_path, ["planner", "worker"], "a.jsonl")
    assert_allowed(run(proj, pl(proj, proj / "main.py", t1)))
    t2 = transcript(tmp_path, ["worker", "planner"], "b.jsonl")
    assert_denied(run(proj, pl(proj, proj / "main.py", t2)))


def test_fail_open_cases(proj, tmp_path):
    for tp in (None, tmp_path / "missing.jsonl", tmp_path):
        assert_allowed(run(proj, pl(proj, proj / "main.py", tp)))
    for bad in ("not json", "[]", ""):
        assert_allowed(run(proj, stdin=bad))


def test_dotdot_escape_denied(proj, tmp_path):
    t = transcript(tmp_path, ["planner"])
    esc = str(proj / ".queue" / "inbox" / ".." / ".." / "main.py")
    assert_denied(run(proj, pl(proj, esc, t)))


def test_kill_switch_allows(proj, tmp_path):
    (proj / ".queue" / "guards.off").write_text("", encoding="utf-8")
    t = transcript(tmp_path, ["planner"])
    assert_allowed(run(proj, pl(proj, proj / "main.py", t)))


def run_env(env_extra, payload, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_extra)
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True,
                          text=True, env=env)


@pytest.mark.parametrize("mode", ["unset", "empty"])
def test_cwd_fallback_when_project_dir_missing(proj, tmp_path, mode):
    t = transcript(tmp_path, ["planner"])
    payload = pl(proj, proj / "main.py", t)
    if mode == "unset":
        r = run_env({}, payload, drop=("CLAUDE_PROJECT_DIR",))
    else:
        r = run_env({"CLAUDE_PROJECT_DIR": ""}, payload)
    assert_denied(r)
    ok = pl(proj, proj / ".queue" / "inbox" / "TASK-1-x.md", t)
    assert_allowed(run_env({"CLAUDE_PROJECT_DIR": ""}, ok))


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names are Windows only")
def test_short_name_path_denied(tmp_path):
    import ctypes
    p = tmp_path / "a_long_project_folder_name"
    (p / ".queue" / "inbox").mkdir(parents=True)
    (p / "main.py").write_text("x", encoding="utf-8")
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p / "main.py"), buf, 1024)
    if not n or buf.value.lower() == str(p / "main.py").lower():
        pytest.skip("8.3 names disabled on this volume")
    t = transcript(tmp_path, ["planner"])
    assert_denied(run(p, pl(p, buf.value, t)))


@pytest.mark.skipif(os.name != "nt", reason="device prefixes are Windows only")
def test_device_prefix_denied(proj, tmp_path):
    t = transcript(tmp_path, ["planner"])
    for pre in ("\\?\\", "\\.\\"):
        assert_denied(run(proj, pl(proj, pre + str(proj / "main.py"), t)))
    assert_allowed(run(proj, pl(proj, "\\?\\" + str(proj / ".queue" / "inbox" / "T.md"), t)))


def test_inbox_link_to_elsewhere_denied(tmp_path):
    p = tmp_path / "proj"
    (p / ".queue").mkdir(parents=True)
    (p / "src").mkdir()
    link = p / ".queue" / "inbox"
    try:
        os.symlink(str(p / "src"), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(p / "src")],
                           capture_output=True, text=True) if os.name == "nt" else None
        if r is None or r.returncode != 0:
            pytest.skip("cannot create symlink or junction here")
    t = transcript(tmp_path, ["planner"])
    assert_denied(run(p, pl(p, link / "main.py", t)))
