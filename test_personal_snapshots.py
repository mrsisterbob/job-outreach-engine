import json
import os
from datetime import datetime, timezone, timedelta

import pytest

import personal_snapshots as ps


@pytest.fixture
def snapdir(tmp_path, monkeypatch):
    d = tmp_path / "snaps"
    monkeypatch.setenv("PERSONAL_SNAPSHOT_DIR", str(d))
    monkeypatch.delenv("PERSONAL_SNAPSHOT_KEEP", raising=False)
    return d


def _t(i):
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i)


def _read_all(d):
    out = {}
    for n in os.listdir(d):
        with open(os.path.join(d, n), encoding="utf-8") as f:
            out[n] = f.read()
    return out


def test_round_trip(snapdir):
    ps.save_snapshot({"total_spend": 12.5, "hours_logged": 3}, now=_t(0))
    got = ps.load_latest()
    assert got["total_spend"] == 12.5 and got["hours_logged"] == 3
    assert "saved_at" in got


def test_keeps_exactly_n_newest(snapdir):
    for i in range(35):
        ps.save_snapshot({"i": i}, now=_t(i))
    files = ps.list_snapshots()
    assert len(files) == 30 == len(os.listdir(snapdir))
    assert ps.load_latest()["i"] == 34
    with open(files[0], encoding="utf-8") as f:
        assert json.load(f)["i"] == 5


def test_keep_env(snapdir, monkeypatch):
    monkeypatch.setenv("PERSONAL_SNAPSHOT_KEEP", "3")
    for i in range(6):
        ps.save_snapshot({"i": i}, now=_t(i))
    assert len(ps.list_snapshots()) == 3
    assert ps.load_latest()["i"] == 5
    for bad in ("abc", "0", "-2"):
        monkeypatch.setenv("PERSONAL_SNAPSHOT_KEEP", bad)
        assert ps.snapshot_keep() == 30


def test_top_level_merchant_rejected_nothing_written(snapdir):
    with pytest.raises(ps.SnapshotRejected, match="merchant_name"):
        ps.save_snapshot({"merchant_name": "x", "total": 1})
    assert ps.list_snapshots() == []
    assert not snapdir.exists() or os.listdir(snapdir) == []


def test_nested_rejection_leaves_existing_untouched(snapdir, monkeypatch):
    monkeypatch.setenv("PERSONAL_SNAPSHOT_KEEP", "2")
    ps.save_snapshot({"i": 1}, now=_t(1))
    ps.save_snapshot({"i": 2}, now=_t(2))
    before = _read_all(snapdir)
    with pytest.raises(ps.SnapshotRejected, match="by_category"):
        ps.save_snapshot({"by_category": [{"transactions": [1]}]}, now=_t(3))
    assert _read_all(snapdir) == before


@pytest.mark.parametrize("key", ["Merchant", "payee-name", "TXN_ID", "lineItem",
                                 "line item", "Vendor", "counterpartyName", "memo"])
def test_key_variants_rejected(snapdir, key):
    with pytest.raises(ps.SnapshotRejected):
        ps.save_snapshot({"ok": {key: 1}})
    assert ps.list_snapshots() == []


def test_aggregate_keys_pass(snapdir):
    ps.save_snapshot({"total_spend": 1, "hours_logged": 2, "category_totals": {"food": 3}})
    assert ps.load_latest()["category_totals"] == {"food": 3}


def test_corrupt_newest_falls_back(snapdir):
    ps.save_snapshot({"i": 1}, now=_t(1))
    newest = ps.save_snapshot({"i": 2}, now=_t(2))
    with open(newest, "w") as f:
        f.write("{garbage")
    assert ps.load_latest()["i"] == 1


def test_empty_or_missing_dir_returns_none(snapdir):
    assert ps.load_latest() is None
    assert ps.list_snapshots() == []


def test_same_now_two_files(snapdir):
    a = ps.save_snapshot({"i": 1}, now=_t(0))
    b = ps.save_snapshot({"i": 2}, now=_t(0))
    assert a != b and len(ps.list_snapshots()) == 2
    assert ps.load_latest()["i"] == 2


def test_caller_saved_at_does_not_break_order(snapdir):
    ps.save_snapshot({"i": 1, "saved_at": "2099-01-01"}, now=_t(1))
    ps.save_snapshot({"i": 2, "saved_at": "1999-01-01"}, now=_t(2))
    assert ps.load_latest()["i"] == 2


def test_non_dict_and_unserializable_rejected(snapdir):
    with pytest.raises(ps.SnapshotRejected):
        ps.save_snapshot([1, 2])
    with pytest.raises(ps.SnapshotRejected):
        ps.save_snapshot({"x": object()})
    assert ps.list_snapshots() == []


def test_mixed_type_keys_rejected_not_typeerror(snapdir):
    with pytest.raises(ps.SnapshotRejected):
        ps.save_snapshot({"a": 1, 2: 3})
    assert ps.list_snapshots() == []


def test_self_referencing_dict_rejected(snapdir):
    d = {}
    d["self"] = d
    with pytest.raises(ps.SnapshotRejected):
        ps.save_snapshot(d)
    assert ps.list_snapshots() == []


def test_source_avoids_jobs_db():
    with open(ps.__file__, encoding="utf-8") as f:
        src = f.read()
    assert "sqlite3" not in src and "get_db_conn" not in src


def test_default_dir_next_to_module(monkeypatch):
    monkeypatch.delenv("PERSONAL_SNAPSHOT_DIR", raising=False)
    expected = os.path.join(os.path.dirname(os.path.abspath(ps.__file__)), "personal_snapshots")
    assert ps.snapshot_dir() == expected
