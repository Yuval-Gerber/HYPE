"""Telegram command router (§8) — turns owner commands into reply text.

Runs inside the Telegram poll thread, so it opens its own SQLite reader
connection (WAL allows concurrent readers) and builds its own API clients for
the live health checks in /status. Engine start/stop are delegated to callbacks
that marshal onto the Qt main thread (the controller emits a signal).

Transport + auth live in TelegramController; this module is pure "command → text".
"""

from __future__ import annotations

import html
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .. import db, secrets
from ..config import load_config
from ..data.birdeye import BirdeyeClient
from ..data.dexscreener import DexScreenerClient
from ..data.helius import HeliusClient
from ..data.jupiter import SOL_MINT, JupiterClient
from ..engine.account import Account
from ..logging_setup import get_logger
from ..wallet import WalletManager

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _fmt_age(secs: Optional[float]) -> str:
    if secs is None:
        return "—"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h {secs % 3600 // 60}m"


def _held(opened_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(opened_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _fmt_age((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return "—"


class CommandRouter:
    def __init__(self, *, engine_stats: Callable[[], dict],
                 start_cb: Callable[[], None], stop_cb: Callable[[], None],
                 panic_cb: Optional[Callable[[], None]] = None) -> None:
        self._engine_stats = engine_stats
        self._start_cb = start_cb
        self._stop_cb = stop_cb
        self._panic_cb = panic_cb
        self._conn: Optional[sqlite3.Connection] = None
        self.log = get_logger()

    def conn(self) -> sqlite3.Connection:
        # Lazily open one connection in the calling (Telegram) thread.
        if self._conn is None:
            self._conn = db.connect()
        return self._conn

    def _mode(self) -> str:
        """Active mode — the commands report on whichever session is live."""
        return load_config().mode

    # --- dispatch ------------------------------------------------------------

    def handle(self, cmd: str, args: list) -> Optional[str]:
        if cmd in ("status", "s"):
            return self._status()
        if cmd in ("positions", "position", "pos", "p"):
            return self._positions()
        if cmd == "pnl":
            return self._pnl()
        if cmd == "start":
            return self._start()
        if cmd == "stop":
            return self._stop()
        if cmd == "panic":
            if self._panic_cb is None:
                return "Panic isn't available."
            self._panic_cb()
            return ("🚨 <b>Panic drain requested</b> — selling everything to SOL and sweeping "
                    "to your home wallet. Watch for the confirmation.")
        if cmd == "mode":
            return f"Mode: <b>{load_config().mode.upper()}</b>"
        return None  # unknown → controller's default reply

    # --- engine control ------------------------------------------------------

    def _start(self) -> str:
        stats = self._safe_stats()
        if stats.get("running"):
            return "▶️ Engine is already running."
        self._start_cb()
        return "▶️ <b>Starting</b> the engine… (send /status in a few seconds)"

    def _stop(self) -> str:
        stats = self._safe_stats()
        if not stats.get("running"):
            return "⏹ Engine is already stopped."
        self._stop_cb()
        return "⏹ <b>Stopping</b> the engine…"

    # --- status --------------------------------------------------------------

    def _safe_stats(self) -> dict:
        try:
            return self._engine_stats() or {}
        except Exception:
            return {}

    def _status(self) -> str:
        cfg = load_config()
        stats = self._safe_stats()
        running = stats.get("running", False)

        lines = ["🤖 <b>Hype — Status</b>", ""]
        mode_emoji = "📝" if cfg.mode == "paper" else "🔴"
        lines.append(f"Mode: {mode_emoji} <b>{cfg.mode.upper()}</b>")
        if running:
            lines.append(f"Engine: 🟢 <b>Running</b> · up {_fmt_age(stats.get('uptime_seconds'))}")
        else:
            lines.append("Engine: ⚪️ <b>Stopped</b>")

        # Live connection health.
        lines.append("")
        lines.append("<b>Connections</b>")
        for label, result in (
            ("Birdeye", self._check_birdeye()),
            ("Helius", self._check_helius()),
            ("Jupiter", self._check_jupiter()),
        ):
            ok, detail = result
            lines.append(f"• {label} {'✅' if ok else '❌'} {html.escape(detail)}")

        # Balance / P&L. LIVE = the REAL on-chain wallet (gas included), the only
        # honest number. PAPER = the DB-simulated equity.
        mode = self._mode()
        try:
            acct = Account(self.conn(), mode)
            sess = acct.active_session()
            if sess is not None:
                sid = sess["id"]
                start = sess["starting_balance_usd"] or 0.0
                if mode == "live":
                    sol, sol_usd, _ = self._wallet_sol()
                    open_val, n_open = self._open_value(sid)
                    equity = (sol_usd or 0.0) + open_val
                    cashline = (f"• Wallet ${sol_usd:,.2f} ({sol:.4f} SOL) · Open ${open_val:,.2f} "
                                f"({n_open} pos)" if sol_usd is not None
                                else f"• Open ${open_val:,.2f} ({n_open} pos)")
                    eqnote = " (real wallet · gas included)"
                else:
                    eq = acct.equity_stored(sid)
                    equity = eq.equity_usd
                    cashline = (f"• Cash ${eq.cash_usd:,.0f} · Open ${eq.open_value_usd:,.0f} "
                                f"({eq.open_positions} pos)")
                    eqnote = ""
                pnl = equity - start
                pct = (pnl / start * 100.0) if start else 0.0
                sign = "+" if pnl >= 0 else "−"
                lines.append("")
                lines.append(f"<b>Balance ({mode})</b>")
                lines.append(f"• Equity: <b>${equity:,.2f}</b>{eqnote}")
                lines.append(cashline)
                lines.append(f"• Start ${start:,.2f} → P&amp;L {sign}${abs(pnl):,.2f} "
                             f"({sign}{abs(pct):.1f}%)")
                lines.append("")
                lines.append("<b>Performance</b>")
                lines.append(self._perf_line(sid, gross=(mode == "live")))
        except Exception as e:  # noqa: BLE001
            lines.append(f"\n(balance unavailable: {html.escape(str(e))})")

        # Feed / wallets (only meaningful while running).
        if running:
            lines.append("")
            feed = stats.get("feed_idle")
            lines.append(f"Feed: last event {_fmt_age(feed) + ' ago' if feed is not None else '—'} · "
                         f"watching {stats.get('wallets', '—')} wallets · "
                         f"{stats.get('buys', 0)} buys detected")
        return "\n".join(lines)

    def _perf_line(self, session_id: int, gross: bool = False) -> str:
        c = self.conn()
        row = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN realized_pnl_usd>0 THEN 1 ELSE 0 END),0) w "
            "FROM positions WHERE session_id=? AND status='closed'", (session_id,)).fetchone()
        n, w = row["n"], row["w"]
        wr = (100.0 * w / n) if n else 0.0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tp = c.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd),0) t FROM positions "
            "WHERE session_id=? AND status='closed' AND closed_ts LIKE ?",
            (session_id, today + "%")).fetchone()["t"]
        tsign = "+" if tp >= 0 else "−"
        note = " <i>(gross, before gas)</i>" if gross else ""
        return (f"• Closed {n} · win rate {wr:.0f}%\n"
                f"• Today trading P&amp;L: {tsign}${abs(tp):,.2f}{note}")

    # --- positions -----------------------------------------------------------

    def _positions(self) -> str:
        try:
            acct = Account(self.conn(), self._mode())
            sess = acct.active_session()
            if sess is None:
                return "No active session."
            rows = self.conn().execute(
                "SELECT token_symbol, token_mint, entry_price, current_price, tp_pct, sl_pct, "
                "opened_ts, trigger_trader FROM positions "
                "WHERE session_id=? AND status='open' ORDER BY id DESC", (sess["id"],)).fetchall()
        except Exception as e:  # noqa: BLE001
            return f"⚠️ positions unavailable: {html.escape(str(e))}"
        if not rows:
            return "📭 No open positions."
        out = [f"📊 <b>Open positions ({len(rows)})</b>", ""]
        for r in rows:
            entry = r["entry_price"] or 0.0
            cur = r["current_price"] or entry
            pct = ((cur - entry) / entry * 100.0) if entry else 0.0
            sym = html.escape(r["token_symbol"] or (r["token_mint"][:4] + "…"))
            emoji = "🟢" if pct >= 0 else "🔴"
            sign = "+" if pct >= 0 else "−"
            out.append(f"{emoji} <b>{sym}</b> {sign}{abs(pct):.1f}% · "
                       f"held {_held(r['opened_ts'])} · TP {r['tp_pct']:.0f}% / SL {r['sl_pct']:.0f}%")
        return "\n".join(out)

    def _pnl(self) -> str:
        mode = self._mode()
        try:
            acct = Account(self.conn(), mode)
            sess = acct.active_session()
            if sess is None:
                return "No active session."
            sid = sess["id"]
            c = self.conn()
            agg = c.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(realized_pnl_usd),0) tot, "
                "COALESCE(SUM(CASE WHEN realized_pnl_usd>0 THEN 1 ELSE 0 END),0) w, "
                "COALESCE(MAX(realized_pnl_usd),0) best, COALESCE(MIN(realized_pnl_usd),0) worst "
                "FROM positions WHERE session_id=? AND status='closed'", (sid,)).fetchone()
            start = sess["starting_balance_usd"] or 0.0
            # LIVE: equity = the REAL on-chain wallet (+ open positions), the only
            # honest number — matches the dashboard top bar. The DB-derived equity
            # overstates because it ignores gas + locked token-account rent. PAPER:
            # DB-simulated equity.
            if mode == "live":
                _, sol_usd, _ = self._wallet_sol()
                open_val, n_open = self._open_value(sid)
                equity = (sol_usd or 0.0) + open_val
                note = " (real wallet · gas + rent included)"
            else:
                eq = acct.equity_stored(sid)
                equity, open_val, n_open, note = eq.equity_usd, eq.open_value_usd, eq.open_positions, ""
        except Exception as e:  # noqa: BLE001
            return f"⚠️ P&amp;L unavailable: {html.escape(str(e))}"
        n, tot, w = agg["n"], agg["tot"], agg["w"]
        wr = (100.0 * w / n) if n else 0.0
        total_pnl = equity - start
        s = "+" if total_pnl >= 0 else "−"
        ts = "+" if tot >= 0 else "−"
        return (f"💰 <b>P&amp;L ({mode})</b>\n\n"
                f"• Equity ${equity:,.2f} (start ${start:,.0f}){note}\n"
                f"• Total P&amp;L {s}${abs(total_pnl):,.2f}\n"
                f"• Realized {ts}${abs(tot):,.2f} over {n} closed · win rate {wr:.0f}%\n"
                f"• Best +${agg['best']:,.2f} · Worst ${agg['worst']:,.2f}\n"
                f"• Open: {n_open} (${open_val:,.0f})")

    def _wallet_sol(self):
        """Real on-chain trading-wallet SOL + its USD value → (sol, usd, addr)."""
        try:
            wm = WalletManager()
            addr = wm.address()
            if not addr:
                return (None, None, None)
            sol = wm.sol_balance_of(HeliusClient(secrets.helius_api_key()), addr)
            price = DexScreenerClient().market(SOL_MINT).get("price_usd")
            return (sol, (sol * price if price else None), addr)
        except Exception:  # noqa: BLE001
            return (None, None, None)

    def _open_value(self, session_id: int):
        """USD value of open positions (DB current price) → (value, count)."""
        try:
            rows = self.conn().execute(
                "SELECT entry_qty, current_price, entry_price FROM positions "
                "WHERE session_id=? AND status='open'", (session_id,)).fetchall()
            val = sum((r["entry_qty"] or 0.0) * (r["current_price"] or r["entry_price"] or 0.0)
                      for r in rows)
            return val, len(rows)
        except Exception:  # noqa: BLE001
            return 0.0, 0

    # --- health checks -------------------------------------------------------

    def _check_helius(self) -> tuple[bool, str]:
        key = secrets.helius_api_key()
        if not key:
            return False, "no key"
        try:
            t = time.perf_counter()
            HeliusClient(key)._rpc("getHealth", [], retries=0)
            return True, f"{(time.perf_counter() - t) * 1000:.0f}ms"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:40]

    def _check_birdeye(self) -> tuple[bool, str]:
        key = secrets.birdeye_api_key()
        if not key:
            return False, "no key"
        try:
            t = time.perf_counter()
            BirdeyeClient(key).fetch_leaderboard(pool_size=1)
            return True, f"{(time.perf_counter() - t) * 1000:.0f}ms"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:40]

    def _check_jupiter(self) -> tuple[bool, str]:
        try:
            t = time.perf_counter()
            ok, _ = JupiterClient(secrets.jupiter_api_key()).simulate_sell(
                BONK, 100_000_000, slippage_bps=200)
            return bool(ok), f"{(time.perf_counter() - t) * 1000:.0f}ms"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:40]
