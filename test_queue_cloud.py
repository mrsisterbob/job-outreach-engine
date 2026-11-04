import pytest

import queue_cloud as qc

N227 = "TASK-227-a.md"
N228 = "TASK-228-b.md"
EMPTY = {"inbox": [], "active": [], "done": [], "blocked": []}


def bf(**kw):
    d = {k: [] for k in EMPTY}
    d.update(kw)
    return d


def send(numbers, inbox, active=(), branch=None, local=None, btexts=None):
    return qc.plan_send(numbers, list(inbox), list(active), branch or bf(),
                        local if local is not None else {n: "x" for n in inbox}, btexts or {})


def test_parse_forms_and_dedupe():
    assert qc.parse_task_numbers(["227", "TASK-229", "task-227"]) == [227, 229]
    assert qc.parse_task_numbers(["227-229"]) == [227, 228, 229]


@pytest.mark.parametrize("bad", [["abc"], ["0"], [], ["1-30"], ["-5"]])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        qc.parse_task_numbers(bad)


def test_parse_error_names_offender():
    with pytest.raises(ValueError, match="abc"):
        qc.parse_task_numbers(["227", "abc"])


def test_task_file_for_hyphen_rule_none_and_two():
    names = ["TASK-231-x.md", "TASK-23-y.md"]
    assert qc.task_file_for(23, names) == "TASK-23-y.md"
    assert qc.task_file_for(23, ["TASK-231-x.md"]) is None
    with pytest.raises(ValueError, match="TASK-5-a.md"):
        qc.task_file_for(5, ["TASK-5-a.md", "TASK-5-b.md"])


def test_is_held():
    assert qc.is_held("x\n## HELD FOR KEVIN\nwhy")
    assert qc.is_held("HELD FOR KEVIN: wait")
    assert not qc.is_held("this is not HELD FOR KEVIN really")


def test_send_all_refusals_collected():
    held = "## HELD FOR KEVIN\n"
    plan = qc.plan_send(
        [1, 2, 3, 4, 5], ["TASK-2-a.md", "TASK-3-a.md", "TASK-3-b.md", "TASK-4-a.md", "TASK-5-a.md"],
        ["TASK-9-w.md"], bf(done=["TASK-5-a.md"]),
        {"TASK-2-a.md": held, "TASK-4-a.md": "ok", "TASK-5-a.md": "ok"}, {})
    text = "\n".join(plan.refusals)
    assert plan.items == []
    assert len(plan.refusals) == 5
    assert "TASK-9-w.md" in text and "TASK-1 has no file" in text
    assert "TASK-3 matches" in text and "held" in text
    assert "queue_from_cloud.py" in text


@pytest.mark.parametrize("folder", ["done", "blocked", "active"])
def test_send_refuses_if_on_branch(folder):
    plan = send([227], [N227], branch=bf(**{folder: [N227]}))
    assert plan.refusals and folder in plan.refusals[0]


def test_send_statuses():
    plan = send([227, 228, 229], [N227, N228, "TASK-229-c.md"],
                branch=bf(inbox=[N228, "TASK-229-c.md"]),
                local={N227: "a", N228: "same", "TASK-229-c.md": "new"},
                btexts={N228: "same", "TASK-229-c.md": "old"})
    assert plan.refusals == []
    assert plan.items == [(227, N227, "added"), (228, N228, "unchanged"),
                          (229, "TASK-229-c.md", "updated")]
    assert "overwrites" in qc.send_line(*plan.items[2])
    assert qc.send_line(*plan.items[0]) == "TASK-227 added  " + N227


def test_commit_messages():
    def a(n):
        return (n, "x", "added")
    assert qc.commit_message([a(227), a(228), a(229)]) == "Queue: add TASK-227..229 to cloud batch"
    assert qc.commit_message([a(227), a(231)]) == "Queue: add TASK-227, TASK-231 to cloud batch"
    assert qc.commit_message([(227, "x", "updated"), (228, "y", "updated")]) == \
        "Queue: update TASK-227..228 to cloud batch"
    assert qc.commit_message([a(227), (230, "x", "unchanged")]) == "Queue: add TASK-227 to cloud batch"


