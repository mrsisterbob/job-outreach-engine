"""Stop hook: refuse to end a turn that leaves the test suites red.

Every rule in .claude/commands/queue-run.md is advisory - prose a worker chooses to follow.
This one is not. The turn does not end until pytest is green, whatever the agent believes
about its own work.

It exists because of how this repo actually fails: a change passes its own spec while the
next real run reads back something different. A worker that skips the suite once, on a
Tuesday, leaves that broken for a week. Reading every diff catches it today; it will not
at 40k lines.

Contract (docs: code.claude.com/docs/en/hooks):
  stdin  - JSON with stop_hook_active, cwd, last_assistant_message, ...
  stdout - {"decision": "block", "reason": "..."} to keep the turn open, or nothing
  exit 0 either way; a non-zero exit is a broken hook, not a failed check

stop_hook_active is the loop guard. When it is true the previous block is already being
worked on, so blocking again would spin. Claude Code also overrides a Stop hook after 8
consecutive blocks, but we never want to reach that.

Written in Python, not the jq shell script the docs show: this machine has no jq, and a
hook that cannot parse its input fails open - which looks exactly like a passing check.
"""

import hashlib
import json
import os
import subprocess
import sys
import time

SUITES = ["test_main_integration.py", "test_pipeline_utils.py"]
TIMEOUT_SECONDS = 300

