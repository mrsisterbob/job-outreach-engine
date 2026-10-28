"""warm_sample tests: made-up companies, fake TSV text in tmp_path, no real export."""
import importlib.util
from pathlib import Path

import pytest

import warm_sample as ws

ROOT = Path(ws.__file__).resolve().parent


def tsv_row(company, email="", person="Zed"):
    cols = [person, "Zork", company, email, "x", "x", "x", "x", "x", "x", "x", company]
    return "\t".join(cols)


def make_tsv(n_heavy=60, n_single=40):
    rows = []
    for i in range(n_heavy):
        name = f"Heavy{chr(65 + i % 26)}{chr(65 + i // 26)} Industries"
        for j in range(2 + i):
            rows.append(tsv_row(name, f"p{j}@gmail.com"))
    for i in range(n_single):
        rows.append(tsv_row(f"Solo{chr(65 + i % 26)}{chr(65 + i // 26)} Works"))
    return "\n".join(rows) + "\n"


def write_warm(tmp_path, text=None, name="warm.tsv"):
    p = tmp_path / name
    p.write_text(text if text is not None else make_tsv(), encoding="utf-8")
    return p


def cli():
    spec = importlib.util.spec_from_file_location("bws", ROOT / "scripts" / "build_warm_sample.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_cli(warm, out, *extra):
    return cli().main(["--warm", str(warm), "--out", str(out), *extra], normalizer=str.lower)


def test_sixty_rows_twenty_per_tier_no_duplicate(tmp_path):
    out = tmp_path / "out" / "s.csv"
    assert run_cli(write_warm(tmp_path), out) == 0
    rows = ws.read_sample(out)
    assert len(rows) == 60
    assert [sum(1 for r in rows if r["tier"] == t) for t in ws.TIERS] == [20, 20, 20]
    assert len({r["company_key"] for r in rows}) == 60


def test_same_input_twice_is_byte_identical(tmp_path):
    warm = write_warm(tmp_path)
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    run_cli(warm, a)
    run_cli(warm, b)
    assert a.read_bytes() == b.read_bytes()


def test_smaller_band_yields_fewer_rows_and_says_so(tmp_path, capsys):
    out = tmp_path / "s.csv"
    assert run_cli(write_warm(tmp_path, make_tsv(n_single=5)), out) == 0
    rows = ws.read_sample(out)
    assert sum(1 for r in rows if r["tier"] == "tail") == 5 and len(rows) == 45
    assert "SHORT" in capsys.readouterr().out.splitlines()[0]


def test_alumni_only_companies_never_appear(tmp_path):
    warm = write_warm(tmp_path)
    alumni = tmp_path / "alumni.tsv"
    alumni.write_text("\n".join(tsv_row("Alumnionly Corp") for _ in range(9)) + "\n", encoding="utf-8")
    out = tmp_path / "s.csv"
    run_cli(warm, out)
    assert alumni.exists() and "Alumnionly" not in out.read_text(encoding="utf-8")


def test_domain_source_email_vs_guess(tmp_path):
    text = "\n".join([tsv_row("Realco Inc", "a@realco.example.com"), tsv_row("Realco Inc", "b@realco.example.com"),
                      tsv_row("Mailco Inc", "c@gmail.com")]) + "\n"
    out = tmp_path / "s.csv"
    run_cli(write_warm(tmp_path, text), out)
    by = {r["name"]: r for r in ws.read_sample(out)}
    assert by["Realco Inc"]["domain_source"] == "email" and by["Realco Inc"]["domain"] == "realco.example.com"
    assert by["Mailco Inc"]["domain_source"] == "guess" and by["Mailco Inc"]["domain"] == "mailcoinc.com"


def test_existing_out_not_overwritten_without_force(tmp_path, capsys):
    out = tmp_path / "s.csv"
    out.write_text("keep me", encoding="utf-8")
    assert run_cli(write_warm(tmp_path), out) == 1
    assert out.read_text(encoding="utf-8") == "keep me"
    assert "already exists" in capsys.readouterr().out
    assert run_cli(write_warm(tmp_path), out, "--force") == 0
    assert len(ws.read_sample(out)) == 60


def test_out_inside_repo_is_refused(tmp_path):
    out = ROOT / "should_not_exist_warm_sample.csv"
    assert run_cli(write_warm(tmp_path), out) == 1
    assert not out.exists()


def test_person_email_local_part_never_in_output(tmp_path):
    text = "\n".join(tsv_row("Leakco Inc", "secretperson.name@leakco.example.com", person="Secretfirst")
                     for _ in range(3)) + "\n"
    out = tmp_path / "s.csv"
    run_cli(write_warm(tmp_path, text), out)
    body = out.read_text(encoding="utf-8").lower()
    assert "secretperson" not in body and "secretfirst" not in body and "@" not in body


def test_read_sample_rejects_bad_files(tmp_path):
    out = tmp_path / "s.csv"
    run_cli(write_warm(tmp_path), out)
    good = out.read_text(encoding="utf-8")
    bad_tier = tmp_path / "t.csv"
    bad_tier.write_text(good.replace(",top,", ",bogus,", 1), encoding="utf-8")
    with pytest.raises(ValueError):
        ws.read_sample(bad_tier)
    short = tmp_path / "n.csv"
    short.write_text("\n".join(good.splitlines()[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ws.read_sample(short)
    nocol = tmp_path / "c.csv"
    nocol.write_text(good.replace("domain_source", "other"), encoding="utf-8")
    with pytest.raises(ValueError):
        ws.read_sample(nocol)


def test_hand_edited_domain_is_read_back_as_written(tmp_path):
    out = tmp_path / "s.csv"
    run_cli(write_warm(tmp_path), out)
    first = ws.read_sample(out)[0]["domain"]
    out.write_text(out.read_text(encoding="utf-8").replace(first, "edited.example.org", 1), encoding="utf-8")
    assert ws.read_sample(out)[0]["domain"] == "edited.example.org"
