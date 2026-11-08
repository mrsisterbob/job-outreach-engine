"""Actual Budget export -> the `money` section of the personal snapshot.

Reads a budget folder, an export/backup zip or a bare db.sqlite, always on a COPY in a temp
directory opened read-only. Only a fixed whitelist of columns is ever selected (see the SQL_*
constants): no payee, description, note, imported text or other merchant-level column is read,
so none can be sent. Pure stdlib plus personal_snapshots.validate_snapshot as the final gate.

as_of is the SAVE date of the export file, not the newest transaction date: it is when these
numbers were last true. report["newest_txn_date"] is returned so the caller can warn when the
two differ widely. The report holds counts and dates only, never an amount or a name.
newest_txn_date is the newest COUNTED row in the chosen month (rows_counted only), by spec.

Rows in off-budget accounts are never spend: they are excluded from flow (counted in
excluded_offbudget_rows) and only feed the off-budget balance. A positive-amount row that
resolves to no live category is not a refund of "Uncategorized": it is set aside and counted
in excluded_unassigned_positive.
"""
import contextlib
import math
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import personal_snapshots

DETROIT = ZoneInfo("America/Detroit")
MAX_NAME = 60
UNCATEGORIZED = "Uncategorized"
_DB_MEMBER = "db.sqlite"
_REQUIRED_TABLES = ("transactions", "accounts", "categories", "category_groups", "category_mapping")

SQL_TABLES = "SELECT name FROM sqlite_master WHERE type = 'table'"
SQL_TXNS = ("SELECT id, acct, category, amount, date, isParent, isChild, parent_id, tombstone, "
            "transferred_id, starting_balance_flag FROM transactions")
SQL_ACCOUNTS = "SELECT id, name, offbudget, closed, tombstone FROM accounts"
SQL_CATEGORIES = "SELECT id, name, is_income, cat_group, tombstone FROM categories"
SQL_GROUPS = "SELECT id, name, is_income, tombstone FROM category_groups"
SQL_MAPPING = "SELECT id, transferId FROM category_mapping"

MAX_DB_BYTES = 512 * 1024 * 1024   # refuse a bigger db.sqlite rather than fill the disk
_TRACE = None                      # test hook: statement callback attached right after connect
_REQUIRED_COLS = {
    "transactions": ("id", "acct", "category", "amount", "date", "isParent", "isChild", "parent_id",
                     "tombstone", "transferred_id", "starting_balance_flag"),
    "accounts": ("id", "name", "offbudget", "closed", "tombstone"),
    "categories": ("id", "name", "is_income", "cat_group", "tombstone"),
    "category_groups": ("id", "name", "is_income", "tombstone"),
    "category_mapping": ("id", "transferId"),
}


class ExportError(Exception):
    """User-showable problem reading the export."""


def _name(v, default=UNCATEGORIZED):
    s = v.strip() if isinstance(v, str) else ""
    return (s or default)[:MAX_NAME]


def _check_db(path):
    conn = None
    try:
        conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
        if _TRACE is not None:
            conn.set_trace_callback(_TRACE)
        names = {r[0] for r in conn.execute(SQL_TABLES)}
        for t in _REQUIRED_TABLES:
            if t not in names:
                raise ExportError(f"the export has no '{t}' table")
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            for col in _REQUIRED_COLS[t]:
                if col not in have:
                    raise ExportError(f"the export's '{t}' table has no '{col}' column")
    except ExportError:
        if conn is not None:
            conn.close()
        raise
    except sqlite3.Error:
        if conn is not None:
            conn.close()
        raise ExportError("that file is not a readable Actual database")
    return conn


def _copy_limited(fin, dest):
    total = 0
    with open(dest, "wb") as fout:
        while True:
            chunk = fin.read(1024 * 1024)
            if not chunk:
                return
            total += len(chunk)
            if total > MAX_DB_BYTES:
                raise ExportError("db.sqlite is too large to read")
            fout.write(chunk)


