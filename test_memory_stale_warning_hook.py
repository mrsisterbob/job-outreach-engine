"""Tests for .claude/hooks/memory_stale_warning.py (SessionStart). Subprocess + real payload."""
import json
import os
import subprocess
import sys

import pytest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude", "hooks", "memory_stale_warning.py")
PAYLOAD = json.dumps({"session_id": "s", "transcript_path": "t.jsonl", "cwd": ".",
                      "hook_event_name": "SessionStart", "source": "startup"})
REAL_DIR = "C:/Users/kjmil/.claude/projects/C--Users-kjmil-job-outreach-engine/memory/"


def run(folder, stdin=PAYLOAD):
    env = dict(os.environ, MEMORY_STALE_DIR=str(folder))
    r = subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True, text=True, env=env, timeout=30)
    return r.returncode, r.stdout


def mem(folder, name, date=None, nested=False):
    if date is None:
        fm = "name: x\n"
    elif nested:
        fm = "name: x\nmetadata:\n  node_type: memory\n  stale_after: %s\n" % date
    else:
        fm = "name: x\nstale_after: %s\n" % date
    (folder / (name + ".md")).write_text("---\n" + fm + "---\n\nbody\n", encoding="utf-8")


def test_past_date_listed_valid_json(tmp_path):
    mem(tmp_path, "old", "2000-01-01")
    rc, out = run(tmp_path)
    assert rc == 0
    d = json.loads(out)
    assert "old (2000-01-01)" in d["systemMessage"]
    assert d["systemMessage"].startswith("Memory past stale_after (1): ")
    h = d["hookSpecificOutput"]
    assert h["hookEventName"] == "SessionStart" and "old (2000-01-01)" in h["additionalContext"]


def test_future_date_not_listed(tmp_path):
    mem(tmp_path, "fresh", "2999-12-31")
    assert run(tmp_path) == (0, "")


def test_no_field_prints_nothing(tmp_path):
    mem(tmp_path, "plain")
    assert run(tmp_path) == (0, "")


def test_all_fresh_prints_nothing(tmp_path):
    mem(tmp_path, "a", "2999-12-31")
    mem(tmp_path, "b")
    assert run(tmp_path) == (0, "")


def test_two_stale_one_line_oldest_first(tmp_path):
    mem(tmp_path, "newer", "2001-05-05")
    mem(tmp_path, "older", "2000-01-01")
    rc, out = run(tmp_path)
    msg = json.loads(out)["systemMessage"]
    assert "\n" not in msg and "(2): " in msg
    assert msg.index("older") < msg.index("newer")


def test_malformed_date_skipped_others_listed(tmp_path):
    mem(tmp_path, "bad1", "2026-13-40")
    mem(tmp_path, "bad2", "soon")
    mem(tmp_path, "good", "2000-01-01")
    rc, out = run(tmp_path)
    msg = json.loads(out)["systemMessage"]
    assert rc == 0 and "good" in msg and "bad1" not in msg and "bad2" not in msg


def test_memory_index_ignored(tmp_path):
    mem(tmp_path, "MEMORY", "2000-01-01")
    assert run(tmp_path) == (0, "")


def test_missing_folder_and_garbage_stdin(tmp_path):
    assert run(tmp_path / "nope") == (0, "")
    mem(tmp_path, "old", "2000-01-01")
    rc, out = run(tmp_path, stdin="not json {{{")
    assert rc == 0  # fails open; output is optional but must not crash


def test_field_nested_under_metadata_read(tmp_path):
    mem(tmp_path, "nested", "2000-01-01", nested=True)
    assert "nested" in json.loads(run(tmp_path)[1])["systemMessage"]


def test_field_after_frontmatter_ignored(tmp_path):
    (tmp_path / "late.md").write_text("---\nname: x\n---\n\nstale_after: 2000-01-01\n", encoding="utf-8")
    assert run(tmp_path) == (0, "")


def test_real_memory_files_have_valid_stale_after():
    import datetime
    import re
    if not os.path.isdir(REAL_DIR):
        pytest.skip("memory folder absent on this machine")
    for n in ("repo-size-snapshot", "pending-deploys", "render-deploy-facts"):
        text = open(REAL_DIR + n + ".md", encoding="utf-8").read().split("---")[1]
        m = re.search(r"^\s*stale_after:\s*(\d{4}-\d{2}-\d{2})\s*$", text, re.M)
        assert m, n
        datetime.date.fromisoformat(m.group(1))


def test_bom_prefixed_file_is_flagged(tmp_path):
    (tmp_path / "bom.md").write_bytes(b"\xef\xbb\xbf---\nname: x\nstale_after: 2000-01-01\n---\n\nbody\n")
    assert "bom (2000-01-01)" in json.loads(run(tmp_path)[1])["systemMessage"]
