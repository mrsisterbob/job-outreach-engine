---
id: TASK-257
scope: build_info.py, test_build_info.py
verify: python -m pytest test_build_info.py -q -p no:cacheprovider 2>&1 | tail -n 1 | grep -E "^([89]|[1-9][0-9]+) passed" | grep -Ev "failed|error"
---

## Target
A pure module `build_info.py` that answers "which commit is this process running" from the places a deployed copy can know it, names the place it learned it from, and says `unknown` instead of guessing when none answers.

## Proof it is needed
`python -m pytest test_build_info.py -q -p no:cacheprovider` on 2026-10-31 (main at 4760805): `ERROR: file or directory not found: test_build_info.py`, exit 4, so the verify exits 1. Pass-regex proven able to pass: `python -m pytest test_hirebase_feed.py -q -p no:cacheprovider` ends `72 passed in 0.09s` and matches `^([89]|[1-9][0-9]+) passed`. Why: Render once ran 69 commits behind main (2026-10-27) and nothing in the repo can say which commit is live; `grep -i -E "RENDER_GIT|git_commit|build_info"` over `main.py` and `FUNCTION_INDEX.md` finds nothing, and `/health` (`health_check`, main.py:19467) returns only `status, mode, elapsed_ms, persistence`.

## Notes
- Search first: `grep -i -E "commit|build|version" FUNCTION_INDEX.md`. Pure, no `import main`, no clock. Every outside fact is passed in so tests need no real git or files: `running_commit(environ, read_file=None, run_git=None)`.
- **Order of sources, decided.** (1) `environ["RENDER_GIT_COMMIT"]`, source `"env:RENDER_GIT_COMMIT"`; (2) the first line of a file `BUILD_COMMIT` in the working directory via `read_file("BUILD_COMMIT")`, source `"file:BUILD_COMMIT"`; (3) `run_git(["rev-parse", "HEAD"])`, source `"git"`; (4) none answers: `commit=""`, source `"unknown"`. `RENDER_GIT_COMMIT` is a Render-set variable recalled from Render's docs and is **UNVERIFIED in this repo**: say so in a comment above the constant, and say in the Log that Kevin checks `/health` after the next deploy (the `source` field tells him which of the four paths answered).
- A candidate counts only if, after strip and lower-casing, it is 7-40 hex characters. Anything else (empty, `HEAD`, a sentence from a git error, a non-string) is skipped and the next source is tried; a source that raises is skipped, never fatal. Never returns a partial guess.
- Returns a dict `{"commit": <40-or-fewer hex, lower-case, or "">, "short": <first 7 or "">, "source": <str>}`. `format_build(info)` -> `"build abc1234 (env:RENDER_GIT_COMMIT)"` or `"build unknown (set on Render: see /health)"`; the unknown text must not claim a commit.
- `behind_by(live_short, git_log_lines)` is NOT in this task. Do not add a comparison against the repo head: the running process cannot see the repo's newest commit, and a made-up "up to date" is the failure this task exists to prevent.
- Tests (`test_build_info.py`, at least 8): env wins over file and git; file wins over git; git used when env and file are empty; all empty -> unknown with `short == ""`; a non-hex env value (`"latest"`) is skipped and the file answers; `run_git` raising is skipped; uppercase hex is lower-cased; a 6-character value is rejected; `format_build` for a known and an unknown commit; a non-string `environ` value does not raise.

## Log