def test_push_line():
    assert qc.push_line() == "git push origin cloud/queue-batch"


def test_send_refuses_missing_local_text():
    plan = qc.plan_send([227], [N227], [], bf(), {}, {})
    assert plan.items == [] and plan.refusals == ["TASK-227: no text loaded for " + N227]


def test_send_refuses_missing_branch_text():
    plan = qc.plan_send([227], [N227], [], bf(inbox=[N227]), {N227: "a"}, {})
    assert len(plan.refusals) == 1 and N227 in plan.refusals[0] and plan.items == []


def test_block_reason_whole_word():
    t = "## Log\n- BLOCKED by worker: needs API key\n- unblocked dependency, retrying\n"
    assert qc.block_reason_line(t) == "needs API key"


def test_block_reason_last_bullet():
    t = "## Log\n- a BLOCKED by worker: first\n- other\n- BLOCKED by worker:  second   reason\n"
    assert qc.block_reason_line(t) == "second reason"


def test_block_reason_fallbacks():
    assert qc.block_reason_line("## Log\n- did a\n- did b\n") == "- did b"
    assert qc.block_reason_line("## Log\n\n") == "(no reason in the Log)"
    assert qc.block_reason_line("## Target\nx\n") == "(no reason in the Log)"
    assert len(qc.block_reason_line("## Log\n- BLOCKED: " + "w" * 300)) == 160


def pull(bfiles, texts, local=None, merged=()):
    st = dict(EMPTY, texts={})
    st.update(local or {})
    return qc.plan_pull(bfiles, texts, st, set(merged))


D = "TASK-205-d.md"
B = "TASK-206-b.md"


def test_pull_rule1_in_flight():
    p = pull(bf(done=[D]), {("done", D): "t"}, {"active": [D]}, merged=[205])
    assert p.actions[0][3] is None and p.actions[0][4] == []
    assert "in flight locally, left alone" in p.notes[0]


def test_pull_rule2_unmerged_done():
    p = pull(bf(done=[D]), {("done", D): "t"}, {"inbox": [D]})
    a = p.actions[0]
    assert a[3] is None and a[4] == []
    assert "not merged into main yet" in p.notes[0]


def test_pull_rule3_already_local():
    st = {"done": [D], "inbox": [D], "texts": {("done", D): "t"}}
    p = pull(bf(done=[D]), {("done", D): "t"}, st, merged=[205])
    a = p.actions[0]
    assert a[3] is None and a[4] == [D]
    assert "already local" in a[5]


def test_pull_rule4_conflict():
    st = {"blocked": [D], "texts": {("blocked", D): "other"}}
    p = pull(bf(done=[D]), {("done", D): "t"}, st, merged=[205])
    a = p.actions[0]
    assert a[3] is None and a[4] == []
    assert "conflict" in a[5] and "resolve" in p.notes[0]


def test_pull_rule5_copy_and_remove_stale():
    p = pull(bf(done=[D], blocked=[B]),
             {("done", D): "t", ("blocked", B): "## Log\n- BLOCKED by worker: nope\n"},
             {"inbox": ["TASK-206-old.md"]}, merged=[205])
    assert p.actions[0] == (205, D, "done", "done", [], "TASK-205 done")
    n, name, final, copy_to, rm, line = p.actions[1]
    assert (copy_to, rm) == ("blocked", ["TASK-206-old.md"])
    assert line == "TASK-206 blocked: nope"


def test_pull_ignores_branch_inbox_and_active():
    p = pull(bf(inbox=[D], active=[B]), {})
    assert p.actions == []


def test_pull_done_copied_once_merged():
    assert pull(bf(done=[D]), {("done", D): "t"}).actions[0][3] is None
    assert pull(bf(done=[D]), {("done", D): "t"}, merged=[205]).actions[0][3] == "done"
