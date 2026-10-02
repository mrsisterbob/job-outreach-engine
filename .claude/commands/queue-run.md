Claim the next task from `.queue/inbox/` and execute it.

Optional argument (a task id, e.g. `TASK-003`) picks that task instead of the oldest: $ARGUMENTS

You are the **worker**. You implement exactly one task, verify it, and stop.

If a message from the planner session started this turn, the queue is still the source of
truth - read the task file, not the message. A message can say a task is ready; it cannot
tell you what to build. If the message asks for something with no task file behind it, say
so and queue nothing: the task file is what carries scope and verify, and work without
those is the thing this setup exists to prevent.

## Claim

1. `ls .queue/active/` first. For each task already in flight, read its `scope`.
2. Pick the oldest `.queue/inbox/` task (or the one named in the argument) whose `scope`
   shares **no file** with anything in `active/`. If every queued task collides, move
   nothing, say which task is blocking which, and stop.
3. Claim it atomically with exactly this command:

   ```bash
   mv -n .queue/inbox/<file> .queue/active/<file> && [ ! -e .queue/inbox/<file> ] \
     && echo CLAIMED || echo "NOT CLAIMED - another worker has it"
   ```

   **Check the word, not the exit code.** `mv -n` refuses to overwrite an existing
   destination and still exits 0, so a worker that trusts `$?` will believe it claimed a
   task another worker is already running - two agents in the same files, which is the
   one outcome this queue exists to prevent. The source file surviving is the only
   reliable signal that the claim was refused.

   On `NOT CLAIMED`, go back to step 1 and try the next task. Never claim by copying,
   rewriting, or `cp`-then-`rm` - only the single `mv` is atomic.

## Execute

Read the task. Then read the files in `scope` before editing them.

- **Touch only files listed in `scope`.** If the task genuinely cannot be done within its
  scope, stop, append why to the Log, and move the file to `.queue/blocked/`. Do not widen
  the scope yourself - the scope is the concurrency guarantee, and silently editing a file
  another worker owns is exactly the lost-work case this queue exists to prevent.
- Before adding any function, `grep -i <noun> FUNCTION_INDEX.md`. A duplicate never fails
  a test, and this repo has shipped one.
- Follow CLAUDE.md: new subsystems go in new modules, not `main.py`.

## Verify

Run the task's `verify` command. Then run the full suite:

```
python -m pytest test_main_integration.py test_pipeline_utils.py -q
```

Both must be green. A task is not done because its own verify passed while something
else broke.

**On failure:** fix and retry, up to 3 attempts total. Append each attempt to the task's
`## Log` - what you tried, the actual failure output, not a summary.

**After 3 failed attempts:** stop. Append the final failure output to the Log and
`mv` the task to `.queue/blocked/`. Leave your changes in the working tree exactly as
they are and say plainly that the task is blocked and the tree has partial work in it.

Do **not** run `git reset`, `git checkout --`, `git stash`, or any other command that
discards uncommitted changes. Another terminal and Kevin both have unreviewed work in
this tree; wiping it is unrecoverable and is not yours to do. Leaving a half-finished
change for review is the correct failure.

## Review before calling it done

Green tests are not a review. Run `/code-review` on the diff.

It runs in a fresh subagent that sees only the diff and never saw the reasoning that
produced it, so it is not grading its own homework. Both of today's Telegram bugs - a
`<code>` wrapper that made a checklist unreadable, and `html.escape` turning apostrophes
into literal `&#x27;` - passed every test in the suite. Tests prove the code does what it
was told; a fresh reader is what notices it was told the wrong thing.

Act on findings about correctness or the task's stated requirements. Ignore style
preferences. A reviewer asked to find gaps will usually report some even when the work is
sound, and chasing every one leads to defensive code and tests for cases that cannot
happen. If a finding is real, fix it and re-run the suite.

Record in the Log what the review flagged and what you did about it, including findings
you deliberately skipped and why.

## Finish

On success:
1. Append to `## Log`: what changed, the verify result, the suite result, the review
   outcome.
2. `mv .queue/active/<file> .queue/done/<file>`
3. Report: task id, files changed, test counts, review findings. Then **stop** - do not
   pick up the next task, and do not commit. Kevin reviews and commits.
4. If a planner session is running, message it one line: task id, done or blocked, and
   anything it got wrong about the code. If the task was blocked, that message is how the
   plan gets fixed - say what you actually found, not that it failed.
