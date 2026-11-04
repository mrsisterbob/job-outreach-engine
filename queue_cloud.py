"""Pure decisions for the cloud-branch queue scripts (no I/O, no git, no main import).

scripts/queue_to_cloud.py and scripts/queue_from_cloud.py do the git and file work;
every "may this be sent / where does this go back" decision lives here so it can be
tested without a repository.
"""
import re
from collections import namedtuple

BRANCH = "cloud/queue-batch"
REMOTE_BRANCH = "origin/cloud/queue-batch"
HELD_MARKER = "HELD FOR KEVIN"

MAX_RANGE = 20

SendPlan = namedtuple("SendPlan", "refusals items summary")
PullPlan = namedtuple("PullPlan", "actions notes")

_ARG_RE = re.compile(r"^(?:task-)?(\d+)(?:-(\d+))?$", re.IGNORECASE)


def parse_task_numbers(args):
    """Sorted unique ints from '227', 'TASK-227', 'task-227' or a range '227-229'."""
    if isinstance(args, str):
        args = [args]
    args = list(args or [])
    if not args:
        raise ValueError("no task numbers given")
    found = set()
    for raw in args:
        text = str(raw).strip()
        m = _ARG_RE.match(text)
        if not m:
            raise ValueError("not a task number: %r" % text)
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) is not None else lo
        if lo <= 0 or hi <= 0:
            raise ValueError("task numbers must be positive: %r" % text)
        if hi < lo:
            raise ValueError("backwards range: %r" % text)
        if hi - lo + 1 > MAX_RANGE:
            raise ValueError("range wider than %d: %r" % (MAX_RANGE, text))
        found.update(range(lo, hi + 1))
    return sorted(found)


def _matches(number, names):
    # Same rule as queue_guard_common.active_task_files: prefix TASK-<n>- with the
    # trailing hyphen, so TASK-23 never matches TASK-231. Reimplemented on purpose
    # to avoid importing from .claude/hooks.
    p = ("TASK-%d-" % int(number)).upper()
    return sorted(n for n in names if n.upper().startswith(p) and n.lower().endswith(".md"))


def task_file_for(number, names):
    """The one file named TASK-<number>-*.md, None when none, ValueError when several."""
    hits = _matches(number, names)
    if not hits:
        return None
    if len(hits) > 1:
        raise ValueError("TASK-%d matches more than one file: %s" % (number, ", ".join(hits)))
    return hits[0]


_HELD_RE = re.compile(r"^\s*(?:#+\s*)?" + re.escape(HELD_MARKER))


def is_held(text):
    """True when HELD FOR KEVIN is a heading or starts a line; mid-sentence does not count."""
    return any(_HELD_RE.match(line) for line in (text or "").splitlines())


def plan_send(numbers, local_inbox, local_active, branch_files, local_texts, branch_inbox_texts):
    refusals = []
    items = []
    if local_active:
        refusals.append("local active/ is not empty (a worker is mid-task): %s"
                        % ", ".join(sorted(local_active)))
    for n in numbers:
        try:
            name = task_file_for(n, local_inbox)
        except ValueError as e:
            refusals.append(str(e))
            continue
        if name is None:
            refusals.append("TASK-%d has no file in local inbox/" % n)
            continue
        if name not in local_texts:
            refusals.append("TASK-%d: no text loaded for %s" % (n, name))
            continue
        if is_held(local_texts[name]):
            refusals.append("TASK-%d is held (%s): %s" % (n, HELD_MARKER, name))
            continue
        placed = [f for f in ("done", "blocked", "active")
                  if _matches(n, branch_files.get(f, []))]
        if placed:
            refusals.append("TASK-%d is already in the branch %s/: pull it back with "
                            "scripts/queue_from_cloud.py, do not resend" % (n, placed[0]))
            continue
        bnames = _matches(n, branch_files.get("inbox", []))
        missing = [b for b in bnames if b not in branch_inbox_texts]
        if missing:
            refusals.append("TASK-%d: no text loaded for branch inbox/%s" % (n, missing[0]))
            continue
        if not bnames:
            status = "added"
        elif bnames == [name] and branch_inbox_texts[name] == local_texts[name]:
            status = "unchanged"
        else:
            status = "updated"
        items.append((n, name, status))
    if refusals:
        return SendPlan(refusals, [], "refused: %d problem(s), nothing sent" % len(refusals))
    counts = {s: sum(1 for i in items if i[2] == s) for s in ("added", "updated", "unchanged")}
    summary = "%d task(s): %d added, %d updated, %d unchanged" % (
        len(items), counts["added"], counts["updated"], counts["unchanged"])
    return SendPlan([], items, summary)


