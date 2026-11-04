"""Put chosen inbox task files onto the local branch cloud/queue-batch as ONE commit.

  python scripts/queue_to_cloud.py 227 228 229

Never pushes: prints the single push line for Kevin to run. All decisions (parsing,
refusals, statuses, commit message, push line) come from queue_cloud; this file only
does git and file copies. Exit 0 ok / nothing to commit, 1 failure, 2 refused.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import queue_cloud  # noqa: E402

WT_REL = ".worktrees/queue-to-cloud"
FOLDERS = ("inbox", "active", "done", "blocked")


def run_git(args, cwd):
    """Seam: tests replace this to force a failure part-way."""
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"] + list(args), cwd=str(cwd), capture_output=True, check=False)


def _out(p):
    return p.stdout.decode("utf-8", "replace").strip()


def _git_ok(args, cwd):
    p = run_git(args, cwd)
    if p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.decode("utf-8", "replace").strip()))
    return p


def _ls_local(folder):
    return sorted(p.name for p in folder.glob("*.md")) if folder.is_dir() else []


def _branch_names(ref, folder, cwd):
    p = run_git(["ls-tree", "--name-only", ref, "--", ".queue/%s/" % folder], cwd)
    if p.returncode != 0:
        return []
    return sorted(Path(line).name for line in _out(p).splitlines() if line.lower().endswith(".md"))


def _branch_text(ref, folder, name, cwd):
    p = run_git(["show", "%s:.queue/%s/%s" % (ref, folder, name)], cwd)
    return p.stdout.decode("utf-8") if p.returncode == 0 else None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path.cwd()
    try:
        numbers = queue_cloud.parse_task_numbers(argv)
    except ValueError as e:
        print("refused: %s" % e)
        return 2
    br, rbr = queue_cloud.BRANCH, queue_cloud.REMOTE_BRANCH

    if run_git(["fetch", "origin", br], root).returncode != 0:
        print("fetch of origin %s failed; nothing changed" % br)
        return 1

    # Prefer the local branch as the picture of "what is on the branch" when it already
    # contains origin's tip, so a second run sees the first run's unpushed commit.
    ref = rbr
    if run_git(["rev-parse", "--verify", "-q", "refs/heads/" + br], root).returncode == 0:
        if run_git(["merge-base", "--is-ancestor", rbr, "refs/heads/" + br], root).returncode == 0:
            ref = "refs/heads/" + br

    inbox_dir, active_dir = root / ".queue" / "inbox", root / ".queue" / "active"
    local_inbox, local_active = _ls_local(inbox_dir), _ls_local(active_dir)
    local_texts = {n: (inbox_dir / n).read_bytes().decode("utf-8") for n in local_inbox}
    branch_files = {f: _branch_names(ref, f, root) for f in FOLDERS}
    branch_texts = {}
    for n in branch_files["inbox"]:
        t = _branch_text(ref, "inbox", n, root)
        if t is not None:
            branch_texts[n] = t

    plan = queue_cloud.plan_send(numbers, local_inbox, local_active, branch_files,
                                 local_texts, branch_texts)
    if plan.refusals:
        for r in plan.refusals:
            print("refused: " + r)
        return 2
    lines = [queue_cloud.send_line(*i) for i in plan.items]
    todo = [i for i in plan.items if i[2] in ("added", "updated")]
    if not todo:
        print("\n".join(lines))
        print("nothing to commit")
        return 0

    wt = root / WT_REL
    if wt.exists():
        print("refused: %s already exists (a crashed earlier run). Clear it with: "
              "git worktree remove --force %s" % (WT_REL, WT_REL))
        return 2

    try:
        if run_git(["rev-parse", "--verify", "-q", "refs/heads/" + br], root).returncode == 0:
            _git_ok(["worktree", "add", WT_REL, br], root)
        else:
            _git_ok(["worktree", "add", WT_REL, "-b", br, rbr], root)
        return _send(root, wt, todo, branch_files, lines)
    except RuntimeError as e:
        print("queue_to_cloud: git failed: %s" % e)
        print("nothing was pushed")
        return 1
    finally:
        if wt.exists():
            run_git(["worktree", "remove", "--force", WT_REL], root)
            if wt.exists():
                shutil.rmtree(str(wt), ignore_errors=True)
                run_git(["worktree", "prune"], root)


def _rebase_failed(wt, rbr, made, p=None):
    conflicts = _out(run_git(["diff", "--name-only", "--diff-filter=U"], wt))
    msg = p.stderr.decode("utf-8", "replace").strip() if p is not None else ""
    run_git(["rebase", "--abort"], wt)
    if made:
        print("commit %s" % made)
    if conflicts:
        print("rebase onto %s conflicted; commit left where it is. Conflicting paths:\n%s"
              % (rbr, conflicts))
    else:
        print("rebase failed: %s" % msg)
    print("nothing was pushed")


def _send(root, wt, todo, branch_files, lines):
    br, rbr = queue_cloud.BRANCH, queue_cloud.REMOTE_BRANCH
    if run_git(["rebase", rbr], wt).returncode != 0:
        _rebase_failed(wt, rbr, None)
        return 1
    dest = wt / ".queue" / "inbox"
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, name, _status in todo:
        for old in queue_cloud._matches(n, branch_files["inbox"]):
            if old != name and (dest / old).exists():
                _git_ok(["rm", "-q", "-f", "--", ".queue/inbox/" + old], wt)
        (dest / name).write_bytes((root / ".queue" / "inbox" / name).read_bytes())
        paths.append(".queue/inbox/" + name)
    _git_ok(["add", "-f", "--"] + paths, wt)
    staged = set(_out(_git_ok(["diff", "--cached", "--name-only"], wt)).splitlines())
    missing = [p for p in paths if p not in staged]
    if missing:
        raise RuntimeError("not staged (gitignored?): " + ", ".join(missing))
    _git_ok(["commit", "-q", "-m", queue_cloud.commit_message(todo)], wt)

    made = _out(run_git(["rev-parse", "HEAD"], wt))
    if run_git(["fetch", "origin", br], wt).returncode != 0:
        print("commit %s" % made)
        print("re-fetch failed; commit left on %s, not rebased" % br)
        return 1
    if run_git(["merge-base", "--is-ancestor", rbr, "HEAD"], wt).returncode != 0:
        p = run_git(["rebase", rbr], wt)
        if p.returncode != 0:
            _rebase_failed(wt, rbr, made, p)
            return 1
    head = _out(_git_ok(["rev-parse", "HEAD"], wt))
    print("\n".join(lines))
    print("commit %s" % head)
    print("run this yourself: %s" % queue_cloud.push_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
