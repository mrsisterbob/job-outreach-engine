"""queue_baseline: the pre-merge baseline is skipped only when main is the proven-green tree."""
import json
import os
import re
import subprocess
import sys

import queue_baseline as qb

ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP_LINE = r"main == proven-green tree \([0-9a-f]{7}\), proof recorded \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def commit(repo, files, message):
    for name, text in files.items():
        with open(os.path.join(repo, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)


def make_repo(base):
    base.mkdir(exist_ok=True)
    repo = str(base / "repo")
    os.mkdir(repo)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, {"main.py": "x = 1\n", "FUNCTION_INDEX.md": "v1\n"}, "initial")
    return repo, str(base / "marker.json")  # marker outside the repo: never dirties it


def green(repo, marker, **kw):
    """The worker's sequence: capture the digest, run the suites (rc), then record."""
    kw.setdefault("exit_code", 0)
    kw.setdefault("tree", qb.tree_id(repo))
    return qb.record_green(repo, marker, **kw)


def test_baseline_skips_after_index_commit(tmp_path):
    repo, marker = make_repo(tmp_path)

    # (1) no marker -> needed
    needed, reason = qb.baseline_needed(repo, marker)
    assert needed and "no proof" in reason

    # (2) proof recorded, then a commit changing only FUNCTION_INDEX.md -> NOT needed
    assert green(repo, marker)
    needed, _ = qb.baseline_needed(repo, marker)
    assert not needed  # same HEAD
    commit(repo, {"FUNCTION_INDEX.md": "v2 regenerated\n"}, "Regenerate FUNCTION_INDEX.md")
    needed, reason = qb.baseline_needed(repo, marker)
    assert not needed
    assert re.fullmatch(SKIP_LINE, reason)

    # (3) a commit changing any other tracked file -> needed
    commit(repo, {"main.py": "x = 2\n"}, "other task")
    needed, reason = qb.baseline_needed(repo, marker)
    assert needed and "differs" in reason

    # (4) a revert restoring the proved tree -> NOT needed. The tree is byte-identical to the
    # one both suites passed on, and the suites have no memory of commit ids, so re-running
    # them would repeat a proven result. (A revert that restores anything else stays needed.)
    git(repo, "revert", "--no-edit", "HEAD")
    needed, _ = qb.baseline_needed(repo, marker)
    assert not needed
    commit(repo, {"main.py": "x = 3\n"}, "another change")
    git(repo, "revert", "--no-edit", "--no-commit", "HEAD")
    commit(repo, {"extra.py": "y = 1\n"}, "revert plus extra")
    needed, _ = qb.baseline_needed(repo, marker)
    assert needed

    # (5) dirty tree or non-main branch -> needed even with a matching tree
    repo2, marker2 = make_repo(tmp_path / "second")
    assert green(repo2, marker2)
    assert qb.baseline_needed(repo2, marker2)[0] is False
    with open(os.path.join(repo2, "scratch.txt"), "w") as handle:
        handle.write("untracked\n")
    needed, reason = qb.baseline_needed(repo2, marker2)
    assert needed and "not clean" in reason
    os.remove(os.path.join(repo2, "scratch.txt"))
    with open(os.path.join(repo2, "main.py"), "a") as handle:
        handle.write("# edit\n")
    assert qb.baseline_needed(repo2, marker2)[0]
    git(repo2, "restore", "main.py")
    assert qb.baseline_needed(repo2, marker2)[0] is False
    git(repo2, "checkout", "-b", "task/x")
    needed, reason = qb.baseline_needed(repo2, marker2)
    assert needed and "not main" in reason

    # (6) garbled marker -> needed
    git(repo2, "checkout", "main")
    for garbage in ("", "not json{", "[]", json.dumps({"version": 1}),
                    json.dumps({"version": 1, "tree": "abc", "exit": 1, "created": 1}),
                    json.dumps({"version": 1, "tree": None, "exit": 0, "created": 1})):
        with open(marker2, "w", encoding="utf-8") as handle:
            handle.write(garbage)
        assert qb.baseline_needed(repo2, marker2)[0], garbage


def test_baseline_record_refuses_unless_green_and_unchanged(tmp_path):
    repo, marker = make_repo(tmp_path)
    captured = qb.tree_id(repo)
    # a non-zero rc (red run, or a revert re-run's rc) writes nothing
    assert not qb.record_green(repo, marker, exit_code=1, tree=captured)
    assert not qb.record_green(repo, marker, exit_code=None, tree=captured)
    assert not qb.record_green(repo, marker, exit_code=0, tree=None)
    assert not os.path.exists(marker)
    # a commit landing between the run and the record: the digest no longer matches
    commit(repo, {"main.py": "x = 2\n"}, "landed after the run")
    assert not qb.record_green(repo, marker, exit_code=0, tree=captured)
    assert not os.path.exists(marker)
    # happy path: same digest, rc 0
    assert qb.record_green(repo, marker, exit_code=0, tree=qb.tree_id(repo))
    assert os.path.isfile(marker)
    # a refusal never touches an existing proof
    with open(marker, encoding="utf-8") as handle:
        before = handle.read()
    assert not qb.record_green(repo, marker, exit_code=1, tree=qb.tree_id(repo))
    with open(marker, encoding="utf-8") as handle:
        assert handle.read() == before
    # a dirty folder cannot be proven
    with open(os.path.join(repo, "main.py"), "a") as handle:
        handle.write("# edit\n")
    assert not qb.record_green(repo, str(tmp_path / "m2.json"), exit_code=0, tree=qb.tree_id(repo))
    assert not (tmp_path / "m2.json").exists()


