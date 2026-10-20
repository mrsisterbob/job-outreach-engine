"""The Stop hook runs the suites under the repo's .venv interpreter when one exists."""

import importlib.util
import io
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(REPO, ".claude", "hooks", "verify_suites.py")


YES = lambda candidate: True  # noqa: E731 - the fake interpreters are empty files


def _load_hook():
    spec = importlib.util.spec_from_file_location("verify_suites_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return str(path)


def _fake_hook_file(root):
    return str(root / ".claude" / "hooks" / "verify_suites.py")


def test_hook_prefers_venv_python_windows_layout(tmp_path):
    expected = _touch(tmp_path / "tree" / ".venv" / "Scripts" / "python.exe")
    got = _load_hook().suite_python(str(tmp_path / "tree"),
                                    hook_file=_fake_hook_file(tmp_path / "empty"),
                                    launches=YES)
    assert got == expected


def test_hook_prefers_venv_python_posix_layout(tmp_path):
    expected = _touch(tmp_path / "tree" / ".venv" / "bin" / "python")
    got = _load_hook().suite_python(str(tmp_path / "tree"),
                                    hook_file=_fake_hook_file(tmp_path / "empty"),
                                    launches=YES)
    assert got == expected


def test_hook_prefers_venv_python_falls_back_to_hook_repo(tmp_path):
    """A .worktrees/TASK-n tree has no .venv; the repo the hook lives in does."""
    tree = tmp_path / "repo" / ".worktrees" / "TASK-1"
    tree.mkdir(parents=True)
    expected = _touch(tmp_path / "repo" / ".venv" / "Scripts" / "python.exe")
    got = _load_hook().suite_python(str(tree),
                                    hook_file=_fake_hook_file(tmp_path / "repo"),
                                    launches=YES)
    assert got == expected


def test_hook_prefers_venv_python_tree_wins_over_hook_repo(tmp_path):
    expected = _touch(tmp_path / "tree" / ".venv" / "bin" / "python")
    _touch(tmp_path / "repo" / ".venv" / "Scripts" / "python.exe")
    got = _load_hook().suite_python(str(tmp_path / "tree"),
                                    hook_file=_fake_hook_file(tmp_path / "repo"),
                                    launches=YES)
    assert got == expected


def test_hook_prefers_venv_python_none_found_uses_sys_executable(tmp_path):
    (tmp_path / "tree").mkdir()
    got = _load_hook().suite_python(str(tmp_path / "tree"),
                                    hook_file=_fake_hook_file(tmp_path / "empty"),
                                    launches=YES)
    assert got == sys.executable


def test_hook_prefers_venv_python_real_repo():
    if not (os.path.isfile(os.path.join(REPO, ".venv", "Scripts", "python.exe"))
            or os.path.isfile(os.path.join(REPO, ".venv", "bin", "python"))):
        pytest.skip("no .venv in this tree (worktrees carry none)")
    got = _load_hook().suite_python(REPO)
    assert got.endswith((os.path.join(".venv", "Scripts", "python.exe"),
                         os.path.join(".venv", "bin", "python")))


def test_hook_prefers_venv_python_skips_a_candidate_that_will_not_launch(tmp_path):
    """An existing but unusable .venv python must fall back, never block every session."""
    _touch(tmp_path / "tree" / ".venv" / "Scripts" / "python.exe")
    got = _load_hook().suite_python(str(tmp_path / "tree"),
                                    hook_file=_fake_hook_file(tmp_path / "empty"),
                                    launches=lambda candidate: False)
    assert got == sys.executable


def test_hook_prefers_venv_python_broken_tree_venv_uses_working_repo_venv(tmp_path):
    broken = _touch(tmp_path / "tree" / ".venv" / "bin" / "python")
    good = _touch(tmp_path / "repo" / ".venv" / "bin" / "python")
    got = _load_hook().suite_python(str(tmp_path / "tree"),
                                    hook_file=_fake_hook_file(tmp_path / "repo"),
                                    launches=lambda candidate: candidate == good)
    assert got == good and got != broken


def test_hook_prefers_venv_python_probe_rejects_an_unlaunchable_file(tmp_path):
    """The real probe, no fakes: an empty file cannot be executed as an interpreter."""
    fake = _touch(tmp_path / "python.exe")
    assert _load_hook().venv_python_launches(fake) is False
    assert _load_hook().venv_python_launches(str(tmp_path / "missing.exe")) is False


def test_hook_prefers_venv_python_probe_accepts_a_working_interpreter():
    pytest.importorskip("pytest")
    assert _load_hook().venv_python_launches(sys.executable) is True


def _drive_main(monkeypatch, capsys, tree, pytest_returncode):
    """Run the hook's main() with subprocess.run faked; return (calls, stdout)."""
    hook = _load_hook()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        out = ""
        rc = 0
        if argv[0] == "git" and argv[1] == "rev-parse":
            out = str(tree)
        elif argv[0] == "git" and argv[1] == "diff":
            out = "main.py\n"
        elif "pytest" in argv and "-m" in argv:
            rc, out = pytest_returncode, "1 failed" if pytest_returncode else "ok"
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tree)})))
    hook.main()
    return calls, capsys.readouterr().out


def _make_tree(tmp_path):
    tree = tmp_path / "tree"
    for name in ("test_main_integration.py", "test_pipeline_utils.py"):
        _touch(tree / name)
    return tree, _touch(tree / ".venv" / "Scripts" / "python.exe")


def test_hook_prefers_venv_python_wiring_green_path(tmp_path, monkeypatch, capsys):
    """main() hands the venv interpreter to both the pytest run and the index rebuild."""
    tree, venv = _make_tree(tmp_path)
    calls, out = _drive_main(monkeypatch, capsys, tree, pytest_returncode=0)
    pytest_calls = [c for c in calls if "pytest" in c and "-m" in c]
    index_calls = [c for c in calls if "build_function_index.py" in c]
    assert len(pytest_calls) == 1 and pytest_calls[0][0] == venv
    assert len(index_calls) == 1 and index_calls[0][0] == venv
    assert out == ""


def test_hook_prefers_venv_python_wiring_red_path_names_the_interpreter(
        tmp_path, monkeypatch, capsys):
    tree, venv = _make_tree(tmp_path)
    calls, out = _drive_main(monkeypatch, capsys, tree, pytest_returncode=1)
    assert [c for c in calls if "pytest" in c and "-m" in c][0][0] == venv
    reason = json.loads(out)["reason"]
    assert json.loads(out)["decision"] == "block"
    assert f"`{venv} -m pytest test_main_integration.py" in reason


def test_hook_prefers_venv_python_wiring_unlaunchable_venv_falls_back(
        tmp_path, monkeypatch, capsys):
    """The probe fails (pytest missing in the venv): the run uses sys.executable."""
    tree, venv = _make_tree(tmp_path)
    hook = _load_hook()
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv[0])
        if argv[0] == venv:  # the launch probe: pytest is not importable in this venv
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no pytest")
        return subprocess.CompletedProcess(argv, 0, stdout=str(tree), stderr="")

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tree)})))
    hook.main()
    assert sys.executable in seen
    assert seen.count(venv) == 1  # the launch probe only; never used to run pytest
