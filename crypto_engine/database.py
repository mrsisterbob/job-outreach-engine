"""SQLite persistence layer for the Crypto Alert & Paper-Trading Engine.

WAL mode + busy_timeout keep the Flask request thread, the APScheduler background thread,
and the reconciliation loop safely concurrent. All writes acquire SQLite's write lock
up-front via BEGIN IMMEDIATE (see get_db_conn(immediate=True)) instead of a Python-level lock.
"""
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto_engine.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    donchian_high REAL,
    donchian_low REAL,
    atr_14 REAL,
    rvol_ratio REAL,
    open_interest REAL,
    funding_rate_daily_equiv REAL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_ticks_lookup ON market_ticks(symbol, timeframe, fetched_at DESC);

CREATE TABLE IF NOT EXISTS open_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    confidence_score INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    rvol_ratio REAL,
    context_summary TEXT,
    reasons_json TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN',
    triggered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_open_signals_lookup ON open_signals(symbol, status, triggered_at DESC);

CREATE TABLE IF NOT EXISTS paper_portfolio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'LONG',
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    risk_pct REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    signal_id INTEGER,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    close_price REAL,
    pnl_pct REAL,
    pnl_usd REAL,
    FOREIGN KEY (signal_id) REFERENCES open_signals(id)
);
CREATE INDEX IF NOT EXISTS idx_paper_portfolio_status ON paper_portfolio(status);

CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_db_conn(immediate: bool = False):
    """Yields a raw sqlite3 connection in manual-commit mode with WAL + busy_timeout set.
    Pass immediate=True to wrap the block in BEGIN IMMEDIATE/COMMIT, which acquires SQLite's
    write lock up-front for safe concurrent writes across threads (Flask + APScheduler jobs).
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE;")
        yield conn
        if immediate:
            conn.execute("COMMIT;")
    except Exception:
        if immediate:
            conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db_conn() as conn:
        conn.executescript(_SCHEMA_SQL)
        now = utcnow_iso()
        conn.execute(
            "INSERT OR IGNORE INTO system_config (key, value, updated_at) VALUES ('virtual_balance_usd', '10000', ?)",
            (now,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO system_config (key, value, updated_at) VALUES ('default_risk_pct', '2.0', ?)",
            (now,),
        )
    logging.info("Crypto engine database schema ready (WAL mode, busy_timeout=5000ms).")


# ---------------------------------------------------------------------------
# market_ticks
# ---------------------------------------------------------------------------
def insert_market_tick(
    symbol: str, timeframe: str, open_=None, high=None, low=None, close=None, volume=None,
    donchian_high=None, donchian_low=None, atr_14=None, rvol_ratio=None,
    open_interest=None, funding_rate_daily_equiv=None,
) -> int:
    with get_db_conn(immediate=True) as conn:
        cursor = conn.execute(
            """INSERT INTO market_ticks
               (symbol, timeframe, open, high, low, close, volume, donchian_high, donchian_low,
                atr_14, rvol_ratio, open_interest, funding_rate_daily_equiv, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, timeframe, open_, high, low, close, volume, donchian_high, donchian_low,
             atr_14, rvol_ratio, open_interest, funding_rate_daily_equiv, utcnow_iso()),
        )
        return cursor.lastrowid