def send_line(number, name, status):
    line = "TASK-%d %s  %s" % (number, status, name)
    if status == "updated":
        line += "  (overwrites the branch inbox copy)"
    return line


def commit_message(items):
    named = [i for i in items if i[2] in ("added", "updated")]
    verb = "update" if named and all(i[2] == "updated" for i in named) else "add"
    nums = sorted(i[0] for i in named)
    if len(nums) >= 2 and nums[-1] - nums[0] == len(nums) - 1:
        label = "TASK-%d..%d" % (nums[0], nums[-1])
    else:
        label = ", ".join("TASK-%d" % n for n in nums)
    return "Queue: %s %s to cloud batch" % (verb, label)


def push_line():
    return "git push origin cloud/queue-batch"


def _squash(s):
    return re.sub(r"\s+", " ", s).strip()


def block_reason_line(text):
    """First line of why a task was blocked, from the last BLOCKED bullet in its Log."""
    log = []
    in_log = False
    for line in (text or "").splitlines():
        if re.match(r"^##\s+Log\b", line, re.IGNORECASE):
            in_log = True
            continue
        if in_log and re.match(r"^##\s", line):
            break
        if in_log:
            log.append(line)
    lines = [ln for ln in log if ln.strip()]
    if not lines:
        return "(no reason in the Log)"
    for line in reversed(lines):
        m = re.search(r"\bblocked\b", line, re.IGNORECASE)
        if m and re.match(r"^\s*[-*]", line):
            rest = re.sub(r"^\s*(?:by worker\s*)?:?\s*", "", line[m.end():], flags=re.IGNORECASE)
            return _squash(rest)[:160]
    return _squash(lines[-1])[:160]


def plan_pull(branch_files, branch_texts, local_state, merged_into_main):
    texts = local_state.get("texts", {})
    actions = []
    notes = []
    tasks = []
    for folder in ("done", "blocked"):
        for name in branch_files.get(folder, []):
            m = re.match(r"^TASK-(\d+)-", name, re.IGNORECASE)
            if m and name.lower().endswith(".md"):
                tasks.append((int(m.group(1)), folder, name))
    for number, final, name in sorted(tasks):
        label = "TASK-%d" % number
        text = branch_texts.get((final, name), "")
        if final == "blocked":
            base = "%s blocked: %s" % (label, block_reason_line(text))
        else:
            base = "%s done" % label

        def act(copy_to, remove_from, suffix=None, note=None):
            line = base + (" (%s)" % suffix if suffix else "")
            actions.append((number, name, final, copy_to, remove_from, line))
            if note:
                notes.append("%s: %s" % (label, note))

        if _matches(number, local_state.get("active", [])):
            act(None, [], "in flight locally", "in flight locally, left alone")
            continue
        if final == "done" and number not in merged_into_main:
            act(None, [], "not merged into main yet",
                "done on the cloud branch but not merged into main yet: "
                "merge origin/cloud/queue-batch, then run again")
            continue
        stale = _matches(number, local_state.get("inbox", []))
        if name in local_state.get(final, []) and texts.get((final, name)) == text:
            act(None, stale, "already local", "already local")
            continue
        other = "blocked" if final == "done" else "done"
        if _matches(number, local_state.get(other, [])):
            act(None, [], "conflict",
                "local %s/ holds this task but the branch has it in %s/: resolve by hand"
                % (other, final))
            continue
        act(final, stale)
    return PullPlan(actions, notes)
