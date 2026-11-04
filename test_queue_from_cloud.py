"""Tests for scripts/queue_from_cloud.py: throwaway repo + throwaway bare origin only."""
import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "scripts" / "queue_from_cloud.py"
spec = importlib.util.spec_from_file_location("queue_from_cloud", SCRIPT)
qfc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qfc)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}

T205 = "TASK-205-merged-done.md"
T210 = "TASK-210-unmerged-done.md"
T206 = "TASK-206-blocked-one.md"
T209 = "TASK-209-still-inbox.md"
BLOCKED_BYTES = (b"---\r\nid: TASK-206\r\n---\r\n## Log\r\n- BLOCKED by worker: needs the key \xe2\x9c\x85\r\n  and more\r\n")


def git(cwd, *args):
    env = dict(os.environ, **ENV)
    p = subprocess.run(["git", "-c", "core.autocrlf=false"] + list(args), cwd=str(cwd), capture_output=True, env=env)
    assert p.returncode == 0, p.stderr.decode()
    return p.stdout.decode().strip()


def add_branch_file(r, folder, name, data):
    d = r / ".queue" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    git(r, "add", "-f", "--", ".queue/%s/%s" % (folder, name))


def merge_task(r, n, tag):
    git(r, "checkout", "-q", "-b", "side%d" % n, "cloud/queue-batch")
    (r / ("side%d.txt" % n)).write_text(tag)
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "work %d" % n)
    git(r, "checkout", "-q", "cloud/queue-batch")
    git(r, "merge", "-q", "--no-ff", "-m", "Merge TASK-%d: %s" % (n, tag), "side%d" % n)
    return git(r, "rev-parse", "HEAD")


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
    (r / "readme.txt").write_text("x")
    git(r, "add", ".gitignore", "readme.txt")
    git(r, "commit", "-q", "-m", "first")
    git(r, "remote", "add", "origin", str(origin))
    git(r, "checkout", "-q", "-b", "cloud/queue-batch")
    sha205 = merge_task(r, 205, "first thing")
    add_branch_file(r, "done", T205, b"205 done\r\n## Log\r\n- ok\r\n")
    merge_task(r, 210, "second thing")
    add_branch_file(r, "done", T210, b"210 done\n")
    add_branch_file(r, "blocked", T206, BLOCKED_BYTES)
    add_branch_file(r, "inbox", T209, b"209 inbox\n")
    git(r, "commit", "-q", "-m", "files")
    git(r, "push", "-q", "origin", "cloud/queue-batch")
    git(r, "branch", "-f", "main", sha205)
    git(r, "checkout", "-q", "main")
    git(r, "branch", "-q", "-D", "cloud/queue-batch")
    for name in (T205, T210, T206, T209):
        d = r / ".queue" / "inbox"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(b"local stale copy\n")
    for f in ("active", "done", "blocked"):
        (r / ".queue" / f).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(r)
    return r


def snapshot(r):
    return (git(r, "rev-parse", "HEAD"), git(r, "rev-parse", "--abbrev-ref", "HEAD"),
            git(r, "status", "--porcelain"))


def names(r, f):
    return sorted(p.name for p in (r / ".queue" / f).glob("*.md"))


def test_blocked_lands_byte_identical(repo, capsys):
    assert qfc.main([]) == 0
    assert (repo / ".queue" / "blocked" / T206).read_bytes() == BLOCKED_BYTES
    assert T206 not in names(repo, "inbox")
    out = capsys.readouterr().out
    assert "TASK-206 blocked: needs the key" in out


def test_merged_done_moves(repo):
    assert qfc.main([]) == 0
    assert (repo / ".queue" / "done" / T205).read_bytes() == b"205 done\r\n## Log\r\n- ok\r\n"
    assert T205 not in names(repo, "inbox")


def test_unmerged_done_not_moved(repo, capsys):
    assert qfc.main([]) == 0
    out = capsys.readouterr().out
    assert T210 in names(repo, "inbox") and T210 not in names(repo, "done")
    assert "TASK-210 done (not merged into main yet)" in out
    assert "merge origin/cloud/queue-batch into main by hand (--no-ff)" in out


def test_active_left_alone(repo, capsys):
    (repo / ".queue" / "active" / T206).write_bytes(b"in flight\n")
    assert qfc.main([]) == 0
    assert (repo / ".queue" / "active" / T206).read_bytes() == b"in flight\n"
    assert T206 not in names(repo, "blocked")
    assert T206 in names(repo, "inbox")
    assert "in flight locally" in capsys.readouterr().out


def test_conflicting_local_file_left_alone(repo, capsys):
    (repo / ".queue" / "blocked" / T205).write_bytes(b"kevin put this here\n")
    assert qfc.main([]) == 0
    assert (repo / ".queue" / "blocked" / T205).read_bytes() == b"kevin put this here\n"
    assert T205 not in names(repo, "done")
    assert "conflict" in capsys.readouterr().out


def test_same_name_different_content_not_overwritten(repo, capsys):
    (repo / ".queue" / "blocked" / T206).write_bytes(b"different\n")
    assert qfc.main([]) == 0
    assert (repo / ".queue" / "blocked" / T206).read_bytes() == b"different\n"
    assert T206 in names(repo, "inbox")
    assert "conflict" in capsys.readouterr().out


def test_second_run_noop_and_git_untouched(repo, capsys):
    before = snapshot(repo)
    assert qfc.main([]) == 0
    capsys.readouterr()
    after_first = {f: names(repo, f) for f in ("inbox", "done", "blocked")}
    assert qfc.main([]) == 0
    out = capsys.readouterr().out
    assert "already local" in out
    assert {f: names(repo, f) for f in ("inbox", "done", "blocked")} == after_first
    assert snapshot(repo) == before


def test_branch_inbox_task_no_action(repo, capsys):
    assert qfc.main([]) == 0
    assert (repo / ".queue" / "inbox" / T209).read_bytes() == b"local stale copy\n"
    assert "TASK-209" not in capsys.readouterr().out


def test_dry_run_writes_nothing(repo, capsys):
    before = {f: names(repo, f) for f in ("inbox", "active", "done", "blocked")}
    assert qfc.main(["--dry-run"]) == 0
    assert {f: names(repo, f) for f in before} == before
    assert "TASK-206 blocked" in capsys.readouterr().out


def test_missing_remote_returns_1_writes_nothing(repo, capsys):
    git(repo, "remote", "set-url", "origin", str(repo.parent / "nope.git"))
    before = {f: names(repo, f) for f in ("inbox", "active", "done", "blocked")}
    assert qfc.main([]) == 1
    assert {f: names(repo, f) for f in before} == before
    assert "fetch" in capsys.readouterr().out


def test_source_has_no_forbidden_git_subcommands():
    src = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"""["'](merge|reset|push|checkout|rebase|stash|worktree)["']""", src)


def test_conflict_not_counted_as_moved(repo, capsys):
    (repo / ".queue" / "blocked" / T206).write_bytes(b"different\n")
    (repo / ".queue" / "done" / T205).write_bytes(b"different too\n")
    assert qfc.main([]) == 0
    out = capsys.readouterr().out
    assert "conflict" in out
    assert "0 task(s) moved" in out


def test_git_error_on_merge_check_is_not_read_as_unmerged(repo, capsys):
    git(repo, "branch", "-m", "main", "trunk")
    before = names(repo, "inbox")
    assert qfc.main([]) == 1
    out = capsys.readouterr().out
    assert "could not tell whether TASK-" in out and "is merged into main" in out
    assert "not merged into main yet" not in out
    assert names(repo, "inbox") == before and names(repo, "done") == []