# The proof lives in the tree's own git directory (never in a tracked or untracked-visible
# file, so writing it cannot change the tree it vouches for).
PROOF_NAME = "verify_suites_proof.json"
# The suites read the real clock (one test went red "from 8pm Eastern onward"), so a tree that
# is byte-identical can still turn red later. A proof therefore expires; after this many
# seconds the suites run again. The age comes from a timestamp inside the proof, not mtime.
PROOF_TTL_SECONDS = 30 * 60
# Rewritten by this very hook after a green run, and read by no test, so it must not count
# as a change - otherwise the next Stop always sees a "changed" tree and the skip never fires.
FINGERPRINT_IGNORED = {"FUNCTION_INDEX.md"}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # A malformed payload is a broken hook, not a red suite. Fail open rather than
        # wedging every turn in the session behind a parse error.
        return

    # Already blocked once this turn - Claude is fixing it. Blocking again spins.
    if payload.get("stop_hook_active"):
        return

    cwd = tree_under_test(payload.get("cwd"))

    # Taken BEFORE the run: the proof vouches for the tree pytest actually saw, so an edit
    # made while the suites ran leaves a proof that no longer matches, never one that hides it.
    fingerprint = tree_fingerprint(cwd)
    if fingerprint and proof_matches(cwd, fingerprint):
        return

    python = suite_python(cwd)

    try:
        result = subprocess.run(
            [python, "-m", "pytest", *SUITES, "-q", "--no-header",
             "-p", "no:cacheprovider"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        clear_proof(cwd)
        block(f"The test suites did not finish within {TIMEOUT_SECONDS}s. Something is "
              "hanging - find it before ending the turn.")
        return
    except Exception as exc:
        # Could not run pytest at all. Say so rather than reporting a pass.
        clear_proof(cwd)
        block(f"Could not run the test suites: {exc}. The turn cannot be verified.")
        return

    if result.returncode == 0:
        refresh_function_index(cwd, python)
        # Re-take it: a proof is earned only for a tree that did not move while pytest ran.
        # Otherwise pytest may have seen a half-edited tree, and the next Stop must run.
        if fingerprint and tree_fingerprint(cwd) == fingerprint:
            write_proof(cwd, fingerprint)
        else:
            clear_proof(cwd)
        return

    clear_proof(cwd)
    tail = failure_summary(result.stdout or result.stderr or "")
    block(
        "The test suites are RED, so this turn is not done.\n\n"
        f"{tail}\n\n"
        "Fix the failures and run "
        f"`{python} -m pytest test_main_integration.py test_pipeline_utils.py -q` again. "
        "If the change is genuinely correct and the test encodes the old behavior, say so "
        "explicitly and explain why - do not delete or skip a test to get to green."
    )


def _git_out(args, cwd, timeout=30):
    """stdout of a git command, or None when it failed (any failure means "cannot prove")."""
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                timeout=timeout)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout if isinstance(result.stdout, bytes) else (result.stdout or "").encode()


def tree_fingerprint(tree):
    """A hash of everything the suites could read in `tree`, or None when it cannot be taken.

    Covers the path of the tree, the staged state (`git ls-files -s`), and the content of
    every tracked file plus every untracked file that is not gitignored - so a new untracked
    `test_*.py` counts, while `.queue/`, `.worktrees/` and `__pycache__` (gitignored) do not.
    Contents are hashed directly rather than via `git add -N` / `git status`, which would
    write to the index. FUNCTION_INDEX.md is skipped (see FINGERPRINT_IGNORED).

    It sees git's view of the tree, not the environment. Two things are invisible to it and
    can change what the suites do without changing the fingerprint: a gitignored file the
    suites import or read (a local config, a generated module), and anything outside the
    tree - an installed, upgraded or removed package, or the Python itself. The skip trusts
    the tree, not the environment; PROOF_TTL_SECONDS bounds how long that trust lasts.
    None (cannot list, cannot read) always means "run the suites".
    """
    try:
        staged = _git_out(["ls-files", "-s", "-z"], tree)
        listed = _git_out(["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                          tree)
        if not staged or not listed:
            return None
        digest = hashlib.sha256()
        digest.update(os.path.normcase(os.path.realpath(tree)).encode("utf-8", "replace"))
        digest.update(b"\0STAGED\0")
        for entry in staged.split(b"\0"):
            # "<mode> <blob> <stage>\t<path>": keep the whole record, minus the ignored file.
            if entry.rpartition(b"\t")[2].decode("utf-8", "replace") in FINGERPRINT_IGNORED:
                continue
            digest.update(entry + b"\0")
        digest.update(b"FILES\0")
        names = sorted({n.decode("utf-8", "replace") for n in listed.split(b"\0") if n})
        for name in names:
            if name in FINGERPRINT_IGNORED:
                continue
            digest.update(name.encode("utf-8", "replace") + b"\0")
            path = os.path.join(tree, name)
            if os.path.islink(path):
                digest.update(b"L" + os.readlink(path).encode("utf-8", "replace"))
            elif os.path.isfile(path):
                digest.update(b"F")
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
            else:
                digest.update(b"M")  # deleted, or a submodule directory
            digest.update(b"\0")
        return digest.hexdigest()
    except Exception:
        return None


def _proof_path(tree):
    out = _git_out(["rev-parse", "--absolute-git-dir"], tree, timeout=15)
    if not out:
        return None
    git_dir = out.decode("utf-8", "replace").strip()
    if not git_dir or not os.path.isdir(git_dir):
        return None
    return os.path.join(git_dir, PROOF_NAME)


def proof_matches(tree, fingerprint, now=None):
    """True only when a readable, well-formed, unexpired proof for exactly this fingerprint exists.

    Missing, unreadable, truncated, garbled, expired (older than PROOF_TTL_SECONDS) or
    future-dated proofs all return False: the suites run.
    """
    try:
        now = time.time() if now is None else now
        path = _proof_path(tree)
        if not path or not os.path.isfile(path):
            return False
        with open(path, encoding="utf-8") as handle:
            proof = json.load(handle)
        created = proof.get("created") if isinstance(proof, dict) else None
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            return False
        if not (0 <= now - created <= PROOF_TTL_SECONDS):
            return False
        return (proof.get("version") == 2
                and isinstance(fingerprint, str) and proof.get("fingerprint") == fingerprint)
    except Exception:
        return False


def write_proof(tree, fingerprint, now=None):
    """Record that the tree with this fingerprint passed. Never raises, never blocks."""
    if not fingerprint:
        return
    tmp = None
    try:
        path = _proof_path(tree)
        if not path:
            return
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"version": 2, "fingerprint": fingerprint,
                       "created": time.time() if now is None else now}, handle)
        os.replace(tmp, path)
    except Exception:
        return
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def clear_proof(tree):
    """Drop any proof: a red, hung or unrunnable suite must never leave one behind.

    If the file cannot be removed (locked, permissions), overwrite it with a payload no
    reader accepts, so a stale proof for an older fingerprint cannot survive a red run.
    """
    try:
        path = _proof_path(tree)
    except Exception:
        return
    if not path or not os.path.exists(path):
        return
    try:
        os.remove(path)
        return
    except Exception:
        pass
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("poisoned: removal failed, run the suites")
    except Exception:
        return


def venv_python_launches(candidate, timeout=20):
    """True when `candidate` starts and can import pytest.

    A .venv python can exist on disk and still be unusable (its base interpreter was
    removed or updated, or pytest was never installed in it). Using it would turn every
    session's Stop hook red, so suite_python probes before trusting one.
    """
    try:
        return subprocess.run(
            [candidate, "-c", "import pytest"],
            capture_output=True, timeout=timeout,
        ).returncode == 0
    except Exception:
        return False


def suite_python(tree, hook_file=__file__, launches=venv_python_launches):
    """The interpreter the suites should run under.

    `sys.executable` is whichever `python` is first on PATH when Claude Code launches the
    hook (the WindowsApps system Python), not the repo's `.venv`. That split already cost a
    red hook: a test passed under `.venv` and failed under the system Python. Prefer the
    tree's own `.venv`, then the `.venv` of the repo this hook file lives in (a
    `.worktrees/TASK-n` tree carries none), else `sys.executable` as before. A candidate
    that exists but fails `launches(candidate)` is skipped, never returned.
    """
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(hook_file))))
    for root in (tree, repo):
        if not root:
            continue
        for rel in (os.path.join(".venv", "Scripts", "python.exe"),
                    os.path.join(".venv", "bin", "python")):
            candidate = os.path.join(root, rel)
            if os.path.isfile(candidate) and launches(candidate):
                return candidate
    return sys.executable


