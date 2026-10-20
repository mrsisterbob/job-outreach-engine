"""The Stop hook runs the suites under the repo's .venv interpreter when one exists."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import time

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


# --- TASK-106: an unchanged tree already proved green is not re-tested -------------------
#
# Real entry point (stdin JSON, cwd) against throwaway git repos in tmp_path. The pytest run
# is counted by a sentinel inside the tiny suites: each test appends one character to the
# file named by HOOK_COUNT_FILE, so "did the hook run the suites" is read off disk.

_TINY_SUITE = """\
import os


def test_counts():
    with open(os.environ["HOOK_COUNT_FILE"], "a") as handle:
        handle.write("x")
    if os.environ.get("HOOK_MUTATE"):  # the tree changes while pytest is running
        with open(os.environ["HOOK_MUTATE"], "w") as late:
            late.write("late")
    assert not os.environ.get("HOOK_RED")
"""

_TINY_INDEX_BUILDER = """\
with open("FUNCTION_INDEX.md", "a") as handle:
    handle.write("regenerated\\n")
"""


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          check=True).stdout


def _make_proof_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    (repo / "test_main_integration.py").write_text(_TINY_SUITE)
    (repo / "test_pipeline_utils.py").write_text(_TINY_SUITE)
    (repo / "build_function_index.py").write_text(_TINY_INDEX_BUILDER)
    (repo / "FUNCTION_INDEX.md").write_text("index\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.queue/\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _stop(tmp_path, repo, red=False, extra_payload=None, env_extra=None):
    """One Stop event through the hook's real entry point; returns (suite_runs, stdout)."""
    count_file = tmp_path / "count.txt"
    before = len(count_file.read_text()) if count_file.exists() else 0
    env = dict(os.environ, HOOK_COUNT_FILE=str(count_file), PYTHONDONTWRITEBYTECODE="1")
    env.pop("HOOK_RED", None)
    env.update(env_extra or {})
    if red:
        env["HOOK_RED"] = "1"
    payload = {"cwd": str(repo), **(extra_payload or {})}
    done = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), env=env,
                          capture_output=True, text=True, timeout=240)
    assert done.returncode == 0, done.stderr
    after = len(count_file.read_text()) if count_file.exists() else 0
    # Each of the two tiny suites appends one character per pytest run.
    return (after - before) // 2, done.stdout


def _proof_file(repo):
    git_dir = _git(repo, "rev-parse", "--absolute-git-dir").strip()
    return os.path.join(git_dir, _load_hook().PROOF_NAME)


def test_hook_skips_unchanged_green_tree_second_stop_does_not_rerun(tmp_path):
    repo = _make_proof_repo(tmp_path)
    runs, out = _stop(tmp_path, repo)
    assert (runs, out) == (1, "")
    assert os.path.isfile(_proof_file(repo))
    runs, out = _stop(tmp_path, repo)
    assert (runs, out) == (0, "")


def test_hook_skips_unchanged_green_tree_a_proof_is_not_a_tracked_or_untracked_change(
        tmp_path):
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_hook_skips_unchanged_green_tree_editing_a_tracked_file_reruns(tmp_path):
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    (repo / "test_pipeline_utils.py").write_text(_TINY_SUITE + "\n# edited\n")
    assert _stop(tmp_path, repo) == (1, "")
    assert _stop(tmp_path, repo) == (0, "")


def test_hook_skips_unchanged_green_tree_staging_a_change_reruns(tmp_path):
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    (repo / "test_pipeline_utils.py").write_text(_TINY_SUITE + "\n# edited\n")
    _stop(tmp_path, repo)
    _git(repo, "add", "test_pipeline_utils.py")  # same content, different staged state
    assert _stop(tmp_path, repo) == (1, "")


def test_hook_skips_unchanged_green_tree_new_untracked_test_file_reruns(tmp_path):
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    (repo / "test_new.py").write_text("def test_new():\n    assert True\n")
    assert _stop(tmp_path, repo)[0] == 1
    assert _stop(tmp_path, repo)[0] == 0


def test_hook_skips_unchanged_green_tree_ignored_files_do_not_count(tmp_path):
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    (repo / ".queue").mkdir()
    (repo / ".queue" / "note.md").write_text("queue chatter")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
    assert _stop(tmp_path, repo)[0] == 0


def test_hook_skips_unchanged_green_tree_deleting_a_tracked_file_reruns(tmp_path):
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    (repo / "build_function_index.py").unlink()
    assert _stop(tmp_path, repo)[0] == 1


