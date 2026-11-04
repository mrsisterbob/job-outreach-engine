"""Offline tests for apify_usage_report and scripts/measure_apify_usage. Fixtures are hand-built, not real replies."""
import json
import sys
from pathlib import Path

import apify_usage_report as aur

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import measure_apify_usage as mau  # noqa: E402

TOKEN = "tok_SECRET_123456"


class Resp:
    def __init__(self, code=200, content=b"{}"):
        self.status_code, self.content = code, content


class FakeGet:
    def __init__(self, mapping):
        self.mapping, self.calls = mapping, []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        v = self.mapping[url]
        if isinstance(v, Exception):
            raise v
        return v


def _run(tmp_path, fake, argv=None, token=TOKEN):
    lines = []
    rc = mau.run(["--record", str(tmp_path / "rec")] + (argv or []), get=fake, out=lines.append,
                 root=tmp_path / "repo", environ={"APIFY_TOKEN": token} if token else {})
    return rc, lines


def test_field_paths_nested_types_numbers_only():
    p = {a: (b, c) for a, b, c in aur.field_paths({"a": {"n": 3, "f": 1.5, "ok": True, "s": "hi", "z": None}})}
    assert p["a.n"] == ("int", 3) and p["a.f"] == ("float", 1.5) and p["a.ok"] == ("bool", True)
    assert p["a.s"] == ("str", None) and p["a.z"] == ("null", None) and p["a"] == ("dict", None)


def test_list_reported_by_first_item_with_count():
    p = {a: (b, c) for a, b, c in aur.field_paths({"rows": [{"x": 1}, {"x": 2}, {"x": 3}]})}
    assert p["rows"][0] == "list[3]" and p["rows[].x"] == ("int", 1)


def test_money_candidates_numeric_only():
    paths = aur.field_paths({"monthlyUsageUsd": 1.2, "usage": "high", "creditsLeft": 4, "other": 9, "spent": True})
    names = [c[0] for c in aur.money_candidates(paths)]
    assert names == ["creditsLeft", "monthlyUsageUsd"]


def test_scrub_drops_free_text_keeps_numbers_dates():
    out = aur.scrub({"e": "kevin@example.com", "long": "x" * 40, "u": "https://a.b/c", "d": "2026-11-01T00:00:00.000Z",
                     "n": 4.5, "short": "FREE", "l": ["a" * 30, 2]})
    assert out["e"] == "<str:17>" and out["long"] == "<str:40>" and out["u"] == "<str:13>"
    assert out["d"] == "2026-11-01T00:00:00.000Z" and out["n"] == 4.5 and out["short"] == "FREE"
    assert out["l"] == ["<str:30>", 2]


def test_no_token_zero_requests_exit_2(tmp_path):
    fake = FakeGet({})
    rc, _ = _run(tmp_path, fake, token=None)
    assert rc == 2 and fake.calls == []


def test_failures_reported_and_do_not_stop_the_other(tmp_path):
    a, b = mau.CANDIDATE_ENDPOINTS
    fake = FakeGet({a: TimeoutError("boom"), b: Resp(404, b"not json")})
    rc, lines = _run(tmp_path, fake)
    text = "\n".join(lines)
    assert rc == 1 and len(fake.calls) == 2
    assert "request failed: TimeoutError" in text and "HTTP status: 404" in text and "body is not JSON" in text
    fake = FakeGet({a: Resp(404, b"nope"), b: Resp(200, b'{"monthlyUsageUsd": 1.5}')})
    rc, lines = _run(tmp_path / "again", fake)
    assert rc == 0 and any("monthlyUsageUsd" in ln for ln in lines)


def test_record_inside_repo_refused(tmp_path):
    fake = FakeGet({})
    lines = []
    rc = mau.run(["--record", str(tmp_path / "repo" / "rec")], get=fake, out=lines.append,
                 root=tmp_path / "repo", environ={"APIFY_TOKEN": TOKEN})
    assert rc == 2 and fake.calls == []


def test_foreign_url_refused(tmp_path):
    fake = FakeGet({})
    rc, _ = _run(tmp_path, fake, argv=["--url", "https://evil.example/x"])
    assert rc == 2 and fake.calls == []


def test_token_never_leaks_and_raw_is_byte_exact(tmp_path, capsys):
    a, b = mau.CANDIDATE_ENDPOINTS
    body = json.dumps({"usageUsd": 2.0, "note": "x" * 30, "echo": TOKEN}).encode()
    fake = FakeGet({a: RuntimeError("bad " + TOKEN), b: Resp(200, body)})
    rc, lines = _run(tmp_path, fake)
    assert rc == 0
    assert all(kw["headers"]["Authorization"] == "Bearer " + TOKEN and kw["allow_redirects"] is False
               and kw["timeout"] == 20 for _, kw in fake.calls)
    cap = capsys.readouterr()
    printed = "\n".join(lines) + cap.out + cap.err
    assert TOKEN not in printed
    files = list((tmp_path / "rec").iterdir())
    raws = [f for f in files if not f.name.endswith(".scrubbed.json")]
    assert len(raws) == 1 and raws[0].read_bytes() == body
    scrubbed = [f for f in files if f.name.endswith(".scrubbed.json")]
    assert len(scrubbed) == 1 and TOKEN not in scrubbed[0].read_text()


def test_json_404_only_endpoint_exit_1(tmp_path):
    a, b = mau.CANDIDATE_ENDPOINTS
    fake = FakeGet({a: Resp(404, b'{"error": 1}'), b: Resp(401, b'{"error": 2}')})
    rc, lines = _run(tmp_path, fake)
    assert rc == 1 and "HTTP status: 404" in "\n".join(lines)
    assert not [f for f in (tmp_path / "rec").iterdir() if f.name.endswith(".scrubbed.json")]


def test_duplicate_url_one_request(tmp_path):
    u = "https://api.apify.com/v2/users/me/x"
    fake = FakeGet({u: Resp(200, b'{"a": 1}'), **{k: Resp(404, b"x") for k in mau.CANDIDATE_ENDPOINTS}})
    _run(tmp_path, fake, argv=["--url", u, "--url", u])
    assert [c[0] for c in fake.calls].count(u) == 1


def test_token_in_url_redacted(tmp_path, capsys):
    u = "https://api.apify.com/v2/users/me/x?token=" + TOKEN
    fake = FakeGet({u: Resp(200, b'{"a": 1}'), **{k: Resp(404, b"x") for k in mau.CANDIDATE_ENDPOINTS}})
    rc, lines = _run(tmp_path, fake, argv=["--url", u])
    cap = capsys.readouterr()
    assert TOKEN not in "\n".join(lines) + cap.out + cap.err
