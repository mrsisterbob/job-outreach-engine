import json
import sys
from pathlib import Path

import pytest

import ats_resolver_report as arr

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import measure_ats_resolver as mar  # noqa: E402

TOKEN = "tok_SECRET_123"
ROWS = [
    {"atsPlatform": "x1", "boardToken": "acme", "boardUrl": "https://b/acme", "companyName": "Acme", "description": "d" * 5000, "n": 1},
    {"atsPlatform": "x1", "boardToken": "", "extra": [1]},
    {"atsPlatform": "x2", "boardToken": "zed", "boardUrl": "", "n": "s"},
]


class Resp:
    def __init__(self, text, code=200):
        self.text, self.status_code = text, code
        self.content = text if isinstance(text, bytes) else text.encode("utf-8")


def args_for(tmp_path, record=None):
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps({"q": ["a"]}))
    return ["--input-json", str(inp), "--record", str(record or tmp_path / "rec")]


def test_census_counts_keys_and_types():
    c = arr.census(ROWS)
    assert c["row_count"] == 3
    assert c["keys"]["n"] == {"present": 2, "types": ["int", "str"]}
    assert set(c["keys"]) == {"atsPlatform", "boardToken", "boardUrl", "companyName", "description", "n", "extra"}


def test_platforms_and_blank_token():
    c = arr.census(ROWS)
    assert c["ats_platforms"] == {"x1": 2, "x2": 1}
    assert c["board_token_rows"] == 2 and c["blank_token_rows"] == 1
    assert c["board_url_rows"] == 1


def test_sample_and_no_long_text():
    c = arr.census(ROWS)
    assert set(c["sample"]) == {"atsPlatform", "boardToken", "boardUrl", "companyName"}
    text = "\n".join(arr.render_census(c))
    assert "d" * 100 not in text
    assert "description =" not in text


@pytest.mark.parametrize("bad", [[], {"a": 1}, None, ["a", "b"], [{"a": 1}, "b"]])
def test_bad_shapes_give_note(bad):
    c = arr.census(bad)
    assert c["row_count"] == 0 and c["note"]
    assert any("note:" in line for line in arr.render_census(c))


def test_no_confirm_makes_no_request(tmp_path):
    calls, lines = [], []
    rc = mar.run(args_for(tmp_path), post=lambda *a, **k: calls.append(1), out=lines.append,
                 root=tmp_path / "repo", environ={"APIFY_TOKEN": TOKEN})
    assert rc == 2 and calls == []
    assert any("<set>" in line for line in lines) and TOKEN not in "\n".join(lines)


def test_record_inside_repo_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls, lines = [], []
    rc = mar.run(args_for(tmp_path, repo / "rec") + ["--confirm-spend"], post=lambda *a, **k: calls.append(1),
                 out=lines.append, root=repo, environ={"APIFY_TOKEN": TOKEN})
    assert rc == 2 and calls == []


def test_token_never_leaks(tmp_path, capsys):
    lines = []

    def boom(*a, **k):
        raise RuntimeError("bad " + TOKEN)

    rc = mar.run(args_for(tmp_path) + ["--confirm-spend"], post=boom, out=lines.append,
                 root=tmp_path / "repo", environ={"APIFY_TOKEN": TOKEN})
    assert rc == 1 and "RuntimeError" in "\n".join(lines)
    cap = capsys.readouterr()
    assert TOKEN not in "\n".join(lines) + cap.out + cap.err

    lines.clear()
    seen = {}

    def ok(url, **k):
        seen.update(k)
        return Resp(json.dumps(ROWS))

    rc = mar.run(args_for(tmp_path) + ["--confirm-spend"], post=ok, out=lines.append,
                 root=tmp_path / "repo", environ={"APIFY_TOKEN": TOKEN})
    assert rc == 0 and seen["json"] == {"q": ["a"]} and seen["allow_redirects"] is False
    recs = list((tmp_path / "rec").glob("ats_resolver_*.json"))
    assert len(recs) == 1 and TOKEN not in recs[0].read_text()
    assert TOKEN not in "\n".join(lines) and lines[-1] == mar.CLOSING


def test_non_json_and_non_list_exit_1_and_save(tmp_path):
    for body in ("<html>", '{"a":1}'):
        rc = mar.run(args_for(tmp_path) + ["--confirm-spend"], post=lambda *a, **k: Resp(body),
                     out=lambda s: None, root=tmp_path / "repo", environ={"APIFY_TOKEN": TOKEN})
        assert rc == 1
    assert list((tmp_path / "rec").glob("ats_resolver_*.json"))


def test_value_tallies_for_token_and_url():
    c = arr.census(ROWS)
    assert c["board_tokens"] == {"acme": 1, "zed": 1}
    assert c["board_urls"] == {"https://b/acme": 1}
    text = "\n".join(arr.render_census(c))
    assert "boardToken values:" in text and "  acme: 1" in text and "  https://b/acme: 1" in text


def test_value_tallies_capped():
    rows = [{"boardToken": "t%02d" % i, "boardUrl": "u" * 500} for i in range(25)]
    lines = arr.render_census(arr.census(rows))
    text = "\n".join(lines)
    assert "+15 more" in text
    assert "u" * 100 not in text
    assert sum(1 for l in lines if l.startswith("  t")) == 10


def test_saved_bytes_exact_and_collision_keeps_earlier(tmp_path):
    # real LF bytes between tokens; a "\n" escape inside a string; both must survive byte for byte
    body = b'[\n{"a": "x\\ny"}\n]'
    other = b'[\n{"a": "second"}\n]'
    for reply in (body, body, other):
        rc = mar.run(args_for(tmp_path) + ["--confirm-spend"], post=lambda *a, _r=reply, **k: Resp(_r),
                     out=lambda s: None, root=tmp_path / "repo", environ={"APIFY_TOKEN": TOKEN})
        assert rc == 0
    saved = list((tmp_path / "rec").glob("ats_resolver_*.json"))
    assert len(saved) == 3
    contents = sorted(p.read_bytes() for p in saved)
    assert contents == sorted([body, body, other])
    assert all(b"\r" not in c for c in contents)