def test_hook_skips_unchanged_green_tree_red_run_blocks_and_leaves_no_proof(tmp_path):
    repo = _make_proof_repo(tmp_path)
    runs, out = _stop(tmp_path, repo, red=True)
    assert runs == 1
    assert json.loads(out)["decision"] == "block"
    assert not os.path.exists(_proof_file(repo))
    runs, out = _stop(tmp_path, repo, red=True)  # still red, still blocked, still runs
    assert runs == 1 and json.loads(out)["decision"] == "block"
    assert _stop(tmp_path, repo) == (1, "")  # green now: runs, passes, proof earned


def test_hook_skips_unchanged_green_tree_red_run_drops_an_earlier_proof(tmp_path):
    """green -> edit to red -> revert: the tree equals the old proven one, but the red run in
    between must have removed that proof, so the suites run again rather than skip."""
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    original = (repo / "test_pipeline_utils.py").read_text()
    (repo / "test_pipeline_utils.py").write_text(original + "\ndef test_boom():\n"
                                                 "    assert False\n")
    runs, out = _stop(tmp_path, repo)
    assert json.loads(out)["decision"] == "block"
    assert not os.path.exists(_proof_file(repo))
    (repo / "test_pipeline_utils.py").write_text(original)
    assert _stop(tmp_path, repo) == (1, "")


def test_hook_skips_unchanged_green_tree_function_index_rewrite_does_not_rerun(tmp_path):
    """The hook's own refresh_function_index rewrites FUNCTION_INDEX.md after a green run;
    that must not make the very next Stop see a changed tree."""
    repo = _make_proof_repo(tmp_path)
    (repo / "test_pipeline_utils.py").write_text(_TINY_SUITE + "\n# touched\n")  # a .py diff
    assert _stop(tmp_path, repo) == (1, "")
    assert (repo / "FUNCTION_INDEX.md").read_text() == "index\nregenerated\n"  # it did rewrite
    assert _stop(tmp_path, repo) == (0, "")
    assert (repo / "FUNCTION_INDEX.md").read_text() == "index\nregenerated\n"  # not again


def test_hook_skips_unchanged_green_tree_a_proof_does_not_follow_a_copy(tmp_path):
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    same = tmp_path / "same_content_elsewhere"
    shutil.copytree(repo, same)  # carries .git, and the proof with it
    assert os.path.isfile(_proof_file(same))
    assert _stop(tmp_path, same)[0] == 1  # a different tree, even with identical content
    different = tmp_path / "different_content"
    shutil.copytree(repo, different)
    (different / "test_pipeline_utils.py").write_text(_TINY_SUITE + "\n# other\n")
    assert _stop(tmp_path, different)[0] == 1
    assert _stop(tmp_path, repo)[0] == 0  # the original is still proven


