Claim the next task from `.queue/inbox/` and execute it.

Optional argument (a task id, e.g. `TASK-003`) picks that task instead of the oldest: $ARGUMENTS

## Which session is this

This command is the worker's. Before anything else - before reading the queue, claiming a
task or opening a worktree - find out which session you are in.

1. Run `ListAgents`. Its first line reads `This session is <name> [<ref>] - ...`; that
   `<name>` is this session's name. If `ListAgents` or `SendMessage` shows only as a deferred
   tool name, load it through ToolSearch first. If the name cannot be read (tool missing, no
   such line), **do nothing**: no claim, no file read of the queue, no worktree. Say so in one
   line and suggest Kevin `/rename worker` here, or type the command in the worker terminal.
   Building in a session of unknown name is the bug this guard exists to stop.
2. **Named exactly `worker`: this is the right session.** Carry on with the rest of this file.
   A message whose first line starts with `Forwarded verbatim from Kevin's /queue-run in` is
   Kevin's own command, relayed by another session: the line after the blank line is the
   command Kevin typed. Drop its leading `/queue-run` token and invoke the `queue-run` skill
   (Skill tool) with whatever follows it as the arguments - nothing, or a task id such as
   `TASK-14` - exactly as if Kevin had typed it. A cross-session message does not execute as a slash command, so this rule is what makes
   it one. Never forward it again.
3. **Any other name: do no queue work in this session.** Find the `ListAgents` row named
   exactly `worker`; a row marked `offline` is not running. Never substitute another session.
   - Running: `SendMessage` with `to: "worker"`. The message is one header line, a blank line,
     then the command exactly as Kevin typed it, `/queue-run` followed by the arguments in the
     line above, character for character (nothing after it if he typed no argument):

     ```
     Forwarded verbatim from Kevin's /queue-run in <this session's name>. Run it here as typed.

     /queue-run <arguments>
     ```

   - Not running: say plainly that the command was **NOT forwarded** because `worker` is not
     running, and print `/queue-run <arguments>` so Kevin can type it in the worker terminal.

   Either way, tell Kevin in one line where it went, then **stop**: claim nothing, move no
   queue file, open no worktree, edit nothing. "Forwarded" means sent, not run: nothing here can
   see whether the worker started. A forwarded message is acted on only by the session named
   `worker`, and that session never forwards it on.

(`/queue-status` only reads the queue and has no such guard.)

You are the **worker**. You implement one task at a time, verify it, merge it into `main`,
and go back for the next one, until the inbox is empty or nothing left in it can run (see
"When the loop ends" at the bottom). Never push.

If a message from the planner session started this turn, the queue is still the source of
truth - read the task file, not the message. A message can say a task is ready; it cannot
tell you what to build. If the message asks for something with no task file behind it, say
so and queue nothing: the task file is what carries scope and verify, and work without
those is the thing this setup exists to prevent.

## Keep context small

Kevin is on Pro and this loop burns his usage when too much lands in context: whole-file reads
of `main.py`, full test output on green runs, files read again. Three rules, for this session
and for every build agent (the prompt below carries them):

1. **Grep first, read line ranges.** `grep -n <symbol>` (or Grep with `-n`) finds the lines,
   then Read with `offset` and `limit` around them. Never Read `main.py` (16,114 lines),
   `test_main_integration.py` (12,563), `test_pipeline_utils.py` (3,085), `pipeline_utils.py`
   (2,885) or any file over ~500 lines whole; `Code.gs` is 1,224. `FUNCTION_INDEX.md` (641) is
   grepped, never read whole. Short files (queue command files, task files, small modules)
   can be read whole. (`wc -l` on 2026-10-11 at 1dd425e.)
