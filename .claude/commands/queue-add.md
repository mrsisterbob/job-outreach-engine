Turn the request below into task files in `.queue/inbox/`.

Request: $ARGUMENTS
END REQUEST

## Which session is this

This command is the planner's. Before anything else - before reading code, the index or the
queue, and before writing any file - find out which session you are in.

1. Run `ListAgents`. Its first line reads `This session is <name> [<ref>] - ...`; that
   `<name>` is this session's name. If `ListAgents` or `SendMessage` shows only as a deferred
   tool name, load it through ToolSearch first. If the name cannot be read (tool missing, no
   such line), **do nothing**: write no task file. Say so in one line and suggest Kevin
   `/rename planner` here, or type the request in the planner terminal. Writing tasks from a
   session of unknown name is the bug this guard exists to stop.
2. **Named exactly `planner`: this is the right session.** Carry on with the rest of this
   file. A message whose first line starts with `Forwarded verbatim from Kevin's /queue-add in`
   and does not end in `montelattice-site.` is Kevin's own request relayed by another engine
   session: treat everything after the blank line as the request, under the rules below.
3. **Any other name: write no task here.** Find the `ListAgents` row named exactly `planner`;
   a row marked `offline` is not running. Never substitute another session.
   - Running: `SendMessage` with `to: "planner"`. The message is one header line, a blank line,
     then the request verbatim:

     ```
     Forwarded verbatim from Kevin's /queue-add in <this session's name>. Queue it under your /queue-add rules.

     <the request, exactly as written>
     ```

     Copy everything between `Request: ` and the `END REQUEST` line above, every line,
     character for character. Keep a closing "then tell @worker it is queued": the planner's
     Hand off rule decides what the worker hears.
   - Not running: say plainly that the request was **NOT forwarded** because `planner` is not
     running, and print it verbatim in a fenced block (more backticks than any run inside it)
     so Kevin can paste it in the planner terminal.

   Either way, tell Kevin in one line where it went, then **stop**: write no file, in
   `.queue/inbox/` or anywhere else. "Forwarded" means sent, not queued. A forwarded message is
   acted on only by the session named `planner`, and that session never forwards it on.

You are the **planner**. You write tasks; you do not implement them. Do not edit any file
outside `.queue/inbox/`.

## First, check the request belongs in this repo