def test_hook_skips_unchanged_green_tree_garbled_proof_means_run(tmp_path):
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    proof = _proof_file(repo)
    good = open(proof, encoding="utf-8").read()
    fp = json.loads(good)["fingerprint"]
    now = time.time()
    for garbage in ("", "not json at all", good[:len(good) // 2], "[]", "null", '"x"',
                    '{"version": 2}', '{"version": 2, "fingerprint": 7, "created": %s}' % now,
                    # right fingerprint but the wrong shape: old version, no/odd/future stamp
                    '{"version": 1, "fingerprint": "%s", "created": %s}' % (fp, now),
                    '{"version": 2, "fingerprint": "%s"}' % fp,
                    '{"version": 2, "fingerprint": "%s", "created": "now"}' % fp,
                    '{"version": 2, "fingerprint": "%s", "created": true}' % fp,
                    '{"version": 2, "fingerprint": "%s", "created": %s}' % (fp, now + 3600)):
        with open(proof, "w", encoding="utf-8") as handle:
            handle.write(garbage)
        assert _stop(tmp_path, repo) == (1, ""), garbage  # runs, and rewrites a good proof
        assert _stop(tmp_path, repo) == (0, ""), garbage
    with open(proof, "wb") as handle:
        handle.write(b"\xff\xfe\x00 binary junk")
    assert _stop(tmp_path, repo)[0] == 1
    os.remove(proof)
    assert _stop(tmp_path, repo)[0] == 1


def test_hook_skips_unchanged_green_tree_stop_hook_active_still_returns_first(tmp_path):
    repo = _make_proof_repo(tmp_path)
    assert _stop(tmp_path, repo, extra_payload={"stop_hook_active": True}) == (0, "")
    assert not os.path.exists(_proof_file(repo))


def test_hook_skips_unchanged_green_tree_outside_a_git_tree_still_runs(tmp_path):
    """No fingerprint (nothing to list) must mean run, never skip."""
    hook = _load_hook()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert hook.tree_fingerprint(str(plain)) is None
    assert hook.proof_matches(str(plain), "anything") is False


def _set_proof_age(repo, seconds_old):
    proof = _proof_file(repo)
    data = json.load(open(proof, encoding="utf-8"))
    data["created"] = time.time() - seconds_old
    with open(proof, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def test_hook_skips_unchanged_green_tree_an_expired_proof_reruns(tmp_path):
    """The suites read the real clock, so an identical tree can turn red; proofs expire."""
    hook = _load_hook()
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    _set_proof_age(repo, hook.PROOF_TTL_SECONDS - 120)  # still fresh
    assert _stop(tmp_path, repo) == (0, "")
    _set_proof_age(repo, hook.PROOF_TTL_SECONDS + 60)  # expired
    assert _stop(tmp_path, repo) == (1, "")
    assert _stop(tmp_path, repo) == (0, "")  # and the rerun earned a fresh one


def test_hook_skips_unchanged_green_tree_ttl_is_thirty_minutes_on_an_injected_clock(
        tmp_path):
    hook = _load_hook()
    repo = _make_proof_repo(tmp_path)
    fingerprint = hook.tree_fingerprint(str(repo))
    assert hook.PROOF_TTL_SECONDS == 30 * 60
    hook.write_proof(str(repo), fingerprint, now=1000.0)
    assert hook.proof_matches(str(repo), fingerprint, now=1000.0 + 29 * 60) is True
    assert hook.proof_matches(str(repo), fingerprint, now=1000.0 + 31 * 60) is False
    assert hook.proof_matches(str(repo), fingerprint, now=999.0) is False  # future-dated


def test_hook_skips_unchanged_green_tree_a_tree_that_moved_during_the_run_earns_no_proof(
        tmp_path):
    repo = _make_proof_repo(tmp_path)
    late = repo / "late.txt"
    runs, out = _stop(tmp_path, repo, env_extra={"HOOK_MUTATE": str(late)})
    assert (runs, out) == (1, "")  # green, so no block ...
    assert late.exists()
    assert not os.path.exists(_proof_file(repo))  # ... but not trusted either
    assert _stop(tmp_path, repo) == (1, "")  # next Stop runs (and now earns one)
    assert _stop(tmp_path, repo) == (0, "")


def test_hook_skips_unchanged_green_tree_a_move_during_the_run_drops_an_older_proof(
        tmp_path):
    repo = _make_proof_repo(tmp_path)
    _stop(tmp_path, repo)
    assert os.path.isfile(_proof_file(repo))
    (repo / "test_pipeline_utils.py").write_text(_TINY_SUITE + "\n# edit\n")
    _stop(tmp_path, repo, env_extra={"HOOK_MUTATE": str(repo / "late.txt")})
    assert not os.path.exists(_proof_file(repo))


def test_hook_skips_unchanged_green_tree_failed_removal_poisons_the_proof(
        tmp_path, monkeypatch):
    """A red run whose proof cannot be deleted must still not leave a usable proof."""
    hook = _load_hook()
    repo = _make_proof_repo(tmp_path)
    fingerprint = hook.tree_fingerprint(str(repo))
    hook.write_proof(str(repo), fingerprint)
    assert hook.proof_matches(str(repo), fingerprint) is True

    def locked(path):
        raise PermissionError("locked")

    monkeypatch.setattr(hook.os, "remove", locked)
    hook.clear_proof(str(repo))
    monkeypatch.undo()
    assert os.path.exists(_proof_file(repo))  # removal really failed ...
    assert hook.proof_matches(str(repo), fingerprint) is False  # ... and it is poisoned
    assert _stop(tmp_path, repo) == (1, "")  # so the next Stop runs the suites
    assert _stop(tmp_path, repo) == (0, "")


def test_hook_skips_unchanged_green_tree_write_proof_leaves_no_tmp_file(tmp_path, monkeypatch):
    hook = _load_hook()
    repo = _make_proof_repo(tmp_path)
    fingerprint = hook.tree_fingerprint(str(repo))

    def broken_replace(src, dst):
        raise OSError("disk says no")

    monkeypatch.setattr(hook.os, "replace", broken_replace)
    hook.write_proof(str(repo), fingerprint)  # must not raise
    monkeypatch.undo()
    git_dir = os.path.dirname(_proof_file(repo))
    assert [n for n in os.listdir(git_dir) if n.endswith(".tmp")] == []
    assert not os.path.exists(_proof_file(repo))