def _copy_file(db, dest):
    if db.stat().st_size > MAX_DB_BYTES:
        raise ExportError("db.sqlite is too large to read")
    with open(db, "rb") as fin:
        _copy_limited(fin, dest)
    for suffix in ("-wal", "-shm"):   # unflushed rows live in the WAL; copy it alongside
        side = db.with_name(db.name + suffix)
        if side.is_file():
            if side.stat().st_size > MAX_DB_BYTES:
                raise ExportError("db.sqlite is too large to read")
            shutil.copyfile(side, dest + suffix)


def _materialize(src, dest, label):
    """Copy the database out of folder/zip/file into dest. Returns saved_at (aware UTC)."""
    try:
        if src.is_dir():
            db = src / _DB_MEMBER
            if not db.is_file():
                raise ExportError(f"no db.sqlite inside folder {label}")
            _copy_file(db, dest)
            return datetime.fromtimestamp(db.stat().st_mtime, timezone.utc)
        if src.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(src) as zf:
                    infos = [i for i in zf.infolist() if i.filename == _DB_MEMBER]
                    if len(infos) != 1:
                        raise ExportError(f"{label} must contain exactly one db.sqlite")
                    info = infos[0]
                    if info.file_size > MAX_DB_BYTES:
                        raise ExportError("db.sqlite is too large to read")
                    with zf.open(info) as fin:
                        _copy_limited(fin, dest)
            except zipfile.BadZipFile:
                raise ExportError(f"{label} is not a valid zip")
            try:
                return datetime(*info.date_time, tzinfo=DETROIT).astimezone(timezone.utc)
            except (ValueError, OverflowError):   # bad DOS date (month/day 0): use the zip's own mtime
                return datetime.fromtimestamp(src.stat().st_mtime, timezone.utc)
        _copy_file(src, dest)
        return datetime.fromtimestamp(src.stat().st_mtime, timezone.utc)
    except OSError:
        raise ExportError(f"could not read {label}")


