import warm_big_companies as w

ROWS = [("Walmart", 23, "scanned"), ("Rocket Companies", 17, "no_board"),
        ("Ford", 14, "unchecked"), ("Tiny", 3, "scanned")]


def test_warm_big_companies_lists_only_ten_plus():
    text = "\n".join(w.big_company_report(ROWS))
    assert "Tiny" not in text and "Walmart" in text and "Ford" in text


def test_warm_big_companies_sorted_biggest_first_with_header():
    lines = w.big_company_report([("Ford", 14, "scanned"), ("Walmart", 23, "scanned"), ("GM", 14, "scanned")])
    assert lines[0].startswith("Biggest warm companies (10+ contacts): 51. 51 have a board")
    assert [l.split(" (")[0] for l in lines[1:]] == ["• Walmart", "• Ford", "• GM"]


def test_warm_big_companies_each_status_has_its_wording():
    lines = w.big_company_report(ROWS)
    assert "• Walmart (23) - board scanned" in lines
    assert "• Rocket Companies (17) - no board found" in lines
    assert "• Ford (14) - not looked up yet" in lines
    assert lines[0].endswith("23 have a board, 17 have none, 14 not looked up yet.")


def test_warm_big_companies_caps_at_twelve_lines():
    lines = w.big_company_report([(f"Co{i:02d}", 20 - i % 5, "scanned") for i in range(13)])
    assert len(lines) == 1 + 12 + 1 and lines[-1] == "and 1 more"


def test_warm_big_companies_escapes_angle_brackets():
    lines = w.big_company_report([("A<b>Corp", 10, "scanned")])
    assert "A&lt;b&gt;Corp" in lines[1] and "<b>" not in lines[1]


def test_warm_big_companies_nobody_big_gives_no_lines():
    assert w.big_company_report([("Tiny", 9, "scanned")]) == []
    assert w.big_company_report([]) == []


FIVE = [("A", 5, "scanned"), ("B", 4, "scanned"), ("C", 9, "no_board"), ("D", 6, "unchecked"),
        ("E", 40, "not_company")]


def test_warm_five_plus_exactly_five_is_in_and_four_is_out():
    line = w.board_target_line([("In", 5, "scanned"), ("Out", 4, "scanned")])
    assert line.startswith("Companies with 5+ contacts: 1 (5 contacts)")


def test_warm_five_plus_buckets_sum_to_total_and_not_company_is_left_out():
    line = w.board_target_line(FIVE)
    assert line == ("Companies with 5+ contacts: 3 (20 contacts), of which 1 have a board connected (5 contacts), "
                    "1 checked with none found (9 contacts), 1 not looked up yet (6 contacts).")


def test_warm_five_plus_no_rows_returns_nothing():
    assert w.board_target_line([]) == ""
    assert w.board_target_line([("X", 4, "scanned"), ("Y", 50, "not_company")]) == ""


def test_warm_five_plus_threshold_defined_once():
    assert w.WARM_BOARD_TARGET_MIN_CONTACTS == 5
