"""Tests for actual_snapshot. All data is synthetic."""
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import actual_snapshot as A
import personal_snapshots

CANARY = "ZZ-CANARY-MERCHANT-4471"


def make_db(path, txns=(), accounts=None, extra_cats=()):
    """txns: dicts with id, acct, category, amount, date and optional overrides."""
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, offbudget INTEGER, closed INTEGER,
        tombstone INTEGER, balance_current INTEGER, balance_available INTEGER, bank TEXT);
    CREATE TABLE category_groups (id TEXT PRIMARY KEY, name TEXT, is_income INTEGER, tombstone INTEGER, hidden INTEGER);
    CREATE TABLE categories (id TEXT PRIMARY KEY, name TEXT, is_income INTEGER, cat_group TEXT, tombstone INTEGER, hidden INTEGER);
    CREATE TABLE category_mapping (id TEXT PRIMARY KEY, transferId TEXT);
    CREATE TABLE payees (id TEXT PRIMARY KEY, name TEXT);
    CREATE TABLE transactions (id TEXT PRIMARY KEY, isParent INTEGER DEFAULT 0, isChild INTEGER DEFAULT 0,
        acct TEXT, category TEXT, amount INTEGER, description TEXT, notes TEXT, date INTEGER,
        financial_id TEXT, type TEXT, location TEXT, error TEXT, imported_description TEXT,
        starting_balance_flag INTEGER DEFAULT 0, transferred_id TEXT, sort_order REAL,
        tombstone INTEGER DEFAULT 0, cleared INTEGER, pending INTEGER, parent_id TEXT,
        schedule TEXT, reconciled INTEGER, raw_synced_data TEXT);
    CREATE VIEW v_transactions AS SELECT id, notes FROM transactions;
    """)
    accounts = accounts if accounts is not None else [("a1", "Checking", 0, 0, 0, 999999)]
    for a in accounts:
        c.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,NULL,NULL)", a)
    c.executemany("INSERT INTO category_groups VALUES (?,?,?,?,0)",
                  [("g1", "Usual", 0, 0), ("g2", "Savings", 0, 0), ("gi", "Income", 1, 0)])
    cats = [("food", "Food", 0, "g1", 0), ("fun", "Fun", 0, "g1", 0), ("inv", "Invest", 0, "g2", 0),
            ("pay", "Pay", 1, "gi", 0), ("old", "OldFood", 0, "g1", 1)] + list(extra_cats)
    c.executemany("INSERT INTO categories VALUES (?,?,?,?,?,0)", cats)
    c.executemany("INSERT INTO category_mapping VALUES (?,?)",
                  [(k[0], k[0]) for k in cats if not k[4]] + [("old", "food")])
    c.execute("INSERT INTO payees VALUES ('p1', ?)", (CANARY,))
    for t in txns:
        row = {"isParent": 0, "isChild": 0, "tombstone": 0, "starting_balance_flag": 0,
               "transferred_id": None, "parent_id": None, "acct": "a1", "category": None}
        row.update(t)
        row.update(description="p1", notes=CANARY, imported_description=CANARY,
                   raw_synced_data=CANARY, financial_id=CANARY)
        cols = ",".join(row)
        c.execute(f"INSERT INTO transactions ({cols}) VALUES ({','.join('?' * len(row))})",
                  list(row.values()))
    c.commit()
    c.close()


BASE = [
    {"id": "t1", "category": "food", "amount": -55000, "date": 20261003},
    {"id": "t2", "category": "food", "amount": -1250, "date": 20261004},
    {"id": "t3", "category": "fun", "amount": -2000, "date": 20261005},
    {"id": "t4", "category": "inv", "amount": -10000, "date": 20261006},
    {"id": "t5", "category": "pay", "amount": 300000, "date": 20261007},
    {"id": "t6", "category": None, "amount": -700, "date": 20261008},
]


def by_cat(snap):
    return {(r["bucket"], r["category"]): r["amount"] for r in snap["money"]["flow"]}


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "db.sqlite"
    make_db(str(p), BASE)
    return p


def test_folder_zip_sqlite_same_snapshot(tmp_path, db):
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "db.sqlite").write_bytes(db.read_bytes())
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(db, "db.sqlite")
        zf.writestr("metadata.json", "{}")
    s1, _ = A.build_snapshot(folder)
    s2, _ = A.build_snapshot(z)
    s3, _ = A.build_snapshot(db)
    assert by_cat(s1) == by_cat(s2) == by_cat(s3)
    assert by_cat(s1) == {("Usual", "Food"): 562.5,
                          ("Usual", "Fun"): 20.0, ("Savings", "Invest"): 100.0,
                          ("Uncategorized", "Uncategorized"): 7.0}


def test_canary_never_appears(tmp_path, db):
    z = tmp_path / "e.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(db, "db.sqlite")
    for p in (db, z):
        snap, rep = A.build_snapshot(p)
        blob = json.dumps(snap) + json.dumps(rep)
        assert CANARY not in blob and "p1" not in json.dumps(snap)


def test_whitelist_statements(db):
    seen = []
    with A.open_export(db) as (conn, saved):
        conn.set_trace_callback(seen.append)
        A.build_money_section(conn, saved)
    assert seen
    for s in seen:
        low = s.lower()
        for bad in ("payee", "notes", "description", "imported", "raw_synced", "financial_id",
                    "location", "v_transactions", "*"):
            assert bad not in low, s
    for const in (A.SQL_TXNS, A.SQL_ACCOUNTS, A.SQL_CATEGORIES, A.SQL_GROUPS, A.SQL_MAPPING):
        assert "*" not in const and "payee" not in const.lower()


def test_exact_money(tmp_path):
    p = tmp_path / "db.sqlite"
    rows = [{"id": "t1", "category": "food", "amount": -55000, "date": 20261003},
            {"id": "t2", "category": "food", "amount": -1350, "date": 20261004}]
    rows += [{"id": f"c{i}", "category": "fun", "amount": -1, "date": 20261005} for i in range(1000)]
    make_db(str(p), rows)
    snap, _ = A.build_snapshot(p)
    assert by_cat(snap) == {("Usual", "Food"): 563.5, ("Usual", "Fun"): 10.0}


def test_exclusions_and_splits(tmp_path):
    p = tmp_path / "db.sqlite"
    rows = [
        {"id": "ok", "category": "food", "amount": -1000, "date": 20261003},
        {"id": "dead", "category": "food", "amount": -9999, "date": 20261003, "tombstone": 1},
        {"id": "par", "category": None, "amount": -3000, "date": 20261003, "isParent": 1},
        {"id": "ch1", "category": "food", "amount": -1000, "date": 20261003, "isChild": 1, "parent_id": "par"},
        {"id": "ch2", "category": "fun", "amount": -2000, "date": 20261003, "isChild": 1, "parent_id": "par"},
        {"id": "dpar", "category": None, "amount": -500, "date": 20261003, "isParent": 1, "tombstone": 1},
        {"id": "dch", "category": "fun", "amount": -500, "date": 20261003, "isChild": 1, "parent_id": "dpar"},
        {"id": "start", "category": None, "amount": 500000, "date": 20261003, "starting_balance_flag": 1},
        {"id": "inc", "category": "pay", "amount": 100000, "date": 20261003},
    ]
    make_db(str(p), rows)
    snap, rep = A.build_snapshot(p)
    assert by_cat(snap) == {("Usual", "Food"): 20.0, ("Usual", "Fun"): 20.0}
    assert rep["excluded_split_parents"] == 1
    assert rep["excluded_tombstoned"] == 3  # dead, dpar, dch
    assert rep["excluded_starting_balance"] == 1
    assert rep["excluded_income"] == 1
    assert rep["rows_counted"] == 3


def test_transfers(tmp_path):
    p = tmp_path / "db.sqlite"
    accts = [("a1", "Checking", 0, 0, 0, 0), ("a2", "Savings acct", 0, 0, 0, 0),
             ("o1", "Reserve", 1, 0, 0, 0)]
    rows = [
        {"id": "x1", "acct": "a1", "amount": -5000, "date": 20261003, "transferred_id": "x2"},
        {"id": "x2", "acct": "a2", "amount": 5000, "date": 20261003, "transferred_id": "x1"},
        {"id": "y1", "acct": "a1", "amount": -7000, "date": 20261003, "transferred_id": "y2"},
        {"id": "y2", "acct": "o1", "amount": 7000, "date": 20261003, "transferred_id": "y1"},
    ]
    make_db(str(p), rows, accounts=accts)
    snap, rep = A.build_snapshot(p)
    assert rep["excluded_transfers"] == 2
    assert by_cat(snap) == {("Uncategorized", "Uncategorized"): 70.0}
    assert snap["money"]["off_budget"] == [{"name": "Reserve", "balance": 70.0}]


def test_category_mapping(tmp_path):
    p = tmp_path / "db.sqlite"
    make_db(str(p), [{"id": "t1", "category": "old", "amount": -4000, "date": 20261003},
                     {"id": "t2", "category": "food", "amount": -1000, "date": 20261003}])
    snap, _ = A.build_snapshot(p)
    assert by_cat(snap) == {("Usual", "Food"): 50.0}


def test_uncategorized_and_net_refund(tmp_path):
    p = tmp_path / "db.sqlite"
    make_db(str(p), [{"id": "t1", "category": None, "amount": -1500, "date": 20261003},
                     {"id": "t2", "category": "food", "amount": -1000, "date": 20261003},
                     {"id": "t3", "category": "food", "amount": 3000, "date": 20261004},
                     {"id": "t4", "category": "fun", "amount": -1000, "date": 20261004},
                     {"id": "t5", "category": "inv", "amount": -1000, "date": 20261004},
                     {"id": "t6", "category": "inv", "amount": 1000, "date": 20261004}])
    snap, rep = A.build_snapshot(p)
    assert by_cat(snap) == {("Uncategorized", "Uncategorized"): 15.0, ("Usual", "Fun"): 10.0}
    assert rep["categories_omitted_net_refund"] == 2
    assert all(r["amount"] > 0 for r in snap["money"]["flow"])
    assert rep["uncategorized_rows"] == 1


def test_off_budget_balances(tmp_path):
    p = tmp_path / "db.sqlite"
    accts = [("a1", "Checking", 0, 0, 0, 0), ("o1", "Emergency", 1, 0, 0, 123456789),
             ("o2", "Closed one", 1, 1, 0, 0), ("o3", "Deleted one", 1, 0, 1, 0)]
    rows = [{"id": "r1", "acct": "o1", "amount": 100000, "date": 20260901},
            {"id": "r2", "acct": "o1", "amount": 2550, "date": 20261001},
            {"id": "r3", "acct": "o1", "amount": 9999, "date": 20261001, "tombstone": 1},
            {"id": "r4", "acct": "o2", "amount": 500, "date": 20261001},
            {"id": "r5", "acct": "a1", "category": "food", "amount": -100, "date": 20261001}]
    make_db(str(p), rows, accounts=accts)
    snap, rep = A.build_snapshot(p)
    assert snap["money"]["off_budget"] == [{"name": "Emergency", "balance": 1025.5}]
    assert rep["offbudget_accounts"] == 1


def test_no_off_budget(db):
    snap, rep = A.build_snapshot(db)
    assert snap["money"]["off_budget"] == []
    assert rep["offbudget_accounts"] == 0


def test_month_selection(tmp_path):
    p = tmp_path / "db.sqlite"
    make_db(str(p), [{"id": "t1", "category": "food", "amount": -1000, "date": 20260915},
                     {"id": "t2", "category": "food", "amount": -2000, "date": 20261003}])
    snap, rep = A.build_snapshot(p)
    assert snap["money"]["period"] == "2026-10" and by_cat(snap) == {("Usual", "Food"): 20.0}
    snap, rep = A.build_snapshot(p, month="2026-09")
    assert by_cat(snap) == {("Usual", "Food"): 10.0}
    assert rep["months_available"] == ["2026-09", "2026-10"]
    with pytest.raises(A.ExportError) as e:
        A.build_snapshot(p, month="2026-08")
    assert "2026-09" in str(e.value) and "2026-10" in str(e.value)


def test_as_of_detroit_date(db):
    # 2026-10-10 02:30 UTC is still 2026-10-09 in Detroit (EDT, UTC-4)
    ts = datetime(2026, 10, 10, 2, 30, tzinfo=timezone.utc).timestamp()
    os.utime(db, (ts, ts))
    snap, _ = A.build_snapshot(db)
    assert snap["money"]["as_of"] == "2026-10-09"


def test_as_of_zip_member_timestamp(tmp_path, db):
    z = tmp_path / "e.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zi = zipfile.ZipInfo("db.sqlite", date_time=(2026, 10, 9, 23, 30, 0))
        zf.writestr(zi, db.read_bytes())
    snap, _ = A.build_snapshot(z)
    assert snap["money"]["as_of"] == "2026-10-09"


def test_source_untouched_and_no_temp_left(tmp_path, db, monkeypatch):
    tdir = tmp_path / "tmproot"
    tdir.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tdir))
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    A.build_snapshot(db)
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert list(tdir.iterdir()) == []
    with pytest.raises(A.ExportError):
        A.build_snapshot(db, month="1999-01")
    assert list(tdir.iterdir()) == []


def test_errors(tmp_path):
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"this is not sqlite at all" * 50)
    with pytest.raises(A.ExportError):
        A.build_snapshot(junk)
    nodb = tmp_path / "nodb.zip"
    with zipfile.ZipFile(nodb, "w") as zf:
        zf.writestr("metadata.json", "{}")
    with pytest.raises(A.ExportError):
        A.build_snapshot(nodb)
    trav = tmp_path / "trav.zip"
    with zipfile.ZipFile(trav, "w") as zf:
        zf.writestr("../db.sqlite", "x")
        zf.writestr("sub/db.sqlite", "x")
    with pytest.raises(A.ExportError):
        A.build_snapshot(trav)
    notxn = tmp_path / "notxn.sqlite"
    c = sqlite3.connect(notxn)
    c.execute("CREATE TABLE accounts (id TEXT)")
    c.commit()
    c.close()
    with pytest.raises(A.ExportError) as e:
        A.build_snapshot(notxn)
    assert "transactions" in str(e.value)
    with pytest.raises(A.ExportError):
        A.build_snapshot(tmp_path / "missing.sqlite")


def test_final_gate_rejects_forbidden_key(db, monkeypatch):
    real = A.build_money_section

    def bad(conn, saved, month=None):
        sec, rep = real(conn, saved, month)
        sec["merchant"] = "x"
        return sec, rep
    monkeypatch.setattr(A, "build_money_section", bad)
    with pytest.raises(A.ExportError):
        A.build_snapshot(db)


def test_snapshot_valid_and_through_finance_model(db):
    snap, _ = A.build_snapshot(db)
    assert list(snap) == ["money"]
    personal_snapshots.validate_snapshot(snap)
    fm = pytest.importorskip("finance_model")
    now = datetime.now(timezone.utc)
    view = fm.build_finance_view([{"received_at": now.isoformat(), **snap}], now)
    money = view["money"] if isinstance(view, dict) and "money" in view else view
    totals = {b["bucket"]: b["total"] for b in money["buckets"]}
    assert totals == {"Usual": 582.5, "Savings": 100.0, "Uncategorized": 7.0}


@pytest.mark.skipif(not os.environ.get("ACTUAL_REAL_EXPORT"), reason="no real export")
def test_real_export_structure():
    snap, rep = A.build_snapshot(os.environ["ACTUAL_REAL_EXPORT"])
    sec = snap["money"]
    assert set(sec) == {"as_of", "period", "flow", "off_budget"}
    assert all(isinstance(r["amount"], float) and r["amount"] > 0 for r in sec["flow"])


# ---- review-fix tests ----

def test_text_amount_is_export_error(tmp_path):
    p = tmp_path / "db.sqlite"
    make_db(str(p), [{"id": "t1", "category": "food", "amount": "12abc", "date": 20261003}])
    with pytest.raises(A.ExportError):
        A.build_snapshot(p)


def test_text_amount_in_other_month_is_ignored(tmp_path):
    p = tmp_path / "db.sqlite"
    make_db(str(p), [{"id": "t1", "category": "food", "amount": "junk", "date": 20260801},
                     {"id": "t2", "category": "food", "amount": -1000, "date": 20261003}])
    snap, _ = A.build_snapshot(p)
    assert by_cat(snap) == {("Usual", "Food"): 10.0}


def test_text_amount_in_off_budget_sum(tmp_path):
    p = tmp_path / "db.sqlite"
    accts = [("a1", "Checking", 0, 0, 0, 0), ("o1", "Reserve", 1, 0, 0, 0)]
    make_db(str(p), BASE + [{"id": "z", "acct": "o1", "amount": b"\xff\xfe", "date": 20260101}],
            accounts=accts)
    with pytest.raises(A.ExportError):
        A.build_snapshot(p)


def test_zero_dos_timestamp_falls_back(tmp_path, db):
    z = tmp_path / "e.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(zipfile.ZipInfo("db.sqlite", date_time=(2026, 10, 9, 12, 0, 0)), db.read_bytes())
    # corrupt the central-directory DOS date to month 0 / day 0
    raw = bytearray(z.read_bytes())
    ts = raw.find(b"PK\x01\x02")
    raw[ts + 14:ts + 16] = (0).to_bytes(2, "little")      # DOS date 0 -> month 0, day 0
    z.write_bytes(bytes(raw))
    snap, _ = A.build_snapshot(z)
    expect = datetime.fromtimestamp(z.stat().st_mtime, timezone.utc).astimezone(
        ZoneInfo("America/Detroit")).strftime("%Y-%m-%d")
    assert snap["money"]["as_of"] == expect


def test_missing_whitelisted_column(tmp_path):
    p = tmp_path / "db.sqlite"
    c = sqlite3.connect(p)
    for t, cols in A._REQUIRED_COLS.items():
        use = [x for x in cols if not (t == "transactions" and x == "amount")]
        c.execute(f"CREATE TABLE {t} ({', '.join(use)})")
    c.commit()
    c.close()
    with pytest.raises(A.ExportError) as e:
        A.build_snapshot(p)
    assert "amount" in str(e.value)


def test_wal_sibling_is_copied(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    p = src / "db.sqlite"
    make_db(str(p), BASE[:1])
    w = sqlite3.connect(p)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("INSERT INTO transactions (id, acct, category, amount, date, tombstone, isParent, "
              "isChild, starting_balance_flag) VALUES ('walrow','a1','fun',-4200,20261009,0,0,0,0)")
    w.commit()
    assert (src / "db.sqlite-wal").exists()
    keep = tmp_path / "keep"
    keep.mkdir()
    for f in src.iterdir():            # snapshot of the files while the writer is still open
        (keep / f.name).write_bytes(f.read_bytes())
    w.close()
    snap, _ = A.build_snapshot(keep)
    assert by_cat(snap).get(("Usual", "Fun")) == 42.0


def test_unassigned_positive_does_not_net_uncategorized(tmp_path):
    p = tmp_path / "db.sqlite"
    make_db(str(p), [{"id": "t1", "category": None, "amount": -1500, "date": 20261003},
                     {"id": "t2", "category": "gone", "amount": 1500, "date": 20261004},
                     {"id": "t3", "category": None, "amount": 900, "date": 20261004}],
            extra_cats=[("gone", "Gone", 0, "g1", 1)])
    snap, rep = A.build_snapshot(p)
    assert by_cat(snap) == {("Uncategorized", "Uncategorized"): 15.0}
    assert rep["excluded_unassigned_positive"] == 2


def test_oversized_member(tmp_path, db, monkeypatch):
    monkeypatch.setattr(A, "MAX_DB_BYTES", 1000)
    z = tmp_path / "e.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(db, "db.sqlite")
    with pytest.raises(A.ExportError):
        A.build_snapshot(z)
    with pytest.raises(A.ExportError):
        A.build_snapshot(db)


def test_trace_covers_check_db(db, monkeypatch):
    seen = []
    monkeypatch.setattr(A, "_TRACE", seen.append)
    with A.open_export(db):
        pass
    assert seen
    for s in seen:
        low = s.lower()
        for bad in ("payee", "notes", "description", "imported", "raw_synced", "financial_id",
                    "location", "v_transactions", "*"):
            assert bad not in low, s


def test_body_oserror_is_not_wrapped(db):
    with pytest.raises(OSError):
        with A.open_export(db):
            raise OSError("caller bug")
