"""Pure tests for warm_export_reader: synthetic rows only, no network, no real names."""
import warm_export_reader as wer


def row(company="Acme", email="", col12=None):
    cols = ["Zed", "Zork", company, email, "x", "x", "x", "x", "x", "x", "x",
            company if col12 is None else col12]
    return "\t".join(cols)


def test_twelve_column_rows_parse():
    companies, stats = wer.parse_rows(row("Acme") + "\n" + row("Acme"), "a")
    assert stats == {"rows": 2, "bad_rows": 0, "no_company": 0}
    assert companies["acme"]["count"] == 2


def test_blank_column_three_falls_back_to_column_twelve():
    companies, stats = wer.parse_rows(row("", col12="Globex"), "a")
    assert companies["globex"]["count"] == 1 and stats["no_company"] == 0


def test_blank_in_both_columns_is_counted_and_skipped():
    companies, stats = wer.parse_rows(row("", col12=""), "a")
    assert companies == {} and stats["no_company"] == 1


def test_wrong_column_count_is_counted_not_fatal():
    companies, stats = wer.parse_rows("a\tb\tc\n" + row("Acme"), "a")
    assert stats["bad_rows"] == 1 and companies["acme"]["count"] == 1


def test_two_spellings_group_together():
    norm = lambda n: n.lower().replace("inc", "").replace("corp", "")
    text = row("Acme Corp") + "\n" + row("ACME corp.") + "\n" + row("Acme, Inc")
    companies, _ = wer.parse_rows(text, "a", normalizer=norm)
    assert len(companies) == 1 and companies["acme"]["count"] == 3


def test_threshold_five_in_four_out():
    text = "\n".join([row("Five")] * 5 + [row("Four")] * 4)
    companies, _ = wer.parse_rows(text, "a")
    assert set(wer.companies_at_least(companies)) == {"five"}


def test_two_files_combine_with_per_source_counts():
    a = wer.parse_rows("\n".join([row("Acme")] * 3), "file1")
    b = wer.parse_rows("\n".join([row("Acme")] * 2), "file2")
    companies, stats = wer.combine([a, b])
    assert companies["acme"]["count"] == 5 and stats["rows"] == 5
    assert companies["acme"]["by_source"] == {"file1": 3, "file2": 2}
    assert "acme" in wer.companies_at_least(companies)


def test_output_never_holds_a_name_or_full_email():
    text = row("Acme", email="Zed.Zork@acme-corp.com")
    companies, stats = wer.parse_rows(text, "a")
    blob = repr(companies) + repr(stats)
    assert "Zed" not in blob and "Zork" not in blob and "zed.zork" not in blob.lower()
    assert companies["acme"]["domains"] == ["acme-corp.com"]


def test_blank_and_missing_files_give_empty_set(tmp_path):
    assert wer.parse_rows("", "a") == ({}, {"rows": 0, "bad_rows": 0, "no_company": 0})
    blank = tmp_path / "blank.tsv"
    blank.write_text("")
    companies, _ = wer.read_files([str(blank), str(tmp_path / "missing.tsv")])
    assert wer.companies_at_least(companies) == {}


def test_read_files_combines_on_disk(tmp_path):
    p1, p2 = tmp_path / "1.tsv", tmp_path / "2.tsv"
    p1.write_text("\n".join([row("Acme")] * 3))
    p2.write_text("\n".join([row("Acme")] * 2))
    companies, _ = wer.read_files([str(p1), str(p2)])
    assert companies["acme"]["count"] == 5
