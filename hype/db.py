"""SQLite persistence layer.

Phase 1 defines the schema only; later phases populate it:
  - traders          (§5.1, §5.7)  ranking + investigation state machine
  - sessions         (§5.10)       per-mode starting-balance baseline
  - positions        (§5.4–5.6)    open/closed copy trades + TP/SL
  - trades           (§5.4)        individual buy/sell fills (ledger)
  - equity_snapshots (§7.2)        annotated equity curve points
  - activity_log     (§7.5)        real-time action feed

Design notes:
  - All money is stored in USD floats for reporting; on-chain amounts (qty,
    SOL) are stored alongside where relevant.
  - `mode` ('paper' | 'live') is carried on every row so paper and live are
    tracked completely separately (§11).
  - Schema is portable standard SQLite — no macOS specifics.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from . import paths

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Followed traders + investigation state machine (§5.1, §5.7).
CREATE TABLE IF NOT EXISTS traders (
    wallet_address        TEXT PRIMARY KEY,
    label                 TEXT,
    label_auto            INTEGER NOT NULL DEFAULT 1,        -- 1 = auto-name (safe to regen), 0 = owner-set
    source                TEXT NOT NULL DEFAULT 'ranked',   -- 'ranked' | 'manual'
    active                INTEGER NOT NULL DEFAULT 1,        -- 0/1; copying enabled
    -- External stats from Birdeye (informational ranking):
    realized_pnl_usd      REAL,
    win_rate              REAL,
    trade_count_external  INTEGER,
    last_active_ts        TEXT,
    -- Hype's own record of copied outcomes (drives investigation):
    copied_wins           INTEGER NOT NULL DEFAULT 0,
    copied_losses         INTEGER NOT NULL DEFAULT 0,
    copied_pnl_usd        REAL NOT NULL DEFAULT 0,     -- running copied P&L (resets with a fresh life)
    consecutive_losses    INTEGER NOT NULL DEFAULT 0,
    grace_trades_done     INTEGER NOT NULL DEFAULT 0,
    -- State machine: 'active' | 'under_investigation' | 'probation'
    --                | 'paused' | 'dropped'
    investigation_state   TEXT NOT NULL DEFAULT 'active',
    investigation_level   INTEGER NOT NULL DEFAULT 0,        -- badge: 0,2,3,...
    probation_results     TEXT,                              -- JSON list of recent W/L
    created_ts            TEXT NOT NULL,
    updated_ts            TEXT NOT NULL
);

-- Per-mode trading session, records the starting-balance baseline (§5.10).
CREATE TABLE IF NOT EXISTS sessions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    mode                  TEXT NOT NULL,                     -- 'paper' | 'live'
    started_ts            TEXT NOT NULL,
    starting_balance_usd  REAL NOT NULL,
    ended_ts              TEXT,
    note                  TEXT
);

-- Copy positions: one row per token position Hype opens (§5.4–5.6).
CREATE TABLE IF NOT EXISTS positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER REFERENCES sessions(id),
    mode              TEXT NOT NULL,                         -- 'paper' | 'live'
    token_mint        TEXT NOT NULL,
    token_symbol      TEXT,
    trigger_trader    TEXT REFERENCES traders(wallet_address),
    status            TEXT NOT NULL DEFAULT 'open',          -- 'open' | 'closed'
    -- Entry:
    entry_price       REAL,
    entry_qty         REAL,                                  -- token units
    entry_amount_usd  REAL,
    opened_ts         TEXT NOT NULL,
    tx_open           TEXT,                                  -- signature (live)
    -- Risk params snapshotted at entry (so live edits don't rewrite history):
    tp_pct            REAL,
    sl_pct            REAL,
    -- Live tracking:
    current_price     REAL,
    peak_price        REAL,                                  -- highest mark seen (for trailing take-profit)
    -- Exit:
    exit_price        REAL,
    exit_reason       TEXT,                                  -- 'tp' | 'sl' | 'manual'
    realized_pnl_usd  REAL,
    realized_pnl_pct  REAL,
    closed_ts         TEXT,
    tx_close          TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, mode);
CREATE INDEX IF NOT EXISTS idx_positions_trader ON positions(trigger_trader);

-- Individual fills ledger (buy/sell), incl. modeled fees/slippage (§5.4).
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id   INTEGER REFERENCES positions(id),
    mode          TEXT NOT NULL,
    side          TEXT NOT NULL,                             -- 'buy' | 'sell'
    token_mint    TEXT NOT NULL,
    qty           REAL,
    price         REAL,
    usd_value     REAL,
    fees_usd      REAL,
    slippage_bps  INTEGER,
    executor      TEXT,                                      -- 'paper' | 'live'
    tx_sig        TEXT,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id);

-- Equity-curve points for the annotated chart (§7.2).
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER REFERENCES sessions(id),
    mode             TEXT NOT NULL,
    ts               TEXT NOT NULL,
    balance_usd      REAL,
    realized_pnl_usd REAL,
    unrealized_pnl_usd REAL,
    open_positions   INTEGER,
    marker           TEXT,                                   -- 'buy' | 'sell' | NULL
    trigger_trader   TEXT,                                   -- for color-coding markers
    note             TEXT                                    -- annotation text
);
CREATE INDEX IF NOT EXISTS idx_equity_mode_ts ON equity_snapshots(mode, ts);

-- Real-time action feed (§7.5). Also written to by the logging layer.
CREATE TABLE IF NOT EXISTS activity_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,                                 -- INFO | WARNING | ERROR ...
    category  TEXT NOT NULL,                                 -- scan | buy | sell | tp | sl ...
    message   TEXT NOT NULL,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts);
CREATE INDEX IF NOT EXISTS idx_activity_category ON activity_log(category);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection with sensible defaults (Row factory, FK on)."""
    db = path or paths.db_path()
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: Optional[sqlite3.Connection] = None) -> sqlite3.Connection:
    """Create all tables if they don't exist and stamp the schema version."""
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent migrations for DBs created before a column existed.
    (SQLite lacks ADD COLUMN IF NOT EXISTS, so we check pragma first.)"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(traders)").fetchall()}
    if "label_auto" not in cols:
        # Existing rows only carry auto-generated names so far → mark them auto
        # (default 1), so a nickname-format change refreshes them once.
        conn.execute("ALTER TABLE traders ADD COLUMN label_auto INTEGER NOT NULL DEFAULT 1")
    if "copied_pnl_usd" not in cols:
        # Running copied P&L per trader — drives the P&L/rug-aware investigation.
        conn.execute("ALTER TABLE traders ADD COLUMN copied_pnl_usd REAL NOT NULL DEFAULT 0")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
    if "peak_price" not in pcols:
        # Highest mark seen per position — powers the trailing take-profit.
        conn.execute("ALTER TABLE positions ADD COLUMN peak_price REAL")


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]
