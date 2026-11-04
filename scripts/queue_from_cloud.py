"""Pull finished cloud tasks back into the local .queue/ (read-only against git).

  python scripts/queue_from_cloud.py [--dry-run]

Fetches origin/cloud/queue-batch; for every task whose final file sits in done/ or
blocked/ on that branch, copies the file (Log included) into the matching local
.queue/ folder and removes the stale .queue/inbox/ copy. A cloud "done" only moves
once its merge commit is an ancestor of local main. All decisions come from
queue_cloud.plan_pull; this file does the git reads and the file moves.
Exit 0 pull ran (notes allowed), 1 fetch failed or a write did not verify.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import queue_cloud  # noqa: E402

FOLDERS = ("inbox", "active", "done", "blocked")
MERGE_HINT = ("merge origin/cloud/queue-batch into main by hand (--no-ff), "
              "regenerate FUNCTION_INDEX.md, push, then run this again")


def run_git(args, cwd):
    """Seam: tests may replace this to force a failure."""
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"] + list(args),
                          cwd=str(cwd), capture_output=True, check=False)


def _out(p):
    return p.stdout.decode("utf-8", "replace").strip()


def _ls_local(folder):
    return sorted(p.name for p in folder.glob("*.md")) if folder.is_dir() else []


def _branch_files(ref, cwd):
    """{folder: [names]} for .queue/<folder>/*.md on the branch. RuntimeError on git failure."""
    p = run_git(["ls-tree", "-r", "--name-only", ref, "--", ".queue"], cwd)
    if p.returncode != 0:
        raise RuntimeError("git ls-tree failed: " + p.stderr.decode("utf-8", "replace").strip())
    found = {f: [] for f in FOLDERS}
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        parts = line.strip().split("/")
        if len(parts) == 3 and parts[0] == ".queue" and parts[1] in found and parts[2].lower().endswith(".md"):
            found[parts[1]].append(parts[2])
    return {f: sorted(v) for f, v in found.items()}


def _branch_bytes(ref, folder, name, cwd):
    p = run_git(["show", "%s:.queue/%s/%s" % (ref, folder, name)], cwd)
    if p.returncode != 0:
        raise RuntimeError("git show failed for .queue/%s/%s: %s"
                           % (folder, name, p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout


def _merged_numbers(ref, done_names, cwd):
    merged = set()
    for name in done_names:
        m = re.match(r"^TASK-(\d+)-", name, re.IGNORECASE)
        if not m:
            continue
        n = int(m.group(1))
        p = run_git(["log", ref, "--merges", "--grep", "^Merge TASK-%d:" % n, "--format=%H"], cwd)
        if p.returncode != 0:
            raise RuntimeError("could not tell whether TASK-%d is merged into main: %s"
                               % (n, p.stderr.decode("utf-8", "replace").strip()))
        for sha in _out(p).split():
            a = run_git(["merge-base", "--is-ancestor", sha, "main"], cwd)
            if a.returncode == 0:
                merged.add(n)
                break
            if a.returncode != 1:
                raise RuntimeError("could not tell whether TASK-%d is merged into main: %s"
                                   % (n, a.stderr.decode("utf-8", "replace").strip()))
    return merged


def _write_atomic(dest_dir, name, data):
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pull-", suffix=".tmp", dir=str(dest_dir))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, str(dest_dir / name))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return (dest_dir / name).read_bytes() == data


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    root = Path.cwd()
    rbr = queue_cloud.REMOTE_BRANCH

    if run_git(["fetch", "origin", queue_cloud.BRANCH], root).returncode != 0:
        print("fetch of origin %s failed; nothing changed" % queue_cloud.BRANCH)
        return 1

    qdir = root / ".queue"
    try:
        files = _branch_files(rbr, root)
        wanted = {"done": files["done"], "blocked": files["blocked"]}
        raw = {(f, n): _branch_bytes(rbr, f, n, root) for f in wanted for n in wanted[f]}
        merged = _merged_numbers(rbr, wanted["done"], root)
    except RuntimeError as e:
        print("queue_from_cloud: %s" % e)
        print("nothing changed")
        return 1

    local = {f: _ls_local(qdir / f) for f in FOLDERS}
    local_texts = {}
    for f in ("done", "blocked"):
        for n in local[f]:
            local_texts[(f, n)] = (qdir / f / n).read_bytes().decode("utf-8", "replace")
    branch_texts = {k: v.decode("utf-8", "replace") for k, v in raw.items()}
    state = {"inbox": local["inbox"], "active": local["active"], "done": local["done"],
             "blocked": local["blocked"], "texts": local_texts}

    plan = queue_cloud.plan_pull(files, branch_texts, state, merged)
    failed = False
    moved = 0
    lines = []
    for number, name, final, copy_to, remove_from, line in plan.actions:
        if copy_to and (qdir / copy_to / name).exists() \
                and (qdir / copy_to / name).read_bytes() != raw[(final, name)]:
            lines.append("TASK-%d conflict: a different local %s/%s exists, left alone" % (number, copy_to, name))
            continue
        if dry:
            lines.append(line)
            if copy_to:
                moved += 1
            continue
        try:
            if copy_to and not _write_atomic(qdir / copy_to, name, raw[(final, name)]):
                lines.append("TASK-%d write did not verify; nothing deleted" % number)
                failed = True
                continue
            for old in remove_from:
                p = qdir / "inbox" / old
                if p.exists():
                    p.unlink()
        except OSError as e:
            lines.append("TASK-%d write failed: %s" % (number, e))
            failed = True
            continue
        if copy_to:
            moved += 1
        lines.append(line)

    print("\n".join(lines) if lines else "nothing to pull")
    print("%d task(s) %s, %d line(s) total%s" % (moved, "would move" if dry else "moved",
                                                 len(plan.actions), " (dry run)" if dry else ""))
    if any("not merged into main yet" in a[5] for a in plan.actions):
        print(MERGE_HINT)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