def test_baseline_proof_edge_cases(tmp_path):
    repo, marker = make_repo(tmp_path)

    def rewrite(data):
        with open(marker, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    # expired and future-dated proofs are not trusted
    assert green(repo, marker, now=1000.0)
    assert qb.baseline_needed(repo, marker, now=1000.0 + qb.PROOF_TTL_SECONDS + 1)[0]
    assert qb.baseline_needed(repo, marker, now=500.0)[0]
    assert not qb.baseline_needed(repo, marker, now=1000.0 + 60)[0]
    # a proof recorded under another Python (or none recorded at all) is not trusted
    with open(marker, encoding="utf-8") as handle:
        proof = json.load(handle)
    for key, value in (("python", "3.0.0 other"), ("executable", "/elsewhere/python")):
        rewrite(dict(proof, **{key: value}))
        needed, reason = qb.baseline_needed(repo, marker, now=1000.0 + 60)
        assert needed and "different Python" in reason
    del proof["python"]
    rewrite(proof)
    assert qb.baseline_needed(repo, marker, now=1000.0 + 60)[0]
    # not a git repo -> needed, never raises
    plain = tmp_path / "plain"
    plain.mkdir()
    assert qb.baseline_needed(str(plain), str(tmp_path / "none.json"))[0]


def out(repo, *args, data=None):
    return subprocess.run(["git", *args], cwd=repo, input=data, capture_output=True,
                          check=True).stdout


def commit_entry(repo, name, blob):
    """Commit HEAD's tree plus one top-level entry, built with mktree: Windows refuses to
    create a file name containing a tab, and update-index refuses it too."""
    entries = out(repo, "ls-tree", "-z", "HEAD")
    entries = b"".join(e + b"\0" for e in entries.split(b"\0")
                       if e and e.partition(b"\t")[2] != name.encode())
    entries += b"100644 blob " + blob + b"\t" + name.encode() + b"\0"
    tree = out(repo, "mktree", "-z", data=entries).strip().decode()
    new = out(repo, "commit-tree", tree, "-p", "HEAD", "-m", "tab path").strip().decode()
    git(repo, "update-ref", "HEAD", new)


def test_baseline_tree_id_tab_in_path(tmp_path):
    """Only the top-level FUNCTION_INDEX.md is ignored; a path containing a tab is not split."""
    repo, _ = make_repo(tmp_path)
    before = qb.tree_id(repo)
    blob = out(repo, "hash-object", "-w", "--stdin", data=b"doc\n").strip()
    for name in ("docs\tFUNCTION_INDEX.md", "FUNCTION_INDEX.md\tx"):
        commit_entry(repo, name, blob)
        after = qb.tree_id(repo)
        assert after != before, name
        before = after
    # while the real FUNCTION_INDEX.md still does not count
    commit_entry(repo, "FUNCTION_INDEX.md", blob)
    assert qb.tree_id(repo) == before


def test_baseline_marker_is_invisible_to_git(tmp_path):
    repo, _ = make_repo(tmp_path)
    marker = qb.default_marker_path(repo)
    assert marker and os.path.abspath(marker).startswith(os.path.abspath(os.path.join(repo, ".git")))
    assert qb.record_green(repo, exit_code=0, tree=qb.tree_id(repo))
    assert os.path.isfile(marker)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert status.stdout.strip() == ""
    assert not qb.baseline_needed(repo)[0]


def test_baseline_cli(tmp_path):
    repo, _ = make_repo(tmp_path)
    script = os.path.join(ROOT, "queue_baseline.py")

    def run(*args):
        return subprocess.run([sys.executable, script, *args], cwd=repo, capture_output=True, text=True)

    assert run("check").returncode == 1
    digest = run("tree")
    assert digest.returncode == 0 and re.fullmatch(r"[0-9a-f]{64}\s*", digest.stdout)
    tree = digest.stdout.strip()
    # refusals: red rc, wrong tree, missing flags
    for args in (("--exit", "1", "--tree", tree), ("--exit", "0", "--tree", "0" * 64),
                 ("--exit", "0"), ("--tree", tree), ("--exit", "x", "--tree", tree)):
        result = run("record", *args)
        assert result.returncode == 1 and "not recorded" in result.stdout, args
    assert run("check").returncode == 1
    ok = run("record", "--exit", "0", "--tree", tree)
    assert ok.returncode == 0 and "recorded" in ok.stdout
    result = run("check")
    assert result.returncode == 0
    assert re.fullmatch("baseline skipped: " + SKIP_LINE + r"\s*", result.stdout)
    assert run("bogus").returncode == 2


def test_baseline_queue_run_mentions_module():
    with open(os.path.join(ROOT, ".claude", "commands", "queue-run.md"), encoding="utf-8") as handle:
        text = handle.read()
    step3 = text[text.index("3. **Take the merge lock"):text.index("4. **Merge, always")]
    step6 = text[text.index("6. **Re-run both suites"):text.index("7. **Green on `main`")]
    assert "queue_baseline.py check" in step3
    assert "baseline skipped: main == proven-green tree" in step3
    assert "queue_baseline.py tree" in step6
    assert "queue_baseline.py record --exit" in step6 and "--tree" in step6
    assert "revert" in step6[step6.index("queue_baseline.py record"):]
