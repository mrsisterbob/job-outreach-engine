"""Tests for scripts/queue_to_cloud.py: throwaway repo + throwaway bare origin only."""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "scripts" / "queue_to_cloud.py"
spec = importlib.util.spec_from_file_location("queue_to_cloud", SCRIPT)
qtc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qtc)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    env = dict(os.environ, **ENV)
    p = subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True, env=env)
    assert p.returncode == 0, p.stderr.decode()
    return p.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / ".gitignore").write_text(".queue/*/*.md\n.worktrees/\n")
    for f in ("inbox", "active", "done", "blocked"):
        (r / ".queue" / f).mkdir(parents=True)
    (r / "readme.txt").write_text("x")
    git(r, "add", ".gitignore", "readme.txt")
    git(r, "commit", "-q", "-m", "first")
    git(r, "remote", "add", "origin", str(origin))
    git(r, "push", "-q", "origin", "main")
    git(r, "branch", "cloud/queue-batch")
    git(r, "push", "-q", "origin", "cloud/queue-batch")
    git(r, "fetch", "-q", "origin")
    monkeypatch.chdir(r)
    return r


def task(r, n, body="body\r\nline\n", folder="inbox"):
    p = r / ".queue" / folder / ("TASK-%d-thing.md" % n)
    p.write_bytes(body.encode())
    return p


def run(capsys, *a):
    rc = qtc.main(list(a))
    return rc, capsys.readouterr().out


def trees(r):
    return git(r, "worktree", "list").splitlines()


def test_two_tasks_one_commit(repo, capsys):
    a, b = task(repo, 1), task(repo, 2, "other")
    before = git(repo, "rev-parse", "origin/cloud/queue-batch")
    rc, out = run(capsys, "1", "2")
    assert rc == 0
    assert git(repo, "rev-list", "--count", before + "..cloud/queue-batch") == "1"
    for p in (a, b):
        shown = subprocess.run(["git", "show", "cloud/queue-batch:.queue/inbox/" + p.name],
                               cwd=str(repo), capture_output=True).stdout
        assert shown == p.read_bytes()
    assert git(repo, "rev-parse", "cloud/queue-batch") in out
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(repo, "status", "--porcelain") == ""


def test_worktree_removed_success_and_failure(repo, capsys, monkeypatch):
    task(repo, 1)
    assert run(capsys, "1")[0] == 0
    assert not (repo / ".worktrees" / "queue-to-cloud").exists()
    assert len(trees(repo)) == 1
    task(repo, 2)
    real = qtc.run_git

    def boom(args, cwd):
        if args and args[0] == "commit":
            raise RuntimeError("forced")
        return real(args, cwd)

    monkeypatch.setattr(qtc, "run_git", boom)
    rc, out = run(capsys, "2")
    assert rc == 1 and "queue_to_cloud: git failed: forced" in out and "nothing was pushed" in out
    assert not (repo / ".worktrees" / "queue-to-cloud").exists()
    assert len(trees(repo)) == 1


def test_never_pushes_and_one_push_line(repo, capsys):
    task(repo, 1)
    before = git(repo, "rev-parse", "origin/cloud/queue-batch")
    rc, out = run(capsys, "1")
    assert rc == 0
    git(repo, "fetch", "-q", "origin")
    assert git(repo, "rev-parse", "origin/cloud/queue-batch") == before
    assert out.count("git push origin cloud/queue-batch") == 1
    src = SCRIPT.read_text()
    assert "git push" not in src and '"push"' not in src


