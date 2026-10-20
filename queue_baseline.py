"""Decide whether the queue worker's pre-merge baseline run on `main` can be skipped.

Finish step 6 of the queue worker re-runs both suites on `main` after every merge, and step 3
of the NEXT task runs the same suites on the same code before merging. When `main` has not
moved in between, that second run proves nothing new. The only thing that changes `main`
after a green post-merge run is the FUNCTION_INDEX.md regeneration commit, which no test
reads, so comparing commit ids never fires. This module compares tree CONTENT instead.

Pure logic over `git`, no network. Anything unreadable, missing, stale or odd means "run the
baseline": the skip is an optimisation and every doubt resolves to the slow, safe path.

Known limits - what a tree digest cannot see (the skip trusts git's view of the tracked tree):
  * untracked or gitignored inputs the suites read: .env, custom_resumes/, and the sibling
    ../montelattice-site checkout that test_main_integration.py reads (line ~11919);
  * installed packages (only the Python version and interpreter path are compared, below);
  * files marked assume-unchanged / skip-worktree, which `git status` hides;
  * line-ending (autocrlf) byte differences between the blob and the checked-out file.
The PROOF_TTL_SECONDS expiry bounds how long any of these can go unnoticed, and any doubt
still means "run the baseline".

CLI (used by .claude/commands/queue-run.md, run from the main folder):
    python queue_baseline.py tree                          print the tracked-tree digest
    python queue_baseline.py check                         exit 0 = skip, 1 = run baseline
    python queue_baseline.py record --exit RC --tree T     record the proof; exit 1 = refused
`record` enforces green in code: T is the digest captured by `tree` BEFORE the post-merge suite
run, RC that run's exit code. It writes only when RC == 0, the digest still equals T, the
folder is clean and the branch is main.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

IGNORED_PATH = "FUNCTION_INDEX.md"   # regenerated after every merge; no test reads it
MARKER_NAME = "queue-baseline-proof.json"
MARKER_VERSION = 1
MAIN_BRANCH = "main"
# The tree is git's view only: an upgraded package or Python can change what the suites do
# without changing it, so a proof is only trusted for a bounded time.
PROOF_TTL_SECONDS = 6 * 60 * 60


def _git(repo, *args):
    """stdout bytes of a git command, or None when it failed (any failure means "cannot prove")."""
    try:
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=60)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout or b""


def tree_id(repo):
    """Hash of HEAD's tracked tree (paths, modes, blob ids) minus FUNCTION_INDEX.md, or None.

    Built from `git ls-tree -r -z HEAD`, so it is the content of the tree and not the commit
    id: a revert that restores an earlier tree hashes the same as that tree did.
    """
    out = _git(repo, "ls-tree", "-r", "-z", "HEAD")
    if not out:
        return None
    digest = hashlib.sha256()
    for entry in out.split(b"\0"):
        if not entry:
            continue
        # "<mode> <type> <blob>\t<path>": the path starts after the FIRST tab (the metadata
        # has none) and may itself contain tabs, so compare the raw bytes of the whole path.
        if entry.partition(b"\t")[2] == IGNORED_PATH.encode():
            continue
        digest.update(entry + b"\0")
    return digest.hexdigest()


def _python_identity():
    """(version, interpreter path): the suites also depend on which Python and venv ran them."""
    return sys.version, os.path.normcase(os.path.abspath(sys.executable))


def default_marker_path(repo):
    """The marker lives in the git dir: git never lists it, so it cannot dirty the folder.

    (`.queue/` is NOT a safe home: only `.queue/*/*.md` is gitignored, so any other file
    there shows up in `git status --porcelain` and trips the "main folder not clean" guard.)
    """
    out = _git(repo, "rev-parse", "--absolute-git-dir")
    if not out:
        return None
    git_dir = out.decode("utf-8", "replace").strip()
    return os.path.join(git_dir, MARKER_NAME) if git_dir else None


def _folder_state(repo):
    """(ok, reason): the working tree is clean and the branch is main."""
    status = _git(repo, "status", "--porcelain")
    if status is None:
        return False, "cannot read git status"
    if status.strip():
        return False, "main folder is not clean"
    branch = _git(repo, "symbolic-ref", "--short", "HEAD")
    if branch is None or branch.decode("utf-8", "replace").strip() != MAIN_BRANCH:
        return False, "branch is not main"
    return True, ""


def record_green(repo, marker_path=None, exit_code=None, tree=None, now=None):
    """Record that `main`'s tree passed both suites. True only when a marker was written.

    `tree` is the digest captured BEFORE the post-merge run and `exit_code` that run's exit
    code. Refuses, writing nothing, unless exit_code == 0, the current digest equals `tree`
    (nothing landed between the run and now), the folder is clean and the branch is main.
    Never raises.
    """
    try:
        marker_path = marker_path or default_marker_path(repo)
        if not marker_path or not isinstance(tree, str) or not tree:
            return False
        if isinstance(exit_code, bool) or exit_code != 0:
            return False
        if not _folder_state(repo)[0] or tree_id(repo) != tree:
            return False
        version, executable = _python_identity()
        payload = {"version": MARKER_VERSION, "tree": tree, "exit": 0,
                   "created": time.time() if now is None else now,
                   "python": version, "executable": executable}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(marker_path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, marker_path)
        except Exception:
            _remove(tmp)
            raise
        return True
    except Exception:
        return False


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def baseline_needed(repo, marker_path=None, now=None):
    """(needed, reason). False only when EVERY skip condition holds; any doubt is True."""
    try:
        marker_path = marker_path or default_marker_path(repo)
        if not marker_path or not os.path.isfile(marker_path):
            return True, "no proof marker"
        try:
            with open(marker_path, encoding="utf-8") as handle:
                proof = json.load(handle)
        except Exception:
            return True, "proof marker unreadable"
        if not isinstance(proof, dict) or proof.get("version") != MARKER_VERSION:
            return True, "proof marker malformed"
        recorded, created = proof.get("tree"), proof.get("created")
        exit_code = proof.get("exit")
        if (not isinstance(recorded, str) or not recorded
                or isinstance(exit_code, bool) or exit_code != 0
                or isinstance(created, bool) or not isinstance(created, (int, float))):
            return True, "proof marker malformed"
        if (proof.get("python"), proof.get("executable")) != _python_identity():
            return True, "proof was recorded under a different Python"
        age = (time.time() if now is None else now) - created
        if not 0 <= age <= PROOF_TTL_SECONDS:
            return True, "proof marker expired"
        ok, why = _folder_state(repo)
        if not ok:
            return True, why
        current = tree_id(repo)
        if current is None:
            return True, "cannot read main's tree"
        if current != recorded:
            return True, "main's tree differs from the proven-green tree"
        stamp = datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return False, f"main == proven-green tree ({current[:7]}), proof recorded {stamp}"
    except Exception:
        return True, "could not decide"


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    repo = os.getcwd()
    if command == "tree":
        digest = tree_id(repo)
        if digest is None:
            print("cannot read main's tree", file=sys.stderr)
            return 1
        print(digest)
        return 0
    if command == "check":
        needed, reason = baseline_needed(repo)
        if needed:
            print(f"baseline needed: {reason}")
            return 1
        print(f"baseline skipped: {reason}")
        return 0
    if command == "record":
        args, exit_code, tree = argv[2:], None, None
        try:
            if "--exit" in args:
                exit_code = int(args[args.index("--exit") + 1])
            if "--tree" in args:
                tree = args[args.index("--tree") + 1]
        except (IndexError, ValueError):
            pass
        if record_green(repo, exit_code=exit_code, tree=tree):
            print("baseline proof recorded")
            return 0
        print("baseline proof not recorded")
        return 1
    print("usage: python queue_baseline.py tree|check|record --exit RC --tree T", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