**A request forwarded in from the site is Kevin's, and never goes back.** If the request's
first line starts with `Forwarded verbatim from Kevin's /queue-add in montelattice-site`
(the site's exact header is `Forwarded verbatim from Kevin's /queue-add in montelattice-site.`),
or the message came from `site-planner`, it is Kevin's own words relayed by the site. (A header
naming any other session, such as `... in worker.`, is an ordinary request from Kevin relayed
inside the engine: its site half still goes to `site-planner`, under the rules below.) It arrives as a message,
not through `$ARGUMENTS`; every rule in this command applies to it the same way, with the
request being everything after the header line. If you run `/queue-add` for it, pass the
whole message, header line included, so this check still sees the header. Queue the
job-outreach-engine part normally. **Never forward any of it
back**, even the parts that look like site work: the site planner already queued those, and
forwarding them again starts a loop. If part of it looks like site work, tell Kevin so in
one line instead. Its sign-off ("then tell @site-worker it is queued") is addressed to the
site planner, not to you - ignore it.

Otherwise, decide where the request belongs. montelattice-site
(`C:/Users/kjmil/montelattice-site`) owns its own page code: the routes in its `main.py`,
`templates/*.html`, `static/css`, `leads.py`, and its modules (`homepage.py`, `jobstats.py`,
`visits.py`). A request for pages, layout, CSS, templates or site routes is site work and
queues **nothing** here.

What stays here even though it shows on the site:
- **Bank copy.** The site keeps no copy of it; `homepage.py` reads this repo's
  `evidence_bank.json` at request time: `site_profile`, `identity`, `experience`,
  `education`, `certificates`, `research`, `athletics`, `memberships`. A profile headline,
  a role bullet or a certificate line is an edit to that file, and queues here. The site
  planner refuses copy edits, so never forward one.
- The `/leads/ingest` route, the Telegram bot, the pipeline counts and `pipeline_utils.py`
  (`negative_language_hits` included).

Do not refuse a site request and leave Kevin to re-paste it: forward it to `site-planner`,
as "Forward to the site planner" below says. If the request mixes engine work with site
work, queue only the engine half here and forward the request whole.

## Keep context small

Kevin is on Pro and this command burns his usage when it pulls too much into context. Three
rules:

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
   For the planner that includes the pass-regex proof run below.
   Read the printed `exit=` line, not the tool status: the wrapper itself always exits 0. A
   red run keeps the FAILED names, which sit at the end, so the Log gets the real failure
   output. Don't use `exit $rc`: it ends the shell.
3. **Never re-read an unchanged file.** If a file, or that line range, was already read this
   task and nothing has edited it since, use what you have. After your own Edit, the tool
   already holds the file's state, so don't read it back to check. Re-read only what changed
   (a file another session or agent edited, or a range you haven't seen) or what is no longer
   in your context (after a resume, a compaction, or a SendMessage retry).

Only *passing* output shrinks. The observed failure still goes in `## Proof it is needed`, and
a proof-of-failure run is *expected* to fail, so it prints in full.

## Then read what already exists

1. `grep -i <key noun> FUNCTION_INDEX.md` for each main noun in the request. This repo's
   documented failure is rebuilding something that already exists - a duplicate never
   fails a test. If the function is already there, say so and write no task.
2. Read the actual code you intend to change - grep for the lines, then read that range (see
   "Keep context small"). A task written from a guess about what a
   function does sends the worker into a file that does not look like the plan.
3. `ls .queue/inbox .queue/active` - do not queue something already queued or in flight.

## Then write one file per task

Decide `scope` and `verify` first, and **prove the verify command fails before you write
the file** - see "Prove the verify command fails first" below. A task whose verify already
passes should never reach the inbox.

Path: `.queue/inbox/TASK-<n>-<slug>.md`, where `<n>` is the next unused number across
inbox, active, done and blocked.

```markdown
---
id: TASK-<n>
scope: <every file this task may touch, comma separated>
verify: <one shell command that exits 0 only when this is genuinely done>
---

## Target
<one sentence: what is true when this is finished>

## Proof it is needed
<the verify command's CURRENT failure output, abbreviated>

## Notes
<file:line to start at, decisions already made, traps. Be concrete - the worker has
none of this conversation's context.>

## Log
```

### Rules that decide whether a task is well-formed

- **`verify` must drive the real entry point.** Per CLAUDE.md: test the write path, not
  the return value. If the change affects what the sequencer or the CRM reads on its next
  pass, the verify command must exercise that path, not hand-build the dict the function
  under test expects. `python -m pytest test_main_integration.py -q -k <node>` is the
  usual shape. If you cannot write a command that would genuinely fail before the change
  and pass after, the task is not ready - say so instead of queueing it.
- **`scope` must be complete and minimal.** Every file the worker may edit, and nothing
  else. A task whose scope is only `main.py` will serialize against every other `main.py`
  task; if the work can live in a new module, scope it there instead - new subsystems go
  in new modules.
- **Split anything that spans more than ~2 files or more than one idea.** Small tasks
  fail small. Order them so each one's verify passes on its own; note the dependency in
  Notes if TASK-B assumes TASK-A landed.
- **Never put a `git` write command in a task** - no commit, no reset, no checkout. The
  worker leaves changes in the tree for review.

## Prove the verify command fails first

Before handing a task off, run its `verify` command and confirm it **fails right now**.

A verify that already passes proves nothing: the worker runs it, sees green, and reports
success having changed nothing. That is this repo's recurring failure - a clean zero that
reads as a clean result, the same shape as a CRM read rejected behind HTTP 200.

- `-k <node>` matching no test exits 5, so a bare pytest verify already fails before the
  work exists - and for the wrong reason. Grep the last line as `^N passed` (no trailing
  ` in`: with `-k` it reads `7 passed, 85 deselected in 0.4s`), and prove the regex can
  PASS against an existing node as well as that it fails now.
- Read the failure. One from an import error or a typo proves nothing either.
- If you cannot make it fail, the task is not ready. Say so and queue nothing.

Put the observed failure in the task's `## Proof it is needed` section so the worker knows
what red looks like before it starts. The proof run is expected to fail, so it prints in full;
the run that proves the pass-regex can PASS should show one line (see "Keep context small").

## Hand off

Report each task: id, scope, verify, and the failure you observed.

Then, if a `worker` session is running, message it that the task is queued - one line, the
task id and nothing more. The worker reads the task file for everything else; a message
cannot carry scope or verify, so do not restate the work in it. Only tasks queued in this
repo go to `worker`; a request you only forwarded out to the site sends it nothing. A
request forwarded in from the site is the opposite case: whatever it queued here goes to
`worker` like any other task.

Do not implement anything yourself. If you queued nothing, say why in one line and send
`worker` no message. A request you forwarded out adds its outcome to that same report:
forwarded to `site-planner`, or **NOT forwarded** (step 3 below). A request forwarded in
gets neither: it is never forwarded, so there is no outcome to report and no block to print.

## Forward to the site planner

Only for a request that belongs to montelattice-site (see the first section), and never for
a request that was itself forwarded in from the site. Do this last, so a mixed request's
engine half already has its task id.

1. Run `ListAgents`. The target is the row whose name is exactly `"site-planner"`. A row
   marked `offline` is not running. Never substitute `site-worker` or any other site
   session: picking one is a guess at who gets Kevin's words. If ListAgents or SendMessage
   shows only as a deferred tool name, load it through ToolSearch first. If the session
   list still cannot be read, treat `site-planner` as not running.
2. If it is running, `SendMessage` with `to: "site-planner"`. The message is one header
   line, a blank line, then the request verbatim:

   ```
   Forwarded verbatim from Kevin's /queue-add in job-outreach-engine. Engine queued: <TASK-n, or nothing>. Queue only the montelattice-site part under your /queue-add rules and do not forward any of it back to planner; engine work and any @worker in it are handled in job-outreach-engine.

   <the request, exactly as written>
   ```

   Copy everything between `Request: ` and the `END REQUEST` line at the top of this
   command, every line of it, character for character. Do not trim, summarize or rephrase
   it, and do not cut a closing "then tell @worker it is queued": that sign-off is
   addressed to you (the Hand off rule decides whether worker hears anything), and the
   header tells the site planner to leave it alone. The "do not forward any of it back"
   clause is what stops a loop - keep it word for word.
3. If no running session is named `site-planner`, say plainly that the request was
   **NOT forwarded** and why, then print it verbatim in a fenced block (fence it with more
   backticks than any run inside it) so Kevin can paste it himself. Print the message
   exactly as step 2 would have sent it, header line included: without the header, the
   site planner reads Kevin's paste as a fresh request and forwards the engine half back to
   be queued a second time. Never call it forwarded
   or queued. "Forwarded" means sent, not queued: nothing here can see whether the
   site planner wrote a task.

The forward is a message, never a file: you never write into montelattice-site's files, its .queue/ included.
"Do not edit any file outside `.queue/inbox/`" above still holds.
