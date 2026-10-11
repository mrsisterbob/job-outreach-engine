# The task queue

A mailbox between two Claude Code terminals: a **planner** (plan mode) that writes tasks,
and a **worker** (bypass permissions) that executes them one at a time and merges each into `main`.

## State is the directory, not a field

```
inbox/    queued, nobody has claimed it      planner writes here
active/   a worker is on it right now        worker only
done/     merged into main, suites green     worker only
blocked/  stopped; the Log says why          worker only
```

`done/` means merged, not just verified: a task moves there only after its merge passes both
suites on `main`, which is what dependency checks rely on. `blocked/` covers verify failing 3
times, a scope clash, a held merge, a merge conflict and a merge reverted for turning `main`
red. In every case but a scope clash the work survives on branch `task/TASK-n` - finished,
or as a `WIP` commit when verify failed.

One task is one file, and the directory it sits in IS its state. Claiming a task is a
single `mv -n`, an atomic rename: two workers cannot both claim the same task, because
the second rename refuses to overwrite the destination.

Watch the one trap: `mv -n` refuses **silently and still exits 0**. The only reliable
test of a claim is whether the source file disappeared, which is what `/queue-run` checks.
Plain `mv`, not `git mv` - a fresh task file is untracked, so `git mv` fails outright, and
staging every task would drag queue churn through the index you are trying to review.

**This is deliberately not a single QUEUE.md.** A shared markdown file has no locking.
The planner appending TASK-004 while the worker rewrites the file to mark TASK-001 done
silently loses one of the two writes - no error, no trace, the task just never runs.
That is the same silent-zero failure this repo keeps getting bitten by (a CRM read
rejected behind HTTP 200 returning `[]`, a hand-rolled contact scan that could not see
three tabs). One writer per file is what makes the loss impossible rather than unlikely.

## Task file format

Filename: `TASK-<n>-<short-slug>.md`

```markdown
---
id: TASK-003
scope: pipeline_utils.py, test_pipeline_utils.py
verify: python -m pytest test_pipeline_utils.py -q
---

## Target
One sentence describing what "done" looks like.

## Notes
Anything the worker needs that is not obvious from the scope: a file:line to start at,
a prior decision, a trap to avoid.

## Log
<!-- worker appends here: attempt, verify output, outcome -->
```

### `scope` is load-bearing

It lists every file the task may touch, and the worker refuses a task whose scope
overlaps anything already in `active/`. That is the entire concurrency story: with
`main.py` at 13k lines, almost every task wants it, so two workers in that file at once
is the realistic way to lose work. The guard makes the collision a refusal instead.

### `verify` is required

A command that exits 0 when the task is genuinely done. Not "the function returns the
right thing" - the command that proves the *next* run reads back what it should. A task
without a real verify command does not go in the queue.

## Running it

One-time setup, per machine:

```
# in the planner terminal (plan mode)
/rename planner

# in the worker terminal (bypass permissions)
/rename worker
```

Then, per task - you type one line, in the planner only:

```
/queue-add <what you want built>, then tell @worker it is queued
```

The planner writes the task file and sends `@worker` a message. **The worker is idle, so
Claude Code starts a new turn with that message** - it wakes up, runs `/queue-run`, and
works. You do not type in the worker terminal.

Nothing watches the `inbox/` directory; no file appearing there triggers anything. The
message is what wakes the worker, not the file. If messaging is off or the planner is not
running, `/queue-run` typed by hand does the same thing.

A montelattice-site request (pages, layout, CSS, templates, site routes) typed into this
planner is not queued here: `/queue-add` forwards it word for word to the `site-planner`
session, and queues only the engine half of a mixed request. `site-planner` must be running
(`/rename site-planner` in a montelattice-site terminal), or the planner prints the request
back as **NOT forwarded** for you to paste there. A request the site forwards in is queued
here and never sent back.

Each command checks the session's name before doing anything. `/queue-add` typed in any
session not named `planner`, or `/queue-run` typed in one not named `worker`, does no work
there: it forwards the command word for word to the session with the right name, tells you in
one line, and stops (or prints it back as **NOT forwarded** when that session is not running).
So both sessions must be `/rename`d exactly `planner` and `worker`. If a session's name cannot
be read, the command does nothing and asks for the `/rename`. `/queue-status` is read-only and
has no guard: it works in any session. A forwarded `/queue-run` arrives as text, and the worker
runs it because its guard says to, so "forwarded" means sent, not necessarily running.

