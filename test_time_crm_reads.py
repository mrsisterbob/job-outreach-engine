"""time_crm_reads: fake crm_post only, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import time_crm_reads  # noqa: E402


class FakeResp:
    def __init__(self, body, status=200, content=b"x" * 123):
        self._body, self.status_code, self.content = body, status, content

    def json(self):
        return self._body


def _install(monkeypatch, responder):
    calls = []

    def crm_post(payload, timeout=10):
        calls.append(payload)
        return responder(payload)

    class FakeMain:
        pass

    FakeMain.crm_post = staticmethod(crm_post)
    monkeypatch.setattr(time_crm_reads, "_load_main", lambda: FakeMain)
    return calls


def test_normal_read_prints_seconds_rows_bytes_for_every_tab(monkeypatch, capsys):
    calls = _install(monkeypatch, lambda p: FakeResp({"status": "success", "followups": [{}, {}, {}]}))
    rc = time_crm_reads.main()
    out = capsys.readouterr().out
    assert rc == 0
    for tab in ("CW", "CC", "TC", "TW", "CL"):
        assert f"{tab}: " in out
    assert out.count("3 rows") == 5 and out.count("123 bytes") == 5
    assert "headroom" in out and "of 10s" in out
    assert [c["tab"] for c in calls] == ["CW", "CC", "TC", "TW", "CL"]
    assert all(c["action"] == "get_followups" for c in calls)
    assert all(set(c) == {"action", "tab"} for c in calls)


def test_none_is_timed_out_and_bad_body_is_failed_never_zero_rows(monkeypatch, capsys):
    def responder(p):
        if p["tab"] == "CW":
            return None
        if p["tab"] == "CC":
            return FakeResp({"status": "error", "message": "lock timeout"})
        if p["tab"] == "TC":
            return FakeResp({}, status=500)
        return FakeResp({"status": "success", "followups": []})

    calls = _install(monkeypatch, responder)
    rc = time_crm_reads.main()
    lines = {l.split(":")[0]: l for l in capsys.readouterr().out.splitlines()}
    assert rc == 1
    assert "TIMED OUT" in lines["CW"] and "0 rows" not in lines["CW"]
    assert "FAILED" in lines["CC"] and "0 rows" not in lines["CC"]
    assert "FAILED" in lines["TC"] and "0 rows" not in lines["TC"]
    assert "0 rows" in lines["TW"]  # a real success with no rows is still honest
    assert all(c["action"] == "get_followups" for c in calls)