@contextlib.contextmanager
def open_export(path):
    """Yield (read-only connection to a temp COPY, saved_at aware UTC). Cleans up always."""
    src = Path(str(path))
    label = src.name
    if not src.exists():
        raise ExportError(f"not found: {label}")
    tmp = tempfile.mkdtemp(prefix="actual_snap_")
    conn = None
    try:
        dest = os.path.join(tmp, "copy.sqlite")
        saved_at = _materialize(src, dest, label)
        conn = _check_db(dest)
        yield conn, saved_at
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _cents(v):
    """Integer cents or None (NULL). Anything non-numeric is a bad export, not a crash."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ExportError("the export has a non-numeric amount")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v == int(v):
        return int(v)
    raise ExportError("the export has a non-numeric amount")


def _month_of(date):
    try:
        d = int(date)
    except (TypeError, ValueError, OverflowError):
        return None
    y, m = d // 10000, (d // 100) % 100
    return f"{y:04d}-{m:02d}" if 1 <= m <= 12 and y > 0 else None


def build_money_section(conn, saved_at, month=None):
    """-> (section, report). See module docstring; counts only in report."""
    try:
        return _build_money_section(conn, saved_at, month)
    except sqlite3.Error as e:
        raise ExportError(f"export schema/read problem: {e}")


def _build_money_section(conn, saved_at, month):
    accounts = {r[0]: r for r in conn.execute(SQL_ACCOUNTS)}
    cats = {r[0]: r for r in conn.execute(SQL_CATEGORIES)}
    groups = {r[0]: r for r in conn.execute(SQL_GROUPS)}
    mapping = {r[0]: r[1] for r in conn.execute(SQL_MAPPING) if r[1]}
    keys = ("id", "acct", "category", "amount", "date", "is_parent", "is_child",
            "parent_id", "tombstone", "transferred_id", "starting")
    txns = [dict(zip(keys, r)) for r in conn.execute(SQL_TXNS)]
    by_id = {t["id"]: t for t in txns}

    rep = {k: 0 for k in ("rows_counted", "excluded_tombstoned", "excluded_split_parents",
                          "excluded_transfers", "excluded_starting_balance", "excluded_income",
                          "uncategorized_rows", "categories_sent", "categories_omitted_net_refund",
                          "offbudget_accounts", "excluded_null_amount", "excluded_offbudget_rows",
                          "excluded_unassigned_positive")}

    def live(t):
        if t["tombstone"]:
            return False
        a = accounts.get(t["acct"])
        if a is None or a[4]:
            return False
        if t["parent_id"] and t["is_child"]:
            p = by_id.get(t["parent_id"])
            if p is not None and p["tombstone"]:
                return False
        return True

    lives = [t for t in txns if live(t)]
    month_counts = {}
    for t in lives:
        if t["is_parent"]:
            continue
        m = _month_of(t["date"])
        if m:
            month_counts[m] = month_counts.get(m, 0) + 1
    months = sorted(month_counts)
    rep["months_available"] = months
    if not months:
        raise ExportError("the export has no live transactions")
    if month is None:
        month = months[-1]
    elif month not in month_counts:
        raise ExportError(f"no transactions in {month}; months available: {', '.join(months)}")
    rep["period"] = month

    def resolve(cid):
        c = cats.get(mapping.get(cid, cid))
        return None if c is None or c[4] else c

    spend = {}
    newest = None
    for t in txns:
        if _month_of(t["date"]) != month:
            continue
        if not live(t):
            rep["excluded_tombstoned"] += 1
            continue
        if t["is_parent"]:
            rep["excluded_split_parents"] += 1
            continue
        if t["starting"] == 1:
            rep["excluded_starting_balance"] += 1
            continue
        if accounts[t["acct"]][2]:
            rep["excluded_offbudget_rows"] += 1
            continue
        amount = _cents(t["amount"])
        if amount is None:
            rep["excluded_null_amount"] += 1
            continue
        if t["transferred_id"]:
            other = by_id.get(t["transferred_id"])
            oa = accounts.get(other["acct"]) if other else None
            if oa is not None and not oa[2]:
                rep["excluded_transfers"] += 1
                continue
        c = resolve(t["category"]) if t["category"] else None
        if c is None:
            if amount > 0:   # money in with no category: not a refund of spending
                rep["excluded_unassigned_positive"] += 1
                continue
            cat_name, bucket = UNCATEGORIZED, UNCATEGORIZED
            rep["uncategorized_rows"] += 1
        else:
            g = groups.get(c[3])
            if c[2] or (g is not None and g[2]):
                rep["excluded_income"] += 1
                continue
            cat_name = _name(c[1])
            bucket = _name(g[1]) if g is not None and not g[3] else UNCATEGORIZED
        key = (cat_name, bucket)
        spend[key] = spend.get(key, 0) - amount
        rep["rows_counted"] += 1
        d = int(t["date"])
        newest = d if newest is None or d > newest else newest

    flow = []
    for (cat_name, bucket), cents in spend.items():
        if cents <= 0:
            rep["categories_omitted_net_refund"] += 1
            continue
        flow.append({"category": cat_name, "bucket": bucket, "amount": round(cents / 100, 2)})
    rep["categories_sent"] = len(flow)
    rep["newest_txn_date"] = (f"{newest // 10000:04d}-{(newest // 100) % 100:02d}-{newest % 100:02d}"
                              if newest else None)

    off = []
    for aid, a in accounts.items():
        if a[2] != 1 or a[4] or a[3]:
            continue
        cents = 0
        for t in lives:
            if t["acct"] == aid and not t["is_parent"]:
                cents += _cents(t["amount"]) or 0
        off.append({"name": _name(a[1], "Account"), "balance": round(cents / 100, 2)})
    rep["offbudget_accounts"] = len(off)

    section = {"as_of": saved_at.astimezone(DETROIT).strftime("%Y-%m-%d"), "period": month,
               "flow": flow, "off_budget": off}
    return section, rep


def build_snapshot(path, month=None):
    """-> ({"money": section}, report), gated by personal_snapshots.validate_snapshot."""
    with open_export(path) as (conn, saved_at):
        section, report = build_money_section(conn, saved_at, month)
    snapshot = {"money": section}
    try:
        personal_snapshots.validate_snapshot(snapshot)
    except personal_snapshots.SnapshotRejected as e:
        raise ExportError(f"snapshot refused: {e}")
    return snapshot, report
