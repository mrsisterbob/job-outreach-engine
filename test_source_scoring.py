"""source_scoring tests: made-up companies and boards, tmp_path files, no real sample or truth."""
import importlib.util
from pathlib import Path

import source_scoring as ss
import warm_sample as ws

ROOT = Path(ss.__file__).resolve().parent
KEYS = [f"co{i}" for i in range(6)]  # co0,co1 top; co2,co3 mid; co4,co5 tail
TIERS_OF = ["top", "top", "mid", "mid", "tail", "tail"]


def cli():
    spec = importlib.util.spec_from_file_location("score_cli", ROOT / "scripts" / "score_source.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sample_file(tmp_path):
    rows = [{"company_key": k, "name": k.upper() + " Inc", "tier": t, "contacts": 3,
             "domain": f"{k}.com", "domain_source": "email", "slug": k}
            for k, t in zip(KEYS, TIERS_OF)]
    p = tmp_path / "sample.csv"
    ok, why = ws.write_sample(rows, p, repo_root=tmp_path / "elsewhere")
    assert ok, why
    return p


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


TRUTH = ("company_key,system,token\n"
         "co0,greenhouse,Alpha\n"      # hit
         "co1,lever,beta\n"            # false hit (wrong token, right system)
         "co2,workday,acme/wd5/ext\n"  # miss
         "co3,none,\n"                 # correct negative
         "co4,none,\n"                 # false hit (result names a board)
         "co5,\n")                     # not labelled


def run(tmp_path, result_text, *extra, truth=TRUTH):
    sp = sample_file(tmp_path)
    tp = write(tmp_path, "truth.csv", truth)
    rp = write(tmp_path, "res.csv", result_text)
    return sp, tp, rp


def score_from(tmp_path, result_text, cols=None):
    sp, tp, rp = run(tmp_path, result_text)
    sample = ws.read_sample(sp)
    truth, blank = ss.read_truth(tp)
    answers, unjoined = ss.read_result(rp, cols or ss.Columns(), sample, normalizer=str.lower)
    return ss.score(sample, truth, answers), blank, unjoined


RESULT = ("company_key,system,token\n"
          "co0,GREENHOUSE, alpha \n"
          "co1,lever,gamma\n"
          "co4,ashby,zed\n"
          "co5,lever,x\n"
          "ghost,lever,x\n")


def test_each_verdict_and_unlabelled(tmp_path):
    s, blank, unjoined = score_from(tmp_path, RESULT)
    o = s["overall"]
    assert (o["n"], o[ss.HIT], o[ss.FALSE_HIT], o[ss.MISS], o[ss.CORRECT_NEGATIVE]) == (5, 1, 2, 1, 1)
    assert blank == 1
    assert s["unlabelled"] == 1  # co5: answered, never labelled, never scored
    assert unjoined == 1  # ghost: matches no sample company (the CLI adds it to unlabelled)


def test_wrong_token_right_system_is_false_hit():
    assert ss.verdict(("lever", "beta"), ("lever", "gamma")) == ss.FALSE_HIT
    assert ss.verdict(("lever", "beta"), ("LEVER", " Beta ")) == ss.HIT


def test_none_truth_no_answer_is_correct_negative_not_miss():
    assert ss.verdict(("none", ""), None) == ss.CORRECT_NEGATIVE
    assert ss.verdict(("none", ""), ("lever", "x")) == ss.FALSE_HIT
    assert ss.verdict(("lever", "x"), None) == ss.MISS


def test_per_tier_counts_with_n(tmp_path):
    s, _, _ = score_from(tmp_path, RESULT)
    assert s["top"]["n"] == 2 and s["top"][ss.HIT] == 1 and s["top"][ss.FALSE_HIT] == 1
    assert s["mid"]["n"] == 2 and s["mid"][ss.MISS] == 1 and s["mid"][ss.CORRECT_NEGATIVE] == 1
    assert s["tail"]["n"] == 1 and s["tail"][ss.FALSE_HIT] == 1
    text = "\n".join(ss.format_report({"src": s}, not_labelled=1))
    assert "50% (n=2)" in text and "not labelled: 1" in text


def test_cost_with_two_hits_divides_by_two_and_zero_hits_is_na():
    two = {"overall": {"n": 2, ss.HIT: 2, ss.FALSE_HIT: 0, ss.MISS: 0, ss.CORRECT_NEGATIVE: 0},
           **{t: {"n": 0, ss.HIT: 0, ss.FALSE_HIT: 0, ss.MISS: 0, ss.CORRECT_NEGATIVE: 0} for t in ws.TIERS},
           "unlabelled": 0}
    zero = {**two, "overall": {**two["overall"], ss.HIT: 0}}
    text = "\n".join(ss.format_report({"a": two, "b": zero}, cost={"a": 3.0, "b": 3.0}, seconds={"a": 10, "b": 10}))
    assert "a: dollars per correct board = 1.50 USD" in text
    assert "a: wall-clock 10 s; seconds per correct board = 5.0" in text
    assert "b: dollars per correct board = n/a (0 hits)" in text
    assert "b: wall-clock 10 s; seconds per correct board = n/a (0 hits)" in text


def test_no_cost_flag_means_no_dollar_or_clock_line(tmp_path, capsys):
    sp, tp, rp = run(tmp_path, RESULT)
    assert cli().main(["--sample", str(sp), "--truth", str(tp), "--result", f"src={rp}"],
                      normalizer=str.lower) == 0
    out = capsys.readouterr().out
    assert "dollars" not in out and "wall-clock" not in out and "src" in out


def test_cost_flag_through_cli(tmp_path, capsys):
    sp, tp, rp = run(tmp_path, RESULT)
    assert cli().main(["--sample", str(sp), "--truth", str(tp), "--result", f"src={rp}",
                       "--cost", "2.5"], normalizer=str.lower) == 0
    assert "dollars per correct board = 2.50 USD" in capsys.readouterr().out


def test_missing_mapped_column_exits_nonzero_and_names_header(tmp_path, capsys):
    sp, tp, rp = run(tmp_path, "Company Name,Board\nco0,x\n")
    code = cli().main(["--sample", str(sp), "--truth", str(tp), "--result", f"src={rp}",
                       "--company-col", "Name", "--url-col", "Board"], normalizer=str.lower)
    out = capsys.readouterr().out
    assert code != 0 and "Name" in out and "Company Name" in out and "Board" in out


def test_url_column_resolves_workday_triple_and_lever_slug(tmp_path):
    assert ss.board_from_url("https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/x") == \
        ("workday", "acme/wd5/Ext")
    assert ss.board_from_url("https://jobs.lever.co/beta/abc-123") == ("lever", "beta")
    assert ss.board_from_url("https://boards.greenhouse.io/alpha") == ("greenhouse", "alpha")
    assert ss.board_from_url("https://zed.bamboohr.com/careers") == ("bamboohr", "zed")
    assert ss.board_from_url("https://example.com/careers") == ("", "")
    text = ("Company,Link\nCO0,https://boards.greenhouse.io/alpha\n"
            "CO2,https://acme.wd5.myworkdayjobs.com/Ext\nCO1,https://jobs.lever.co/beta\n")
    s, _, _ = score_from(tmp_path, text, ss.Columns(company="Company", system=None, token=None, url="Link"))
    assert s["overall"][ss.HIT] == 3


def test_domain_join(tmp_path):
    s, _, unjoined = score_from(tmp_path, "site,sys,tok\nwww.co0.com,greenhouse,alpha\nnope.org,lever,q\n",
                                ss.Columns(company=None, domain="site", system="sys", token="tok"))
    assert s["overall"][ss.HIT] == 1 and unjoined == 1


def test_two_sources_get_a_row_each(tmp_path, capsys):
    sp, tp, rp = run(tmp_path, RESULT)
    rp2 = write(tmp_path, "res2.csv", "company_key,system,token\n")
    assert cli().main(["--sample", str(sp), "--truth", str(tp), "--result", f"one={rp}",
                       "--result", f"two={rp2}"], normalizer=str.lower) == 0
    lines = capsys.readouterr().out.splitlines()
    overall = lines[lines.index("[overall]") + 2: lines.index("[top]")]
    assert [l.split()[0] for l in overall] == ["one", "two"]


def test_init_truth_writes_blank_labels_and_refuses_overwrite_and_repo(tmp_path, capsys):
    sp = sample_file(tmp_path)
    out = tmp_path / "labels" / "truth.csv"
    assert cli().main(["--sample", str(sp), "--init-truth", str(out)]) == 0
    first = out.read_text(encoding="utf-8").splitlines()
    assert first[0] == "company_key,name,tier,domain,system,token" and len(first) == 7
    truth, blank = ss.read_truth(out)  # extra columns accepted; all rows unlabelled
    assert truth == {} and blank == 6
    assert cli().main(["--sample", str(sp), "--init-truth", str(out)]) == 1
    assert cli().main(["--sample", str(sp), "--init-truth", str(ROOT / "zz_truth.csv")]) == 1
    assert not (ROOT / "zz_truth.csv").exists()


def test_mapped_columns_without_join_flag_keep_company_key_or_exit_2(tmp_path, capsys):
    sp = sample_file(tmp_path)
    tp = write(tmp_path, "truth.csv", TRUTH)
    good = write(tmp_path, "good.csv", "company_key,board,slug\nco0,greenhouse,alpha\n")
    args = ["--sample", str(sp), "--truth", str(tp), "--system-col", "board", "--token-col", "slug"]
    assert cli().main([*args, "--result", f"a={good}"], normalizer=str.lower) == 0
    row = capsys.readouterr().out.split("[overall]")[1].splitlines()[2].split()
    assert row[:3] == ["a", "5", "1"]  # n=5 labelled, 1 HIT: the join worked
    bad = write(tmp_path, "bad.csv", "Company,board,slug\nco0,greenhouse,alpha\n")
    assert cli().main([*args, "--result", f"a={bad}"], normalizer=str.lower) == 2
    assert "company_key" in capsys.readouterr().out


def test_greenhouse_embed_url_uses_for_param():
    assert ss.board_from_url("https://boards.greenhouse.io/embed/job_board?for=acme") == ("greenhouse", "acme")
    assert ss.board_from_url("https://boards.greenhouse.io/embed/job_board") == ("greenhouse", "")