This needs `crossSessionInbound: "accept"`, already set in `.claude/settings.local.json`.
Without it, a bypass-permissions session holds incoming messages for approval and drops
them after five minutes - the worker would look like it was ignoring the planner.

`/queue-status` in either terminal shows the whole queue without changing anything.

## The worker works in its own worktree, then merges it itself

Each claimed task gets its own checkout: `.worktrees/TASK-n/` on branch `task/TASK-n`,
started from `main`. The worker edits, tests and commits there.

Why: with one shared folder, every agent's Stop hook tested every other agent's
half-finished edits. On 2026-10-03 the worker's mid-save `main.py` turned the chat's hook
red with a 500 that had nothing to do with the chat. The hook now tests the git tree the
session's shell is in, so each session only answers for its own work.

When the task's verify, both suites and `/code-review` are green, the worker merges it from
the main folder and moves on to the next inbox task, until the inbox is empty:

```bash
mkdir .queue/merge.lock                        # one merging worker at a time
git status --porcelain                         # must print nothing, on branch main
.venv/Scripts/python -m pytest test_main_integration.py test_pipeline_utils.py -q   # baseline: green before
git merge --no-ff task/TASK-n -m "Merge TASK-n: ..."
.venv/Scripts/python -m pytest test_main_integration.py test_pipeline_utils.py -q   # green after
git worktree remove .worktrees/TASK-n && git branch -d task/TASK-n   # only once main is green
rmdir .queue/merge.lock
```

If a worker dies holding the lock, later merges wait on it; `rmdir .queue/merge.lock` clears it.
The baseline run is what keeps a `main` that was already red from being blamed on - and
reverting - whichever task merges next.

`--no-ff` is what makes a bad merge undoable: it always leaves one merge commit, which
`git revert -m 1` can take back as a single new commit.

Three guards, each of which tells you in the worker's output and in its message to the planner:
- **Main folder not clean** (anything from `git status --porcelain`, or not on `main`), or
  **`main` already red before the merge**: no merge. The task goes to `blocked/` as "merge
  held", its branch stays, and the loop stops. Commit or move your own work (or fix `main`),
  then merge that branch by hand or move the task back to `inbox/` - the worker resumes on
  the existing branch.
- **Merge conflict:** `git merge --abort`, the task goes to `blocked/`, its branch stays, and
  the loop moves on to the next task.
- **Suites red on `main` after the merge:** `git revert -m 1` of that merge (never a reset),
  the task goes to `blocked/` with the revert's sha in its Log, its branch stays, and the loop
  moves on. If `main` is still red after the revert, the loop stops. If the main folder changed
  during the suite run, the worker cannot tell whose red it is, so it reverts nothing, leaves
  the merge in place and stops.

**A reverted task cannot be re-landed by merging its branch again.** Its commits are already
in `main`'s history, so `git merge task/TASK-n` says "Already up to date" and changes nothing.
To re-land it, move the task back to `inbox/`: the worker resumes on the branch, merges `main`
into it, runs `git revert <revert-sha>` there to bring the work back as new commits, and fixes
what was red on top. By hand, the equivalent on `main` is `git revert <revert-sha>`.

What the worker does on `main`, and nothing else: a `--no-ff` merge and, when the suites go
red, a revert. It never pushes, never commits there directly, never resets and never stashes,
and it holds every merge while the main folder has uncommitted work - your unreviewed changes
are never mixed into a merge. You review after the fact: `git log --merges` on `main`, and
`git show <merge>` for any one of them.

Two consequences to know:
- **A task can only build on merged work, and `done/` means merged.** The worktree starts from
  `main`, and a task moves to `done/` only after its merge passes the suites there. So if
  TASK-4 needs TASK-3, the worker checks that TASK-3 is in `done/`; if it is not, TASK-4 waits
  in the inbox and the worker tries the next task instead.
- **The loop ends** when the inbox is empty, or when everything left in it collides in scope
  or waits on a task that is not in `done/`. The final report says what is left and why.

Known gap: one test (`test_main_integration.py`, the montelattice-site check) looks for that
repo *beside* this one, so inside `.worktrees/` it skips instead of running. It still runs
on `main` after the merge.