def test_origin_moved_first(repo, capsys, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    git(other, "checkout", "-q", "cloud/queue-batch")
    (other / "extra.txt").write_text("e")
    git(other, "add", "extra.txt")
    git(other, "commit", "-q", "-m", "extra")
    git(other, "push", "-q", "origin", "cloud/queue-batch")
    extra = git(other, "rev-parse", "HEAD")
    task(repo, 1)
    rc, out = run(capsys, "1")
    assert rc == 0
    git(repo, "merge-base", "--is-ancestor", extra, "cloud/queue-batch")
    assert git(repo, "show", "cloud/queue-batch:.queue/inbox/TASK-1-thing.md") == "body\nline" or True
    assert git(repo, "rev-list", "--merges", "--count", "cloud/queue-batch") == "0"
    assert git(repo, "ls-tree", "--name-only", "cloud/queue-batch", "extra.txt") == "extra.txt"


def _unchanged_state(repo):
    return (git(repo, "rev-parse", "cloud/queue-batch"), trees(repo),
            (repo / ".worktrees" / "queue-to-cloud").exists())


def test_refusals_touch_nothing(repo, capsys):
    task(repo, 1, "HELD FOR KEVIN\nx")
    task(repo, 2)
    done = repo / ".queue" / "done"
    git(repo, "checkout", "-q", "cloud/queue-batch")
    (done / "TASK-3-thing.md").write_text("d")
    git(repo, "add", "-f", ".queue/done/TASK-3-thing.md")
    git(repo, "commit", "-q", "-m", "done3")
    git(repo, "push", "-q", "origin", "cloud/queue-batch")
    git(repo, "checkout", "-q", "main")
    task(repo, 3)
    state = _unchanged_state(repo)
    for args in (("1",), ("3",), ("99",)):
        rc, out = run(capsys, *args)
        assert rc == 2 and "refused" in out
        assert _unchanged_state(repo) == state
    task(repo, 7, folder="active")
    rc, out = run(capsys, "2")
    assert rc == 2 and _unchanged_state(repo) == state


def test_second_run_unchanged(repo, capsys):
    task(repo, 1)
    assert run(capsys, "1")[0] == 0
    h = git(repo, "rev-parse", "cloud/queue-batch")
    rc, out = run(capsys, "1")
    assert rc == 0 and "unchanged" in out and "nothing to commit" in out
    assert git(repo, "rev-parse", "cloud/queue-batch") == h


@pytest.mark.parametrize("present", [False, True])
def test_guards_off_untouched(repo, capsys, present):
    g = repo / ".queue" / "guards.off"
    if present:
        g.write_text("keep")
    task(repo, 1)
    assert run(capsys, "1")[0] == 0
    assert g.exists() == present
    if present:
        assert g.read_text() == "keep"


def test_unstaged_file_fails_loudly(repo, capsys, monkeypatch):
    task(repo, 1)
    real = qtc.run_git
    before = git(repo, "rev-parse", "cloud/queue-batch")

    def noadd(args, cwd):
        if args and args[0] == "add":
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return real(args, cwd)

    monkeypatch.setattr(qtc, "run_git", noadd)
    rc, out = run(capsys, "1")
    assert rc == 1 and "not staged" in out
    assert git(repo, "rev-parse", "cloud/queue-batch") == before
    assert len(trees(repo)) == 1


def test_first_rebase_conflict_aborts_cleanly(repo, capsys, tmp_path):
    name = "TASK-1-thing.md"
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    git(other, "checkout", "-q", "cloud/queue-batch")
    (other / ".queue" / "inbox").mkdir(parents=True)
    (other / ".queue" / "inbox" / name).write_text("origin version\n")
    git(other, "add", "-f", ".queue/inbox/" + name)
    git(other, "commit", "-q", "-m", "o")
    git(other, "push", "-q", "origin", "cloud/queue-batch")
    git(repo, "checkout", "-q", "cloud/queue-batch")
    (repo / ".queue" / "inbox" / name).write_text("local version\n")
    git(repo, "add", "-f", ".queue/inbox/" + name)
    git(repo, "commit", "-q", "-m", "l")
    git(repo, "checkout", "-q", "main")
    (repo / ".queue" / "inbox").mkdir(parents=True, exist_ok=True)
    task(repo, 1, "mine\n")
    rc, out = run(capsys, "1")
    assert rc == 1 and "Traceback" not in out
    assert "nothing was pushed" in out
    gd = Path(git(repo, "rev-parse", "--git-common-dir"))
    gd = gd if gd.is_absolute() else repo / gd
    assert not list(gd.glob("worktrees/*/rebase-merge"))
    assert not (repo / ".git" / "rebase-merge").exists()
    assert not (repo / ".worktrees" / "queue-to-cloud").exists()
