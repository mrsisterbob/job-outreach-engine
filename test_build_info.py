import build_info as bi

SHA = "a" * 40
OTHER = "b" * 40
THIRD = "c" * 40


def _file(text):
    return lambda name: text


def _git(text):
    return lambda args: text


def test_build_info_env_wins_over_file_and_git():
    info = bi.running_commit({"RENDER_GIT_COMMIT": SHA}, _file(OTHER), _git(THIRD))
    assert info == {"commit": SHA, "short": "aaaaaaa", "source": "env:RENDER_GIT_COMMIT"}


def test_build_info_file_wins_over_git_and_reads_only_the_first_line():
    info = bi.running_commit({}, _file(OTHER + "\nsecond line"), _git(THIRD))
    assert info["commit"] == OTHER and info["source"] == "file:BUILD_COMMIT"


def test_build_info_git_is_used_when_env_and_file_are_empty():
    info = bi.running_commit({"RENDER_GIT_COMMIT": ""}, _file(""), _git(THIRD + "\n"))
    assert info["commit"] == THIRD and info["source"] == "git"


def test_build_info_nothing_answers_is_unknown_with_an_empty_short():
    info = bi.running_commit({}, None, None)
    assert info == {"commit": "", "short": "", "source": "unknown"}
    assert bi.running_commit(None)["source"] == "unknown"


def test_build_info_a_non_hex_env_value_is_skipped_and_the_file_answers():
    info = bi.running_commit({"RENDER_GIT_COMMIT": "latest"}, _file(OTHER))
    assert info["commit"] == OTHER and info["source"] == "file:BUILD_COMMIT"


def test_build_info_a_raising_source_is_skipped_never_fatal():
    def boom(*a):
        raise RuntimeError("no git here")
    assert bi.running_commit({}, boom, boom)["source"] == "unknown"
    assert bi.running_commit({}, boom, _git(THIRD))["source"] == "git"


def test_build_info_uppercase_hex_is_lowercased():
    assert bi.running_commit({"RENDER_GIT_COMMIT": "ABCDEF1234"})["commit"] == "abcdef1234"


def test_build_info_a_short_or_long_or_error_value_is_rejected():
    assert bi.running_commit({"RENDER_GIT_COMMIT": "abc12"})["source"] == "unknown"
    assert bi.running_commit({"RENDER_GIT_COMMIT": "a" * 41})["source"] == "unknown"
    assert bi.running_commit({}, None, _git("fatal: not a git repository"))["source"] == "unknown"
    assert bi.running_commit({}, None, _git("HEAD"))["source"] == "unknown"


def test_build_info_format_build_for_a_known_and_an_unknown_commit():
    known = bi.running_commit({"RENDER_GIT_COMMIT": SHA})
    assert bi.format_build(known) == "build aaaaaaa (env:RENDER_GIT_COMMIT)"
    text = bi.format_build(bi.running_commit({}))
    assert text == "build unknown (set on Render: see /health)" and "a" * 7 not in text
    assert bi.format_build(None) == text


def test_build_info_non_string_values_do_not_raise():
    assert bi.running_commit({"RENDER_GIT_COMMIT": 12345678})["source"] == "unknown"
    assert bi.running_commit({"RENDER_GIT_COMMIT": None}, _file(None), _git(123))["source"] == "unknown"
