Show the current state of the task queue.

Run:

```bash
for d in active inbox blocked done; do
  echo "== $d =="
  ls -1 .queue/$d/*.md 2>/dev/null | sed 's|.*/||' || true
done
```

For everything in `active/` and `blocked/`, read the file and report its `scope` and the
last entry in its `## Log`.

Then give a one-line summary: how many queued, in flight, blocked, done. If anything in
`active/` has a scope that overlaps another active task, flag it - that should be
impossible and means a task was claimed by hand rather than through `/queue-run`.

Report only. Change nothing.
