import warm_stage_samples as w


def _t(n):
    return "T" * n


def test_warm_stage_samples_cap_total_counts_all():
    s = w.StageSampler()
    for i in range(30):
        s.note("title_filter", "Co%d" % i, "Role %d" % i, "r")
    snap = s.snapshot()["title_filter"]
    assert snap["total"] == 30 and len(snap["samples"]) == 10


def test_warm_stage_samples_per_company_cap():
    s = w.StageSampler()
    for i in range(6):
        s.note("region", "Acme", "Role %d" % i)
    snap = s.snapshot()["region"]
    assert snap["total"] == 6 and len(snap["samples"]) == 2


def test_warm_stage_samples_interleaved_companies_keep_both():
    s = w.StageSampler()
    for i in range(4):
        s.note("remote", "A", "a%d" % i)
        s.note("remote", "B", "b%d" % i)
    cos = [x["company"] for x in s.snapshot()["remote"]["samples"]]
    assert cos.count("A") == 2 and cos.count("B") == 2


def test_warm_stage_samples_unknown_stage_ignored():
    s = w.StageSampler()
    s.note("nope", "A", "x")
    assert all(v["total"] == 0 for v in s.snapshot().values())


def test_warm_stage_samples_snapshot_lists_all_stages_in_order():
    snap = w.StageSampler().snapshot()
    assert list(snap) == [k for k, _ in w.STAGES] and len(snap) == 7
    assert snap["card"] == {"total": 0, "samples": []}


def test_warm_stage_samples_deterministic():
    def run():
        s = w.StageSampler()
        for i in range(25):
            s.note("michigan_screen", "C%d" % (i % 4), "R%d" % i, "city_allowlist")
        return s.snapshot()
    assert run() == run()


def test_warm_stage_samples_truncates_and_none():
    s = w.StageSampler()
    s.note("card", None, _t(200), None)
    x = s.snapshot()["card"]["samples"][0]
    assert x == {"company": "", "title": "T" * 80, "detail": ""}


def test_warm_stage_samples_report_escapes():
    s = w.StageSampler()
    s.note("title_filter", "A&B", "<script>x</script> & y", "why")
    rep = w.format_stage_report(s.snapshot(), "run 1")
    assert "<script>" not in rep and "&lt;script&gt;" in rep and "A&amp;B" in rep


def test_warm_stage_samples_report_limit_and_trim_marker():
    s = w.StageSampler()
    for st, _ in w.STAGES:
        for i in range(10):
            s.note(st, "Company%d" % i + "C" * 60, _t(80), "d" * 40)
    rep = w.format_stage_report(s.snapshot(), "run")
    assert len(rep) <= 3500 and "(samples trimmed)" in rep
    for _, label in w.STAGES:
        assert label in rep


def test_warm_stage_samples_small_report_not_trimmed_and_zero_stage():
    s = w.StageSampler()
    s.note("card", "A", "Dev")
    rep = w.format_stage_report(s.snapshot(), "run")
    assert "trimmed" not in rep and "A - Dev" in rep and ": 0" in rep


def test_warm_stage_samples_malformed_snapshot():
    assert w.format_stage_report({"pulled": 3}, "x") == "no stage data"
    assert w.format_stage_report(None, "x") == "no stage data"


def test_warm_stage_samples_snapshot_is_empty():
    s = w.StageSampler()
    assert w.snapshot_is_empty(s.snapshot())
    s.note("card", "A", "B")
    assert not w.snapshot_is_empty(s.snapshot())