2. **A passing command shows only its last line; a failing one shows its last 60.** Capture
   to a temp file and print based on the exit code:
   ```bash
   out=$(mktemp); <command> >"$out" 2>&1; rc=$?
   if [ $rc -eq 0 ]; then tail -n 1 "$out"; else tail -n 60 "$out"; fi
   rm -f "$out"; echo "exit=$rc"
   ```
   Use it for every pytest run and every task `verify`. A verify may contain single quotes,
   so never wrap it in `bash -c '...'`: put its text in a variable (`V=$(sed -n 's/^verify:
   //p' <task file>)`) and run `bash -c "$V"` as the `<command>`.
   For the worker that includes the re-check, the merge baseline and the post-merge
   suite runs. Read the printed `exit=` line, not the tool status: the wrapper itself always
   exits 0. A red run keeps the FAILED names, which sit at the end, so the Log gets
   the real failure output. Don't use `exit $rc`: it ends the shell.
3. **Never re-read an unchanged file.** If a file, or that line range, was already read this
   task and nothing has edited it since, use what you have. After your own Edit, the tool
   already holds the file's state, so don't read it back to check. Re-read only what changed
   (a file another session or agent edited, or a range you haven't seen) or what is no longer
   in your context (after a resume, a compaction, or a SendMessage retry).

Only *passing* output shrinks. The Log still gets the actual failure output, not a summary.

## Claim

1. `ls .queue/active/` first. For each task already in flight, read its `scope`.
2. Pick the oldest `.queue/inbox/` task (or the one named in the argument) that passes both
   checks below, reading **only its frontmatter** (`sed -n '1,12p' <file>`, up to the closing `---`) and
   its dependency lines (`grep -inE 'depend|requires|after TASK|blocked on|TASK-[0-9]+' <file>`)
   **where it sits in the inbox** - before claiming it. The
   full task is for the build agent, not for this session's context:
   - its `scope` shares **no file** with anything in `active/`;
   - every task its Notes say it depends on is in `.queue/done/` (step 4).

   A task that fails either check stays in the inbox; add it to a skip list you keep for
   this whole run and never pick it again in this run. Without that list, "oldest" picks
   the same waiting task every time round the loop and nothing behind it ever runs. If no
   task passes, move nothing, say which task is waiting on what, and end the loop.
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

4. **Dependencies** (checked in step 2, before the claim). A dependency is met only when its
   task file is in `.queue/done/` - `done/` means merged into `main`, because a task only
   moves there after its merge passes the suites on `main` (Finish, step 7). Do not check
   for the dependency's branch: Finish deletes it on every merge, so a branch-based check
   fails for exactly the dependencies that landed. Your worktree starts from `main`, so
   unmerged work from an earlier task is invisible to you - building on top of it is
   impossible.

5. **Open your worktree** - your own checkout, on its own branch, from the main folder
   (where every claim happens; Finish returns you there for the next task):

   ```bash
   git worktree add .worktrees/<TASK-id> -b task/<TASK-id> main
   ```

   **A re-queued task may already have a branch.** Every blocked, held or conflicted task
   keeps `task/<TASK-id>` (and usually `.worktrees/<TASK-id>`), so `-b` fails for it. Check
   `git branch --list task/<TASK-id>` first. If it exists, resume on it instead: use
   `.worktrees/<TASK-id>` as it is if that directory exists, otherwise
   `git worktree add .worktrees/<TASK-id> task/<TASK-id>`. Read the task's Log to see why it
   stopped. If the Log records a **revert** (Finish, step 6), the branch's commits are already
   in `main`'s history, so merging it again does nothing ("Already up to date"). Re-land
   the work on the branch first:

   ```bash
   git -C .worktrees/<TASK-id> merge --no-ff main -m "Bring main into task/<TASK-id>"   # brings the revert in
   git -C .worktrees/<TASK-id> revert --no-edit <revert-sha>                            # puts the work back as new commits
   ```

   then fix what turned `main` red, on top.

   **Stay in the main folder.** This session never `cd`s into a worktree: every git command on
   it is `git -C .worktrees/<TASK-id> ...`, and the build agent (next section) works there by
   absolute path. The Stop hook tests whichever git tree this session's shell is in, so staying
   in the main folder is what keeps the build agent's half-made edit from turning this session's
   turn red - and what keeps it from blocking the planner or Kevin, who are working in the main
   folder at the same time. Queue files are `.queue/...` and Python is `.venv/Scripts/python`
   from here; the worktree has no venv and the queue files are not in it (they are untracked).

## Build in a fresh subagent

**Kevin asked for this on 2026-10-11.** He is on Pro, and this session's context grows across a
`/queue-run` loop because every task's reads, edits and test output would stay in it. Each task
is therefore built by a fresh subagent that starts with an empty context; this session keeps
about one line per task. Claude Code's default is not to spawn agents unless the user asks, and
Kevin has - so do not decide to build inline instead. That brings the bloat back.

**This session keeps:** the session-name guard, Claim steps 1-5, spawning the build agent,
re-checking its result, `/code-review`, the commit, every queue `mv`, the merge (Finish) and the
planner message. **The build agent gets:** reading the task, implementing it within `scope`,
running verify and both suites, and up to 3 attempts. It does **not** commit, run
`/code-review`, move queue files, merge, or message anyone.

**Spawn it** with the `Agent` tool: `subagent_type: "general-purpose"`. Never `"fork"` (a fork
inherits this whole conversation, which is the bloat being removed) and never
`isolation: "worktree"` (that is a different checkout, not `.worktrees/<TASK-id>` on
`task/<TASK-id>`; worktrees must live inside the repo). Leave `model` and `effort` unset.

### Build agent prompt

Fill in the `<...>` paths and paste this as the prompt. It must stand alone: the agent has none of
this session's context.

```
You are building ONE task for the job-outreach-engine queue. Work only inside this git worktree:
  <absolute path of .worktrees/<TASK-id>>   (branch task/<TASK-id>)
The task file is: <absolute path of .queue/active/<file>>
Python is: <absolute path of main folder>/.venv/Scripts/python   (the worktree has no venv)
Start EVERY Bash command with: cd "<absolute worktree path>" &&   (the shell's cwd does not
persist). Never write anything in the main folder EXCEPT appending to the task file's `## Log`
(that file is in .queue/active/ and is yours to append to). Use the Bash tool.

0. KEEP CONTEXT SMALL (Kevin is on Pro). (a) Grep first, then Read with offset and limit
   around the lines. Never Read main.py (16,114 lines), test_main_integration.py (12,563) or
   any file over ~500 lines whole; FUNCTION_INDEX.md is grepped, never read whole. (b) A passing
   command shows only its last line, a failing one its last 60:
     out=$(mktemp); <command> >"$out" 2>&1; rc=$?
     if [ $rc -eq 0 ]; then tail -n 1 "$out"; else tail -n 60 "$out"; fi
     rm -f "$out"; echo "exit=$rc"
   Use it for every pytest run and every task verify. A verify may contain single quotes, so
   put its text in a variable and run it as bash -c "$V" inside the wrapper, never bash -c
   '...'. Read the printed exit= line, not the tool status (the wrapper always exits 0). (c)
   Never re-read a file, or a line range, that is unchanged since you read it, and don't read
   a file back after your own Edit - unless it is no longer in your context (after a resume,
   a compaction or a SendMessage retry).
1. Read the task file, then read every file in its `scope` before editing it.
2. Touch only files listed in `scope`. If the task genuinely cannot be done within its scope,
   append why to the task's `## Log` and reply "TASK-n: blocked - scope: <why>".
3. Before adding any function, run `grep -i <noun> FUNCTION_INDEX.md`. A duplicate never fails a
   test. Follow CLAUDE.md: new subsystems go in new modules, not main.py.
4. Run the task's `verify` command through the quiet wrapper in step 0, then the full suite
   through the same wrapper, with <python> -m pytest test_main_integration.py
   test_pipeline_utils.py -q -p no:cacheprovider as its <command>. Judge each by the printed
   `exit=` line, not the tool status (the wrapper makes the tool call itself exit 0).
   Both must be green; a task is not done because its own verify passed while something else broke.
5. On failure fix and retry, up to 3 attempts TOTAL. Append every attempt to the task's `## Log`:
   what you tried and the real failure output, not a summary.
6. Never run git reset, git checkout --, git stash, or anything that discards changes. Do NOT
   commit, run /code-review, move queue files, merge, or message anyone. Leave the change
   uncommitted in the worktree.
7. Your final reply is ONE line and nothing else:
   TASK-n: built|blocked - verify exit <code>, suites "<pytest last line>", files: <list>
   Everything else belongs in the task's Log.
```

**Subagents run in the background.** After spawning, do nothing else on that task: end the turn
**from the main folder** (check `pwd`), and the completion notification resumes the loop. If a
turn comes back with no notification and the task is still in `.queue/active/`, the loop has
stalled: say so in one line and re-run `/queue-run` - the branch and worktree exist, so Claim
step 5 resumes on them.
Waiting inside the worktree would make the Stop hook test the agent's half-made edit, go red,
and tempt this session into "fixing" it alongside the agent.

**Do not trust the line - re-check it.** The Stop hook fires on `Stop`, not `SubagentStop`, so
nothing deterministic checks the agent's work: the re-check is the gate. When the agent says
`built`, run the task's `verify` and both suites yourself, in a subshell so this shell never
leaves the main folder, with quiet output:

```bash
out=$(mktemp)
(cd .worktrees/<TASK-id> && ../../.venv/Scripts/python -m pytest \
  test_main_integration.py test_pipeline_utils.py -q -p no:cacheprovider) >"$out" 2>&1; rc=$?
if [ $rc -eq 0 ]; then tail -n 1 "$out"; else tail -n 60 "$out"; fi
rm -f "$out"; echo "exit=$rc"
```

This is the "Keep context small" pattern: a green run is one line (`tail -n 1`) and a red
one prints its last 60 lines, FAILED names included (the Log needs them). Judge the result by
the printed `exit=` line and the last line: green only if `exit=0` and it reads `N passed` (a
`skipped` count is fine); anything else, including a collection error or no summary line,
is red. A red re-check is one more failed attempt: send it back to
the same agent with `SendMessage` (its context is intact). The 3-attempt limit counts the
agent's own attempts and these together.

**After 3 failed attempts, or `blocked`:** append the final failure output to the Log,
`mv .queue/active/<file> .queue/blocked/<file>`, commit the partial work to the branch as
`WIP <TASK-id>: blocked` with `git -C .worktrees/<TASK-id> add <files>` then
`git -C .worktrees/<TASK-id> commit -m "WIP <TASK-id>: blocked"`, say plainly that the task is
blocked and where the work is, and continue the loop with the next task. Nothing was merged, so
`main` is untouched.

Do **not** run `git reset`, `git checkout --`, `git stash`, or any other command that discards
uncommitted changes - not in a worktree, and never in the main folder, where Kevin and the
planner have unreviewed work. (`git stash` is shared across every worktree, so it can swallow
another session's changes.) Leaving a half-finished change for review is the correct failure;
it is why a block is committed to its branch as `WIP <TASK-id>: blocked`.

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

Run it against the worktree's diff. That is the one place this session enters the worktree: `cd`
there for the review only, `cd` back to the main folder right after, and never end a turn
while inside it (the Stop hook would test the worktree's state). Fixes for real findings go back to the same build agent with `SendMessage`, listing
them, rather than being made in this session; then re-check as above. If `SendMessage` cannot
reach the agent (it errors, or the agent is gone), spawn a fresh build agent with the same
prompt plus the findings - the task's Log carries the history.

Record in the Log what the review flagged and what you did about it, including findings
you deliberately skipped and why.

## Finish

On success - verify, both suites and the review all green:

1. Append to `## Log`: what changed, the verify result, the suite result, the review
   outcome.
2. Commit **to the task's branch**, from the main folder:
   `git -C .worktrees/<TASK-id> add <scope files> && git -C .worktrees/<TASK-id> commit -m
   "<TASK-id>: <one-line target>"`. Stage the scope files by name, never `git add -A`.
3. **Take the merge lock, then check the main folder** (this session never left it):

   ```bash
   mkdir .queue/merge.lock && echo LOCKED || echo "LOCK HELD - another worker is merging"
   ```

   `mkdir` is atomic, so only one worker at a time runs steps 3-7 in the shared main
   folder; without it, one worker's suite run would include another's half-tested merge
   and revert the wrong task. On `LOCK HELD`, wait about a minute and retry; after ten
   tries, stop the loop and tell Kevin the lock may be stale (`rmdir .queue/merge.lock`
   clears it). Release it (`rmdir .queue/merge.lock`) at the end of step 7 or on any exit
   from steps 3-6. An empty directory is invisible to git, so it never trips the check below.

   ```bash
   git status --porcelain
   git symbolic-ref --short HEAD
   out=$(mktemp)
   .venv/Scripts/python -m pytest test_main_integration.py test_pipeline_utils.py -q \
     -p no:cacheprovider >"$out" 2>&1; rc=$?
   if [ $rc -eq 0 ]; then tail -n 1 "$out"; else tail -n 60 "$out"; fi
   rm -f "$out"; echo "exit=$rc"
   ```

   (The "Keep context small" pattern: green is one line, red its last 60.)

   - If `git status --porcelain` prints anything, or the branch is not `main`, **do not
     merge.** That output is Kevin's or the planner's unreviewed work, and a merge on top of
     it mixes their changes into yours. (The task files and `.worktrees/` are gitignored, so
     the queue itself never trips this.) Append the output to the Log,
     `mv .queue/active/<file> .queue/blocked/<file>` with "merge held: main folder not
     clean", keep the worktree and branch, tell Kevin, and **stop the loop**: every later
     merge would hit the same wall, and later tasks may depend on this one.
   - If the suite run (the **baseline**) is red, `main` was already broken before your merge.
     Do not merge - a red after it would be blamed on your task and reverted. Hold the task
     the same way ("merge held: main red before merge", with the failing test names) and
     **stop the loop**. Keep the baseline's list of failing tests either way.
4. **Merge, always with `--no-ff`:**

   ```bash
   git merge --no-ff task/<TASK-id> -m "Merge <TASK-id>: <one-line target>"
   ```

   `--no-ff` is required. A fast-forward leaves no merge commit, and then step 6 has no
   single commit for `git revert -m 1` to undo.
5. **If the merge stops:**
   - **Conflict** (`git rev-parse -q --verify MERGE_HEAD` prints a sha): `git merge --abort`.
     The folder was clean at step 3, so this restores it exactly. Append the conflicting paths to the Log,
     `mv .queue/active/<file> .queue/blocked/<file>`, keep the worktree and branch, tell
     Kevin, and continue the loop with the next task.
   - **Refused before starting** (that command prints nothing; e.g. "would be overwritten"
     because someone edited the main folder after step 3): nothing was merged and there is nothing
     to abort. Treat it as a held merge (step 3, first bullet) and stop the loop.
6. **Re-run both suites on `main`, from the main folder** (same command as the baseline,
   quiet output included, per "Keep context small").
   This is also the first run of the montelattice-site test, which skips inside a worktree.
   **If anything is red, first run `git status --porcelain` again.** If it prints anything,
   someone edited the main folder during the run, and the red cannot be pinned on your
   merge: do **not** revert. Append both outputs to the Log, leave the merge in place, tell
   Kevin, and stop the loop. If it is still clean, the red is your merge's - undo it with a
   new commit, never by moving `main` back:

   ```bash
   git revert -m 1 --no-edit <merge-sha>
   ```

   Then re-run the suites. If `main` is green again, append the red output **and the revert
   commit's sha** to the Log, `mv .queue/active/<file> .queue/blocked/<file>`, keep the
   worktree and branch, tell Kevin, and continue the loop. The branch alone cannot be merged
   again - its commits are already in `main`'s history - which is why the sha goes in the
   Log: Claim step 5 uses it to re-land the work. If `main` is **still** red after the revert,
   stop the loop and tell Kevin with the output - do not attempt a second fix.
6a. **Regenerate the function index** - still inside the merge lock, and only now that `main`
   is proven green (a reverted merge never reaches this). The duplicate check greps
   `FUNCTION_INDEX.md`, so it has to list what just landed.

   ```bash
   git diff --name-only <merge-sha>^1 <merge-sha> -- '*.py' Code.gs
   ```

   If that prints nothing, skip this step. `Code.gs` is included on purpose: the index also
   lists every CRM action `Code.gs` handles. Otherwise, from the main folder:

   ```bash
   .venv/Scripts/python build_function_index.py
   git status --porcelain
   ```

   - If `git status --porcelain` lists only `FUNCTION_INDEX.md`, commit it by name:
     `git add FUNCTION_INDEX.md && git commit -m "Regenerate FUNCTION_INDEX.md after <TASK-id> merge"`.
     If nothing changed, commit nothing. The first run after a gap also catches up earlier
     drift; the message still names the task that triggered it.
   - If **any other path** shows up, someone edited the main folder: commit nothing, leave the
     file as it is, tell Kevin, and stop the loop - the same as the step 6 dirty case. Never
     stage anything else.
   - If the indexer itself fails, the merge is green and stays. Append the error to the Log,
     tell Kevin, leave `main` clean (at most an unstaged `FUNCTION_INDEX.md`; say so), and stop
     the loop.

   Either stop still finishes the task first: the merge is on `main` and green, so do step 7
   (remove the worktree - never with `--force` here, since `main` may not have the fresh index;
   if it refuses over the index alone, keep the worktree and say so - then move the task to
   `done/` and `rmdir .queue/merge.lock`) before you stop. Otherwise the lock is
   left held and a merged task sits in `active/`.
7. **Green on `main`:** only now remove the worktree and branch, mark the task done, and
   release the lock. Task scopes never include `FUNCTION_INDEX.md`, but a regenerated copy can
   still be left uncommitted in the worktree, so check it first:

   ```bash
   git -C .worktrees/<TASK-id> status --porcelain
   ```

   If the only change is `FUNCTION_INDEX.md`, that is generated output and `main` now has the
   fresh copy, so use `git worktree remove --force`. If anything else is dirty, do not force:
   tell Kevin and keep the worktree.

   ```bash
   git worktree remove .worktrees/<TASK-id> && git branch -d task/<TASK-id>
   mv .queue/active/<file> .queue/done/<file>
   rmdir .queue/merge.lock
   ```

   Removal waits until `main` is proven green, so a reverted merge still has its branch.
   `done/` therefore means merged into `main`; dependency checks (Claim, step 4) rely on it.
8. Report one line - task id, merged / blocked / held, test counts, review findings - and if
   a planner session is running, message it the same line plus anything it got wrong about
   the code. If the task was blocked, that message is how the plan gets fixed - say what you
   actually found, not that it failed. Then go back to **Claim** from the main folder.

**Telling Kevin** means a plain line in your own output, plus the planner message in step 8.
The same shape for every guard: what happened, the task id, and where the work is (branch
`task/<TASK-id>`).

**The only git writes on `main` are three: a `--no-ff` merge, a `git revert`, and the
`FUNCTION_INDEX.md` regeneration commit (step 6a).** Never push.
Never commit there directly, never reset, never stash - the paragraph under "Build in a fresh
subagent" still holds in both folders.

## When the loop ends

Keep claiming until the inbox is empty, or until every task left in it is on this run's
skip list (Claim, step 2) - colliding in scope with `active/`, or waiting on a dependency
that is not in `done/`. A dependency sitting in `blocked/` means its dependents wait; name
them in the final report.

A "TASK-n queued" message from the planner that arrives mid-loop needs no action - the loop
already reads the inbox. The loop also stops early at a held merge (steps 3 and 5), at a
red it cannot pin on its own merge, at a `main` that stays red after a revert (step 6), or at
either stop in step 6a (a stray path in the main folder, or the indexer failing).

**Where you end the turn.** The Stop hook tests the git tree your shell is in. While a build
agent runs, end every turn in the main folder (check `pwd`). End in the
main folder only if `git status --porcelain` is empty there - then the hook tests exactly
what you merged. If the main folder is dirty (every held-merge stop), `cd` into the held
task's worktree before you finish, so the hook tests your work and not Kevin's. If the
hook ever reports red in files outside the scope of the task you just ran, do not edit
them: report the failure and stop.

Final report: each task with its outcome, then what is left in the inbox and why it did
not run.
