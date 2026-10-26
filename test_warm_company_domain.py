from warm_company_domain import corporate_domain, email_domain, guess_domain


def test_corporate_wins_over_freemail():
    assert corporate_domain(["a@gmail.com", "b@Acme.com"]) == "acme.com"


def test_only_freemail_returns_empty():
    assert corporate_domain(["a@gmail.com", "b@yahoo.com", "c@comcast.net"]) == ""


def test_majority_beats_one_off():
    assert corporate_domain(["a@x.com", "b@acme.com", "c@acme.com"]) == "acme.com"


def test_edu_ignored():
    assert corporate_domain(["a@msu.edu", "b@umich.edu"]) == ""


def test_malformed_does_not_raise():
    assert corporate_domain(["nope", "a@@b.com", None, 5, "a@b", ""]) == ""
    assert email_domain("x@") == ""


def test_empty_inputs():
    assert corporate_domain(None) == ""
    assert corporate_domain([]) == ""


def test_guess_domain():
    assert guess_domain("acme") == "acme.com"
    assert guess_domain("") == ""
