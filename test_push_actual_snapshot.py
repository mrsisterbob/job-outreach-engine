"""Tests for scripts/push_actual_snapshot.py. All data is synthetic; no real network call."""
import importlib.util
import os
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

import personal_snapshots
from test_actual_snapshot import BASE, CANARY, make_db

_spec = importlib.util.spec_from_file_location(
    "push_actual_snapshot", Path(__file__).parent / "scripts" / "push_actual_snapshot.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

TOKEN = "tok-SECRET-9931"
URL = "https://engine.example.com"


class Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def boom(*a, **k):
    raise AssertionError("post must not be called")


def fixed_post(status, body=None):
    def _post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        return Resp(status, body)
    return _post


def listing(*dirs):
    return sorted(str(p) for d in dirs for p in Path(d).rglob("*"))


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("real network use in a test")
    monkeypatch.setattr(requests, "post", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "budget"
    f.mkdir()
    make_db(str(f / "db.sqlite"), BASE)
    return f


def set_mtime(path, y, m, d):
    ts = datetime(y, m, d, 18, 0, tzinfo=timezone.utc).timestamp()
    os.utime(path, (ts, ts))


def test_dry_run_prints_and_sends_nothing(tmp_path, folder, capsys, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    before = listing(tmp_path)
    rc = P.main([str(folder), "--dry-run"], post=boom)
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-10" in out and "Nothing was sent." in out
    assert "Food | Usual | 562.50" in out and "Invest | Savings | 100.00" in out
    assert out.index("562.50") < out.index("100.00") < out.index("20.00") < out.index("7.00")
    assert "none - no off-budget accounts in this file" in out
    assert CANARY not in out and TOKEN not in out
    assert listing(tmp_path) == before


def test_real_route_end_to_end(tmp_path, folder, capsys, monkeypatch):
    import main
    snapdir = tmp_path / "snaps"
    snapdir.mkdir()
    monkeypatch.setenv("PERSONAL_SNAPSHOT_DIR", str(snapdir))
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    client = main.app.test_client()

    def post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        assert url == URL + "/snapshots/ingest"
        r = client.post("/snapshots/ingest", json=json, headers=headers)
        return Resp(r.status_code, r.get_json())

    rc = P.main([str(folder), "--url", URL], post=post)
    out = capsys.readouterr().out
    assert rc == 0 and "HTTP 200" in out and "received_at:" in out and TOKEN not in out
    latest = personal_snapshots.load_latest()
    assert latest["received_at"] and len(latest["money"]["flow"]) == 4
    fm = pytest.importorskip("finance_model")
    now = datetime.now(timezone.utc)
    view = fm.build_finance_view([latest], now)
    totals = {b["bucket"]: b["total"] for b in view["money"]["buckets"]}
    assert totals == {"Usual": 582.5, "Savings": 100.0, "Uncategorized": 7.0}


def test_wrong_token_and_unset_server_token(tmp_path, folder, capsys, monkeypatch):
    import main
    snapdir = tmp_path / "snaps"
    snapdir.mkdir()
    monkeypatch.setenv("PERSONAL_SNAPSHOT_DIR", str(snapdir))
    client = main.app.test_client()

    def post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        r = client.post("/snapshots/ingest", json=json, headers=headers)
        return Resp(r.status_code, r.get_json())

    # PC side sends the env token; the server (same process) sees the same variable, so make
    # the server compare against a different value by sending a different header via the adapter.
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", "server-real")

    def wrong_header_post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        return post(url, json=json, headers={"X-Snapshot-Token": "not-it"}, timeout=timeout)

    rc = P.main([str(folder), "--url", URL], post=wrong_header_post)
    out = capsys.readouterr().out
    assert rc == 1 and "403" in out and "refused the token" in out
    assert personal_snapshots.list_snapshots() == []

    # server token unset: the PC still has one, so send via an adapter that clears it server-side
    def unset_server_post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        monkeypatch.delenv("SNAPSHOT_INGEST_TOKEN")
        try:
            return post(url, json=json, headers=headers, timeout=timeout)
        finally:
            monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", "server-real")

    rc = P.main([str(folder), "--url", URL], post=unset_server_post)
    out = capsys.readouterr().out
    assert rc == 1 and "503" in out and "no ingest token configured" in out
    assert personal_snapshots.list_snapshots() == []


def test_missing_url_or_token(folder, capsys, monkeypatch):
    monkeypatch.delenv("ENGINE_URL", raising=False)
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    assert P.main([str(folder)], post=boom) == 2
    out = capsys.readouterr()
    assert "URL" in out.out and TOKEN not in out.out + out.err
    monkeypatch.delenv("SNAPSHOT_INGEST_TOKEN")
    assert P.main([str(folder), "--url", URL], post=boom) == 2
    assert "token" in capsys.readouterr().out
    monkeypatch.setenv("ENGINE_URL", URL)
    assert P.main([str(folder)], post=boom) == 2   # URL from env, token still missing


def test_url_scheme_rules(folder, capsys, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    assert P.main([str(folder), "--url", "http://example.com"], post=boom) == 2
    assert "Nothing was sent" in capsys.readouterr().out
    seen = []

    def post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        seen.append(url)
        return Resp(200, {"received_at": "x"})

    assert P.main([str(folder), "--url", "https://example.com"], post=post) == 0
    assert P.main([str(folder), "--url", "http://localhost:5000/"], post=post) == 0
    assert seen == ["https://example.com/snapshots/ingest", "http://localhost:5000/snapshots/ingest"]


def test_empty_export_never_posted(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    p = tmp_path / "db.sqlite"
    make_db(str(p), [{"id": "i1", "category": "pay", "amount": 300000, "date": 20261007}])
    rc = P.main([str(p), "--url", URL], post=boom)
    assert rc == 1 and "Nothing to send for 2026-10." in capsys.readouterr().out


def test_token_never_printed_on_failure(folder, capsys, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)

    def post(*a, **k):
        raise RuntimeError(TOKEN)

    rc = P.main([str(folder), "--url", URL], post=post)
    cap = capsys.readouterr()
    assert rc == 1 and "RuntimeError" in cap.out and TOKEN not in cap.out + cap.err


def test_bad_month_and_ambiguous_folders(tmp_path, folder, capsys, monkeypatch):
    rc = P.main([str(folder), "--month", "2026-09", "--dry-run"], post=boom)
    out = capsys.readouterr().out
    assert rc == 2 and "2026-10" in out
    home = tmp_path / "home"
    for name in ("Alpha-1", "Beta-2"):
        d = home / "Documents" / "Actual" / name
        d.mkdir(parents=True)
        make_db(str(d / "db.sqlite"), BASE)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    rc = P.main(["--dry-run"], post=boom)
    out = capsys.readouterr().out
    assert rc == 2 and "Alpha-1" in out and "Beta-2" in out and "db.sqlite" not in out
    shutil.rmtree(home / "Documents" / "Actual" / "Beta-2")
    assert P.main(["--dry-run"], post=boom) == 0


def test_stale_last_transaction_warning(tmp_path, capsys):
    row = [{"id": "t1", "category": "food", "amount": -1000, "date": 20261003}]
    old = tmp_path / "old"
    old.mkdir()
    make_db(str(old / "db.sqlite"), row)
    set_mtime(old / "db.sqlite", 2026, 10, 13)
    P.main([str(old), "--dry-run"], post=boom)
    out = capsys.readouterr().out
    assert "Last transaction in this file is 10 days older than the file itself" in out
    assert "as-of 2026-10-13" in out
    new = tmp_path / "new"
    new.mkdir()
    make_db(str(new / "db.sqlite"), row)
    set_mtime(new / "db.sqlite", 2026, 10, 5)
    P.main([str(new), "--dry-run"], post=boom)
    assert "days older" not in capsys.readouterr().out


def test_server_400_text_printed(folder, capsys, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    rc = P.main([str(folder), "--url", URL],
                post=fixed_post(400, {"status": "invalid", "error": "flow row has odd amount"}))
    out = capsys.readouterr().out
    assert rc == 1 and "flow row has odd amount" in out and TOKEN not in out


def test_413_and_unexpected_reply(folder, capsys, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    assert P.main([str(folder), "--url", URL], post=fixed_post(413)) == 1
    assert "too large" in capsys.readouterr().out
    assert P.main([str(folder), "--url", URL], post=fixed_post(500)) == 1
    assert "Unexpected" in capsys.readouterr().out


def test_redirect_is_not_followed_and_is_unexpected(folder, capsys, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    seen = {}

    def post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        seen["redir"] = allow_redirects
        return Resp(302)

    assert P.main([str(folder), "--url", URL], post=post) == 1
    cap = capsys.readouterr()
    assert seen["redir"] is False and "Unexpected" in cap.out and TOKEN not in cap.out + cap.err


def test_dry_run_makes_no_request(folder, monkeypatch, capsys):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    monkeypatch.setenv("ENGINE_URL", URL)
    assert P.main([str(folder), "--dry-run"]) == 0   # default post would hit the denying fixture
    assert "Nothing was sent." in capsys.readouterr().out


def test_dry_run_warns_on_empty(tmp_path, capsys):
    p = tmp_path / "db.sqlite"
    make_db(str(p), [{"id": "i1", "category": "pay", "amount": 300000, "date": 20261007}])
    assert P.main([str(p), "--dry-run"], post=boom) == 0
    assert "a real run would refuse: nothing to send" in capsys.readouterr().out


def test_no_token_beats_empty_guard(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SNAPSHOT_INGEST_TOKEN", raising=False)
    p = tmp_path / "db.sqlite"
    make_db(str(p), [{"id": "i1", "category": "pay", "amount": 300000, "date": 20261007}])
    assert P.main([str(p), "--url", URL], post=boom) == 2


def test_unreadable_file_prints_class_only(tmp_path, capsys):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip at all")
    rc = P.main([str(bad), "--dry-run"], post=boom)
    out = capsys.readouterr().out
    assert rc == 2 and "Traceback" not in out


def test_preview_shows_excluded_total_and_counts(folder, capsys):
    P.main([str(folder), "--dry-run"], post=boom)
    out = capsys.readouterr().out
    assert "Excluded rows (total):" in out and "Other non-zero counts:" in out


def test_bad_date_prints_could_not_compare():
    snap = {"money": {"as_of": "2026-10-13", "period": "2026-10", "flow": [], "off_budget": []}}
    lines = P.summary_lines(snap, {"newest_txn_date": "garbage"})
    assert any("Could not compare" in x for x in lines)


@pytest.mark.parametrize("url", ["https://user:pw@example.com", "https://example.com/x",
                                 "https://example.com?a=1", "https://example.com#f",
                                 "https://example.com:notaport"])
def test_url_rejects_userinfo_path_query_fragment(url):
    assert not P.url_ok(url)


def test_server_text_truncated_and_cleaned(folder, capsys, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    nasty = "bad[31m" + "x" * 500
    assert P.main([str(folder), "--url", URL], post=fixed_post(400, {"error": nasty})) == 1
    out = capsys.readouterr().out
    assert "" not in out and "x" * 201 not in out


def test_200_without_readable_body_is_flagged(folder, capsys, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)

    class Bad(Resp):
        def json(self):
            raise ValueError("no json")

    rc = P.main([str(folder), "--url", URL], post=lambda *a, **k: Bad(200))
    assert rc == 0 and "Unexpected reply body" in capsys.readouterr().out


@pytest.mark.parametrize("status", [400, 403, 413, 503, 500, 302])
def test_token_absent_on_every_failure_path(folder, capsys, monkeypatch, status):
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", TOKEN)
    assert P.main([str(folder), "--url", URL], post=fixed_post(status, {"error": "e"})) == 1
    cap = capsys.readouterr()
    assert TOKEN not in cap.out + cap.err
