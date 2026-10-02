# The task queue

A mailbox between two Claude Code terminals: a **planner** (plan mode) that writes tasks,
and a **worker** (bypass permissions) that executes them one at a time.

## State is the directory, not a field

```
inbox/    queued, nobody has claimed it      planner writes here
active/   a worker is on it right now        worker only
done/     verify passed                      worker only
blocked/  verify failed 3x, or scope clash   worker only
```

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

This needs `crossSessionInbound: "accept"`, already set in `.claude/settings.local.json`.
Without it, a bypass-permissions session holds incoming messages for approval and drops
them after five minutes - the worker would look like it was ignoring the planner.

`/queue-status` in either terminal shows the whole queue without changing anything.

## The worker does not commit

It leaves changes in the working tree for review. Two agents sharing one git index
fight over it, and an autonomous `git reset --hard` would wipe whatever the other
terminal (or you, by hand) had uncommitted. Review and commit yourself.