def get_recent_ticks(symbol: str, timeframe: str, since_iso: str = None, limit: int = 100) -> list[dict]:
    with get_db_conn() as conn:
        if since_iso:
            rows = conn.execute(
                """SELECT * FROM market_ticks WHERE symbol = ? AND timeframe = ? AND fetched_at >= ?
                   ORDER BY fetched_at ASC LIMIT ?""",
                (symbol, timeframe, since_iso, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        rows = conn.execute(
            """SELECT * FROM market_ticks WHERE symbol = ? AND timeframe = ?
               ORDER BY fetched_at DESC LIMIT ?""",
            (symbol, timeframe, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# open_signals
# ---------------------------------------------------------------------------
def create_open_signal(signal: dict) -> int:
    with get_db_conn(immediate=True) as conn:
        cursor = conn.execute(
            """INSERT INTO open_signals
               (symbol, asset_class, confidence_score, entry_price, stop_loss, take_profit,
                rvol_ratio, context_summary, reasons_json, status, triggered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
            (
                signal["symbol"], signal["asset_class"], signal["confidence_score"],
                signal["current_price"], signal["stop_loss"], signal["take_profit"],
                signal.get("rvol_ratio"), signal.get("context_summary"),
                json.dumps(signal.get("reasons", [])), utcnow_iso(),
            ),
        )
        return cursor.lastrowid


def get_latest_open_signal(symbol: str, max_age_minutes: int = 60) -> dict | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    with get_db_conn() as conn:
        row = conn.execute(
            """SELECT * FROM open_signals
               WHERE symbol = ? AND status = 'OPEN' AND triggered_at >= ?
               ORDER BY triggered_at DESC LIMIT 1""",
            (symbol, cutoff),
        ).fetchone()
        return dict(row) if row else None


def mark_signal_converted(signal_id: int) -> None:
    with get_db_conn(immediate=True) as conn:
        conn.execute("UPDATE open_signals SET status = 'CONVERTED' WHERE id = ?", (signal_id,))


# ---------------------------------------------------------------------------
# paper_portfolio
# ---------------------------------------------------------------------------
def insert_paper_trade(
    symbol: str, side: str, entry_price: float, quantity: float, risk_pct: float,
    stop_loss: float, take_profit: float, signal_id: int | None = None,
) -> int:
    with get_db_conn(immediate=True) as conn:
        cursor = conn.execute(
            """INSERT INTO paper_portfolio
               (symbol, side, entry_price, quantity, risk_pct, stop_loss, take_profit,
                status, signal_id, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)""",
            (symbol, side, entry_price, quantity, risk_pct, stop_loss, take_profit, signal_id, utcnow_iso()),
        )
        trade_id = cursor.lastrowid
    if signal_id:
        mark_signal_converted(signal_id)
    return trade_id


def get_open_paper_trades() -> list[dict]:
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM paper_portfolio WHERE status = 'OPEN' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def close_paper_trade(trade_id: int, close_price: float, status: str, pnl_pct: float, pnl_usd: float) -> bool:
    """Closes a trade and compounds the realized PnL into virtual_balance_usd atomically
    (same transaction), so position sizing on future trades reflects real performance.
    Returns False (no-op) if the trade was already closed by a concurrent reconciliation pass.
    """
    if status not in ("CLOSED_TP", "CLOSED_SL", "CLOSED_MANUAL"):
        raise ValueError(f"Invalid close status '{status}'")
    now = utcnow_iso()
    with get_db_conn(immediate=True) as conn:
        cursor = conn.execute(
            """UPDATE paper_portfolio SET status = ?, close_price = ?, pnl_pct = ?, pnl_usd = ?, closed_at = ?
               WHERE id = ? AND status = 'OPEN'""",
            (status, close_price, pnl_pct, pnl_usd, now, trade_id),
        )
        if cursor.rowcount == 0:
            return False
        balance_row = conn.execute(
            "SELECT value FROM system_config WHERE key = 'virtual_balance_usd'"
        ).fetchone()
        current_balance = float(balance_row["value"]) if balance_row else 10000.0
        new_balance = current_balance + pnl_usd
        conn.execute(
            """INSERT INTO system_config (key, value, updated_at) VALUES ('virtual_balance_usd', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (str(new_balance), now),
        )
    return True


def get_closed_trade_stats() -> dict:
    with get_db_conn() as conn:
        row = conn.execute(
            """SELECT
                   COUNT(*) AS total_closed,
                   COALESCE(SUM(pnl_usd), 0.0) AS realized_pnl_usd,
                   COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END), 0) AS losses
               FROM paper_portfolio WHERE status IN ('CLOSED_TP', 'CLOSED_SL', 'CLOSED_MANUAL')"""
        ).fetchone()
    total_closed = row["total_closed"] or 0
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    win_rate_pct = (wins / total_closed * 100.0) if total_closed else 0.0
    return {
        "total_closed": total_closed,
        "realized_pnl_usd": row["realized_pnl_usd"] or 0.0,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate_pct,
    }


# ---------------------------------------------------------------------------
# system_config
# ---------------------------------------------------------------------------
def get_config(key: str, default=None):
    with get_db_conn() as conn:
        row = conn.execute("SELECT value FROM system_config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value) -> None:
    with get_db_conn(immediate=True) as conn:
        conn.execute(
            """INSERT INTO system_config (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, str(value), utcnow_iso()),
        )
