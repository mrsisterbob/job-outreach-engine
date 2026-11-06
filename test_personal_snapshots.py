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


# ---- per-section retention and load_recent (TASK-291) ----

@pytest.fixture
def secdir(snapdir, monkeypatch):
    monkeypatch.delenv("PERSONAL_SNAPSHOT_SECTION_KEEP", raising=False)
    return snapdir


def _money(i):
    return {"money": {"net": i}, "period": f"m{i}"}


def _time(i):
    return {"time": {"hours": i}, "period": f"t{i}"}


def _both(i):
    return {"money": {"net": i}, "time": {"hours": i}, "period": f"b{i}"}


def _periods(recs):
    return [r.get("period") for r in recs]


def _repro():
    ps.save_snapshot(_money(0), now=_t(0))
    for i in range(1, 32):
        ps.save_snapshot(_time(i), now=_t(i))


def test_section_keep_reproduction_keeps_money(secdir):
    _repro()
    recs = [ps._read_snapshot(p) for p in ps.list_snapshots()]
    assert any("money" in r for r in recs)
    assert len(ps.list_snapshots()) <= ps.snapshot_keep() + 2 * ps.section_keep()


def test_section_keep_newest_three_money_survive(secdir):
    for i in range(5):
        ps.save_snapshot(_money(i), now=_t(i))
    for i in range(5, 45):
        ps.save_snapshot(_time(i), now=_t(i))
    recs = [ps._read_snapshot(p) for p in ps.list_snapshots()]
    money = [r["period"] for r in recs if "money" in r]
    assert money == ["m2", "m3", "m4"]


def test_section_keep_both_sections_count_for_both(secdir):
    ps.save_snapshot(_both(0), now=_t(0))
    for i in range(1, 41):
        ps.save_snapshot(_money(i), now=_t(i))
    assert any("time" in r for r in ps.load_recent(30))


@pytest.mark.parametrize("val,expected", [("1", 1), ("0", 3), ("abc", 3), (None, 3)])
def test_section_keep_env(secdir, monkeypatch, val, expected):
    if val is None:
        monkeypatch.delenv("PERSONAL_SNAPSHOT_SECTION_KEEP", raising=False)
    else:
        monkeypatch.setenv("PERSONAL_SNAPSHOT_SECTION_KEEP", val)
    assert ps.section_keep() == expected
    for i in range(6):
        ps.save_snapshot(_money(i), now=_t(i))
    for i in range(6, 46):
        ps.save_snapshot(_time(i), now=_t(i))
    recs = [ps._read_snapshot(p) for p in ps.list_snapshots()]
    assert len([r for r in recs if "money" in r]) == expected


def test_section_keep_corrupt_file_neither_raises_nor_protects(secdir):
    secdir.mkdir(parents=True)
    bad = secdir / "snapshot_20250101T000000000000_000.json"
    bad.write_text("{not json", encoding="utf-8")
    ps.save_snapshot(_time(1), now=_t(1))
    assert bad.exists()  # young enough: within newest 30
    for i in range(2, 40):
        ps.save_snapshot(_time(i), now=_t(i))
    assert not bad.exists()


def test_load_recent_reproduction_has_both_sections(secdir):
    _repro()
    recs = ps.load_recent(30)
    assert any("money" in r for r in recs) and any("time" in r for r in recs)
    assert len(recs) <= 32
    stamps = [r["saved_at"] for r in recs]
    assert stamps == sorted(stamps, reverse=True)


def test_load_recent_order_limit_clamp_and_empty(secdir):
    assert ps.load_recent() == []
    for i in range(10):
        ps.save_snapshot(_both(i), now=_t(i))
    recs = ps.load_recent(10)
    assert _periods(recs) == [f"b{i}" for i in range(9, -1, -1)]
    assert len(ps.load_recent(3)) == 3
    assert len(ps.load_recent(0)) == 1
    assert len(ps.load_recent(500)) == 10
    assert len(ps.load_recent("x")) == 10  # 30 default, only 10 exist
    (secdir / "snapshot_29990101T000000000000_000.json").write_text("junk", encoding="utf-8")
    assert len(ps.load_recent(30)) == 10


def test_load_recent_clamp_values(secdir):
    for i in range(40):
        ps.save_snapshot(_both(i), now=_t(i))
    assert len(ps.load_recent("x")) == 30
    assert len(ps.load_recent(0)) == 1


def test_load_recent_missing_dir(secdir):
    assert not secdir.exists()
    assert ps.load_recent(5) == []


def test_load_recent_returns_stored_records_and_never_writes(secdir):
    for i in range(5):
        ps.save_snapshot(_both(i), now=_t(i))
    before = ps.list_snapshots()
    contents = _read_all(secdir)
    recs = ps.load_recent(30)
    assert recs[0]["period"] == "b4" and "saved_at" in recs[0]
    assert ps.list_snapshots() == before
    assert _read_all(secdir) == contents


def test_load_recent_after_real_ingest_route(secdir, monkeypatch):
    from main import app
    monkeypatch.setenv("SNAPSHOT_INGEST_TOKEN", "tok")
    c = app.test_client()
    hdr = {"X-Snapshot-Token": "tok"}
    r = c.post("/snapshots/ingest", json=_money(0), headers=hdr)
    assert r.status_code == 200
    for i in range(1, 41):
        assert c.post("/snapshots/ingest", json=_time(i), headers=hdr).status_code == 200
    recs = ps.load_recent(30)
    money = [x for x in recs if "money" in x]
    assert [x["period"] for x in money] == ["m0"]
    assert len(ps.list_snapshots()) <= ps.snapshot_keep() + 2 * ps.section_keep()


def test_load_recent_upper_clamp_is_100(secdir, monkeypatch):
    monkeypatch.setenv("PERSONAL_SNAPSHOT_KEEP", "200")
    for i in range(120):
        ps.save_snapshot(_both(i), now=_t(i))
    assert len(ps.load_recent(500)) == 100
