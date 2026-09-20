"""Account, sessions, and equity tracking (§5.10, §7.2).

A *session* records the starting-balance baseline when a mode is entered
(paper = paper_starting_balance_usd; live = real wallet balance later). Cash and
equity are derived from the trade ledger so they can never drift from history:

  cash   = starting_balance − Σ(buy spend) + Σ(sell proceeds)   [this session]
  equity = cash + Σ(open position qty × current price)

Every buy/sell writes an equity_snapshot (with a marker + trigger trader) so the
dashboard can draw the annotated equity curve.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..logging_setup import utcnow_iso
from .pricing import PriceFeed


@dataclass(slots=True)
class Equity:
    starting_balance_usd: float
    cash_usd: float
    open_value_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    open_positions: int

    @property
    def equity_usd(self) -> float:
        return self.cash_usd + self.open_value_usd

    @property
    def total_pnl_usd(self) -> float:
        return self.equity_usd - self.starting_balance_usd


class Account:
    def __init__(self, conn: sqlite3.Connection, mode: str) -> None:
        self.conn = conn
        self.mode = mode

    # --- sessions ------------------------------------------------------------

    def start_session(self, starting_balance_usd: float, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions(mode, started_ts, starting_balance_usd, note) VALUES(?,?,?,?)",
            (self.mode, utcnow_iso(), starting_balance_usd, note),
        )
        self.conn.commit()
        return cur.lastrowid

    def active_session(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE mode=? AND ended_ts IS NULL ORDER BY id DESC LIMIT 1",
            (self.mode,),
        ).fetchone()

    def get_or_start_session(self, starting_balance_usd: float) -> int:
        row = self.active_session()
        if row:
            return row["id"]
        return self.start_session(starting_balance_usd)

    def reset_session(self, starting_balance_usd: float) -> int:
        """End the active session and start a fresh one with a new starting
        balance (used to reset/set the paper balance). Old positions stay under
        the ended session as history; the UI/engine use the active session."""
        self.conn.execute(
            "UPDATE sessions SET ended_ts=? WHERE mode=? AND ended_ts IS NULL",
            (utcnow_iso(), self.mode))
        sid = self.start_session(starting_balance_usd, note="reset")
        self.conn.commit()
        return sid

    def starting_balance(self, session_id: int) -> float:
        row = self.conn.execute(
            "SELECT starting_balance_usd FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return float(row["starting_balance_usd"]) if row else 0.0

    # --- balances ------------------------------------------------------------

    def cash(self, session_id: int) -> float:
        start = self.starting_balance(session_id)
        row = self.conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN side='buy'  THEN usd_value END),0) AS spent,
                 COALESCE(SUM(CASE WHEN side='sell' THEN usd_value END),0) AS proceeds
               FROM trades t
               JOIN positions p ON p.id = t.position_id
               WHERE p.session_id = ?""",
            (session_id,),
        ).fetchone()
        return start - float(row["spent"]) + float(row["proceeds"])

    def realized_pnl(self, session_id: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd),0) AS r FROM positions "
            "WHERE session_id=? AND status='closed'",
            (session_id,),
        ).fetchone()
        return float(row["r"])

    def open_positions(self, session_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM positions WHERE session_id=? AND status='open'", (session_id,)
        ).fetchall()

    def equity(self, session_id: int, price_feed: PriceFeed) -> Equity:
        cash = self.cash(session_id)
        opens = self.open_positions(session_id)
        open_value = 0.0
        unreal = 0.0
        for p in opens:
            price = price_feed.get_price_usd(p["token_mint"])
            if price is None:
                # fall back to last known entry value if price unavailable
                price = p["entry_price"] or 0.0
            value = (p["entry_qty"] or 0.0) * price
            open_value += value
            unreal += value - (p["entry_amount_usd"] or 0.0)
        return Equity(
            starting_balance_usd=self.starting_balance(session_id),
            cash_usd=cash,
            open_value_usd=open_value,
            realized_pnl_usd=self.realized_pnl(session_id),
            unrealized_pnl_usd=unreal,
            open_positions=len(opens),
        )

    def equity_stored(self, session_id: int) -> Equity:
        """Equity computed from the DB's last-known prices (positions.current_price,
        updated by the engine each manage cycle). Lets the UI read live equity
        without its own price feed."""
        cash = self.cash(session_id)
        opens = self.open_positions(session_id)
        open_value = 0.0
        unreal = 0.0
        for p in opens:
            price = p["current_price"] or p["entry_price"] or 0.0
            value = (p["entry_qty"] or 0.0) * price
            open_value += value
            unreal += value - (p["entry_amount_usd"] or 0.0)
        return Equity(
            starting_balance_usd=self.starting_balance(session_id),
            cash_usd=cash, open_value_usd=open_value,
            realized_pnl_usd=self.realized_pnl(session_id),
            unrealized_pnl_usd=unreal, open_positions=len(opens),
        )

    # --- equity curve --------------------------------------------------------

    def snapshot(self, session_id: int, eq: Equity, *, marker: Optional[str] = None,
                 trigger_trader: Optional[str] = None, note: Optional[str] = None) -> None:
        self.conn.execute(
            """INSERT INTO equity_snapshots
               (session_id, mode, ts, balance_usd, realized_pnl_usd, unrealized_pnl_usd,
                open_positions, marker, trigger_trader, note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (session_id, self.mode, utcnow_iso(), eq.equity_usd, eq.realized_pnl_usd,
             eq.unrealized_pnl_usd, eq.open_positions, marker, trigger_trader, note),
        )
        self.conn.commit()
