"""Position manager — open/close positions and fire TP/SL (§5.6).

Opens a position from a Fill, persists it with its TP/SL snapshotted at entry
(so live config edits don't rewrite open trades), and evaluates open positions
against the current mark price to trigger take-profit / stop-loss. Selling the
full position goes back through the Executor.

A closed position yields a realized P&L and a win/loss outcome, which the engine
feeds to the investigation engine (§5.7).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..logging_setup import utcnow_iso
from ..models import BuySignal
from .executor import Executor, Fill
from .pricing import PriceFeed


@dataclass(slots=True)
class CloseResult:
    position_id: int
    token_mint: str
    trigger_trader: Optional[str]
    reason: str            # 'tp' | 'sl' | 'manual'
    realized_pnl_usd: float
    realized_pnl_pct: float
    win: bool


class PositionManager:
    def __init__(self, conn: sqlite3.Connection, executor: Executor, price_feed: PriceFeed) -> None:
        self.conn = conn
        self.executor = executor
        self.feed = price_feed

    # --- open ----------------------------------------------------------------

    def open_from_fill(self, session_id: int, signal: BuySignal, fill: Fill,
                       *, tp_pct: float, sl_pct: float, token_symbol: Optional[str] = None) -> int:
        ts = utcnow_iso()
        cur = self.conn.execute(
            """INSERT INTO positions
               (session_id, mode, token_mint, token_symbol, trigger_trader, status,
                entry_price, entry_qty, entry_amount_usd, opened_ts, tp_pct, sl_pct,
                current_price, peak_price)
               VALUES (?,?,?,?,?, 'open', ?,?,?,?,?,?,?,?)""",
            (session_id, self.executor.name, signal.token_mint, token_symbol,
             signal.trader_wallet, fill.price_usd, fill.qty, fill.usd_value, ts,
             tp_pct, sl_pct, fill.price_usd, fill.price_usd),
        )
        position_id = cur.lastrowid
        self._record_trade(position_id, fill, ts)
        self.conn.commit()
        return position_id

    def has_open_position(self, session_id: int, token_mint: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM positions WHERE session_id=? AND token_mint=? AND status='open' LIMIT 1",
            (session_id, token_mint),
        ).fetchone()
        return row is not None

    # --- evaluate / close ----------------------------------------------------

    def pnl_pct(self, position: sqlite3.Row, price: float) -> float:
        entry = position["entry_price"] or 0.0
        if entry <= 0:
            return 0.0
        return (price - entry) / entry * 100.0

    def plan_closes(self, session_id: int, cfg=None) -> list[tuple[sqlite3.Row, str]]:
        """Re-price open positions (persist the marks) and return which ones to
        close and why — WITHOUT selling. The caller runs the (slow, network)
        sells concurrently. Isolated per position so one bad mark can't stall the
        others.

        Trailing (per cfg.trading): once a position's PEAK profit reaches
        `trail_activate_pct`, sell as soon as the current profit falls
        `trail_gap_pct` below that peak."""
        from ..logging_setup import get_logger
        picks: list[tuple[sqlite3.Row, str]] = []
        opens = self.conn.execute(
            "SELECT * FROM positions WHERE session_id=? AND status='open'", (session_id,)
        ).fetchall()
        # Live: price each position by a REAL sell quote of its held size (below);
        # only warm the feed fallback when the executor can't do that.
        exit_price = getattr(self.executor, "position_price_usd", None)
        if exit_price is None:
            try:
                self.feed.prime([p["token_mint"] for p in opens])
            except Exception:  # noqa: BLE001 - pricing hiccup shouldn't stop the loop
                pass
        for p in opens:
            try:
                price = None
                if exit_price is not None:   # live: true realizable sell-side price
                    price = exit_price(p["token_mint"], p["entry_qty"] or 0.0)
                if price is None:            # paper, or no sell route (→ feed/rug)
                    price = self.feed.get_price_usd(p["token_mint"])
                if price is None:
                    continue
                entry = p["entry_price"] or 0.0
                peak = max(p["peak_price"] or entry, price)
                self.conn.execute("UPDATE positions SET current_price=?, peak_price=? WHERE id=?",
                                  (price, peak, p["id"]))
                self.conn.commit()
                pct = self.pnl_pct(p, price)
                reason = None
                if pct >= (p["tp_pct"] or 0):
                    reason = "tp"                       # hard take-profit ceiling
                elif pct <= (p["sl_pct"] or 0):
                    reason = "sl"                       # hard stop-loss
                elif cfg is not None and cfg.trading.trail_activate_pct > 0:
                    peak_pct = self.pnl_pct(p, peak)
                    if (peak_pct >= cfg.trading.trail_activate_pct
                            and pct <= peak_pct - cfg.trading.trail_gap_pct):
                        reason = "tp"                   # trailing take-profit
                if reason:
                    picks.append((p, reason))
            except Exception as e:  # noqa: BLE001 - never let one position stall the plan
                get_logger().debug("plan_closes error on #%s: %s", p["id"], e)
        return picks

    def evaluate(self, session_id: int, cfg=None) -> list[CloseResult]:
        """Sequential plan→close (paper/tests). Live uses plan_closes + concurrent
        execute_close in the engine so sells don't block each other."""
        from ..logging_setup import get_logger
        results: list[CloseResult] = []
        for p, reason in self.plan_closes(session_id, cfg):
            try:
                results.append(self.close(p, reason))
            except Exception as e:  # noqa: BLE001 - rug / no sell route
                get_logger().warning("close #%s (%s) failed: %s", p["id"], reason, e)
                price = self.feed.get_price_usd(p["token_mint"]) or p["current_price"] or 0.0
                entry = p["entry_price"] or 0.0
                pct = ((price - entry) / entry * 100.0) if entry else 0.0
                if pct <= -90.0:
                    wo = self.write_off(p, price)
                    if wo is not None:
                        results.append(wo)
        return results

    def write_off(self, position: sqlite3.Row, price: float) -> Optional[CloseResult]:
        """Mark a rugged/unsellable position closed at a total loss (its tokens
        are worthless and can't be swapped). Records the loss so the equity curve
        and trader-policing see it, and stops the endless failed-sell retries."""
        ts = utcnow_iso()
        cost = position["entry_amount_usd"] or 0.0
        pnl = -cost
        self.conn.execute(
            """UPDATE positions SET status='closed', exit_price=?, exit_reason='rug',
               realized_pnl_usd=?, realized_pnl_pct=?, closed_ts=?, current_price=?
               WHERE id=? AND status='open'""",
            (price, pnl, -100.0, ts, price, position["id"]))
        self.conn.commit()
        return CloseResult(
            position_id=position["id"], token_mint=position["token_mint"],
            trigger_trader=position["trigger_trader"], reason="rug",
            realized_pnl_usd=pnl, realized_pnl_pct=-100.0, win=False)

    def close(self, position: sqlite3.Row, reason: str) -> CloseResult:
        """Sell (network) + record (DB). Kept whole for the manual/sequential
        path; the concurrent path calls the executor then record_close directly."""
        fill = self.executor.sell(position["token_mint"], position["entry_qty"])
        return self.record_close(position, reason, fill)

    def record_close(self, position: sqlite3.Row, reason: str, fill) -> CloseResult:
        """DB-only finalize of a close from an already-executed sell Fill. Must be
        called serialized (engine lock) — the network sell already happened."""
        ts = utcnow_iso()
        cost = position["entry_amount_usd"] or 0.0
        proceeds = fill.usd_value
        pnl = proceeds - cost
        pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
        self.conn.execute(
            """UPDATE positions SET status='closed', exit_price=?, exit_reason=?,
               realized_pnl_usd=?, realized_pnl_pct=?, closed_ts=?, current_price=?
               WHERE id=? AND status='open'""",
            (fill.price_usd, reason, pnl, pnl_pct, ts, fill.price_usd, position["id"]),
        )
        self._record_trade(position["id"], fill, ts)
        self.conn.commit()
        return CloseResult(
            position_id=position["id"], token_mint=position["token_mint"],
            trigger_trader=position["trigger_trader"], reason=reason,
            realized_pnl_usd=pnl, realized_pnl_pct=pnl_pct, win=pnl > 0,
        )

    def close_by_id(self, position_id: int, reason: str = "manual") -> Optional[CloseResult]:
        p = self.conn.execute("SELECT * FROM positions WHERE id=? AND status='open'",
                              (position_id,)).fetchone()
        if not p:
            return None
        result = self.close(p, reason)
        self.conn.commit()
        return result

    # --- helpers -------------------------------------------------------------

    def _record_trade(self, position_id: int, fill: Fill, ts: str) -> None:
        self.conn.execute(
            """INSERT INTO trades
               (position_id, mode, side, token_mint, qty, price, usd_value, fees_usd,
                slippage_bps, executor, tx_sig, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (position_id, self.executor.name, fill.side, fill.token_mint, fill.qty,
             fill.price_usd, fill.usd_value, fill.fees_usd, fill.slippage_bps,
             fill.executor, fill.tx_sig, ts),
        )
