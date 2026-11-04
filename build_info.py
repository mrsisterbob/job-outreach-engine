"""Which commit is this process running? Pure: no `import main`, no clock, no real git or files here.

Every outside fact is passed in (`environ`, `read_file`, `run_git`), so tests need none. The answer names
the place it came from, and says `unknown` instead of guessing when none answers. There is deliberately no
comparison against the repo head: the running process cannot see the repo's newest commit, and a made-up
"up to date" is the failure this module exists to prevent.
"""
from __future__ import annotations

import re

# UNVERIFIED in this repo: RENDER_GIT_COMMIT is a Render-set variable recalled from Render's docs. The `source`
# field of the answer says which path really answered, so Kevin can check /health after the next deploy.
RENDER_COMMIT_ENV = "RENDER_GIT_COMMIT"
BUILD_COMMIT_FILE = "BUILD_COMMIT"
_HEX = re.compile(r"^[0-9a-f]{7,40}$")


def _clean(value):
    """A 7-40 character lower-case hex string, or "" (empty, `HEAD`, an error sentence, a non-string)."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    return text if _HEX.match(text) else ""


def _first_line(value):
    return value.splitlines()[0] if isinstance(value, str) and value.splitlines() else ""


def _answer(commit, source):
    return {"commit": commit, "short": commit[:7], "source": source}


def running_commit(environ, read_file=None, run_git=None):
    """{"commit", "short", "source"} from, in order: environ RENDER_GIT_COMMIT, the first line of the file
    BUILD_COMMIT (read_file(name) -> text), `git rev-parse HEAD` (run_git(args) -> text). A candidate counts only
    when it is 7-40 hex characters; a source that is missing, invalid or raises is skipped. None answers:
    commit "" and source "unknown" (never a partial guess)."""
    try:
        commit = _clean((environ or {}).get(RENDER_COMMIT_ENV))
    except Exception:
        commit = ""
    if commit:
        return _answer(commit, "env:" + RENDER_COMMIT_ENV)
    if read_file is not None:
        try:
            commit = _clean(_first_line(read_file(BUILD_COMMIT_FILE)))
        except Exception:
            commit = ""
        if commit:
            return _answer(commit, "file:" + BUILD_COMMIT_FILE)
    if run_git is not None:
        try:
            commit = _clean(_first_line(run_git(["rev-parse", "HEAD"])))
        except Exception:
            commit = ""
        if commit:
            return _answer(commit, "git")
    return _answer("", "unknown")


def format_build(info):
    """`build abc1234 (env:RENDER_GIT_COMMIT)`, or `build unknown (set on Render: see /health)` (never claims a commit)."""
    short = (info or {}).get("short") if isinstance(info, dict) else ""
    if not short:
        return "build unknown (set on Render: see /health)"
    return f"build {short} ({info.get('source') or 'unknown'})"