def tree_under_test(session_cwd):
    """The root of the git tree this session is actually working in.

    The session's shell directory, resolved to its git toplevel. A worker cd'd into
    .worktrees/TASK-7/ gets its own worktree tested, so a half-finished edit there never
    blocks the planner or the chat, and theirs never block it. A shell sitting in a
    subfolder of the main tree (`cd .queue/inbox`) still resolves to the main root - testing
    the raw cwd made pytest collect nothing and report a false red ("no tests ran").

    Falls back to the repo this hook file lives in when the cwd is missing, outside any git
    tree, or a tree without the suites - a hook that cannot find the tests must test the
    main repo, never silently test nothing.
    """
    fallback = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not session_cwd:
        return fallback
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=session_cwd, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception:
        return fallback
    if top and all(os.path.isfile(os.path.join(top, s)) for s in SUITES):
        return top
    return fallback


def refresh_function_index(cwd, python=None):
    """Rewrite FUNCTION_INDEX.md when a turn changed what is in it.

    The index is what CLAUDE.md's anti-duplication rule reads: before writing a helper you
    grep it, and a duplicate never fails a test. Nothing regenerated it, so it drifted 221
    lines behind - two functions a worker added were simply missing, which means the grep
    that is supposed to prevent a duplicate could not see them.

    This runs on the Stop hook rather than PostToolUse on purpose. PostToolUse fires on
    every edit, including a one-line comment, and would churn a 565-line file all day. Here
    it runs once per turn, only after the suites are green, and only when a tracked source
    file actually changed - so a turn that edited nothing but markdown rewrites nothing.

    It never blocks. A stale index is a problem; a turn that cannot end because an indexer
    hiccuped is a worse one.
    """
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", "*.py"],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
        touched = [f for f in changed.stdout.split()
                   if f.endswith(".py") and not f.startswith(".claude/")]
        if not touched:
            return

        subprocess.run(
            [python or suite_python(cwd), "build_function_index.py"],
            cwd=cwd, capture_output=True, text=True, timeout=60,
        )
    except Exception:
        return


def failure_summary(output, max_lines=30):
    """The part of pytest's output that names what broke.

    Taking a plain tail is wrong: with -q the run is mostly progress dots, and on a suite
    this size they pad the end of the capture, so the last 25 lines were dots and the
    reason handed back said nothing. Start at the FAILURES banner instead, and fall back
    to the lines that carry a node id or the final counts.
    """
    lines = output.strip().splitlines()

    for i, line in enumerate(lines):
        if "= FAILURES =" in line or "= ERRORS =" in line:
            return "\n".join(lines[i:i + max_lines])

    signal = [ln for ln in lines
              if ("::" in ln or " failed" in ln or " error" in ln)
              and not set(ln.strip()) <= {".", "F", "E", "s", "x", "%", "[", "]", " "}
              and not ln.strip().endswith("%]")]
    if signal:
        return "\n".join(signal[-max_lines:])

    return "\n".join(lines[-max_lines:])


def block(reason):
    json.dump({"decision": "block", "reason": reason}, sys.stdout)


if __name__ == "__main__":
    main()
