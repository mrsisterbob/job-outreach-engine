from board_system_classifier import classify_board_system as c


def test_workday():
    assert c("https://acme.wd5.myworkdayjobs.com/en-US/Careers", "") == "workday"


def test_greenhouse_iframe():
    assert c("https://acme.com/careers", '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>') == "greenhouse"


def test_lever_host():
    assert c("https://jobs.lever.co/acme", "") == "lever"


def test_icims_link():
    assert c("https://acme.com/careers", '<a href="https://careers-acme.icims.com/jobs">Jobs</a>') == "icims"


def test_taleo():
    assert c("https://acme.taleo.net/careersection/ex/jobsearch.ftl", "") == "taleo"


def test_successfactors_script():
    assert c("https://acme.com/c", '<script src="https://career5.successfactors.com/x.js"></script>') == "successfactors"


def test_oracle():
    assert c("https://eabc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience", "") == "oracle"


def test_own_site():
    assert c("https://acme.com/careers", "<h1>Open positions</h1><ul><li>Engineer</li></ul>") == "own_site"


def test_unknown():
    assert c("https://acme.com/about", "<p>We make chairs.</p>") == "unknown"


def test_two_vendors_not_first_wins():
    html = '<a href="https://jobs.lever.co/a">a</a><a href="https://boards.greenhouse.io/b">b</a>'
    assert c("https://acme.com/careers", html) == "unknown"


def test_empty():
    assert c("", "") == "unknown"


def test_non_string():
    assert c(None, 123) == "unknown"
