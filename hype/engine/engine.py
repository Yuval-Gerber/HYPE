"""Paper engine orchestrator (Phase 4).

Flow per buy signal (§5.2 → §5.3 → §5.4–5.6):
  signal → copyable? → dedupe → safety filters → size → buy → open position
         → equity snapshot → activity log

Background management (§5.6/§5.7):
  re-price open positions → fire TP/SL → on close, feed outcome to the
  investigation engine and snapshot equity.

The core is synchronous and lock-guarded so it's identical to drive from a live
async monitor (P2) or a deterministic test. Only the Executor differs paper↔live.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import List, Optional

from ..config import HypeConfig
from ..data.jupiter import SOL_MINT
from ..logging_setup import get_logger, log_activity
from ..models import BuySignal
from .account import Account, Equity
from .executor import Executor, ExecutionError, Fill
from .investigation import InvestigationEngine, InvestigationEvent
from .positions import CloseResult, PositionManager
from .pricing import PriceFeed
from .sizing import decide_size

try:  # filters are optional in pure-sim tests
    from ..safety import SafetyFilters
except Exception:  # pragma: no cover
    SafetyFilters = None  # type: ignore


@dataclass(slots=True)
class SignalOutcome:
    accepted: bool
    reason: str
    position_id: Optional[int] = None


class PaperEngine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: HypeConfig,
        executor: Executor,
        price_feed: PriceFeed,
        *,
        filters: Optional["SafetyFilters"] = None,
        skip_filters: bool = False,
        live_cash_usd=None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.executor = executor
        self.feed = price_feed
        self.filters = filters
        self.skip_filters = skip_filters
        # LIVE only: a callable returning the real wallet's spendable USD (SOL
        # value). When set, sizing uses the on-chain balance instead of the
        # DB-simulated cash, so live trades are sized off real funds.
        self.live_cash_usd = live_cash_usd
        self.log = get_logger()

        self.account = Account(conn, executor.name)
        self.positions = PositionManager(conn, executor, price_feed)
        self.investigation = InvestigationEngine(conn, cfg)
        # The lock guards the single SQLite connection only (not the network
        # clients, which are thread-safe). Held briefly around DB ops so buys
        # and concurrent sells never block each other on the slow network parts.
        self._lock = threading.Lock()
        self._inflight: set = set()   # tokens with an in-progress buy (dedupe guard)

        start_bal = live_cash_usd() if live_cash_usd is not None else cfg.trading.paper_starting_balance_usd
        self.session_id = self.account.get_or_start_session(start_bal)

    # --- trader bookkeeping --------------------------------------------------

    def _concentration_size_mult(self, concentration) -> float:
        """Risk-scaled bet size by token concentration (higher top-10 % → smaller
        bet). Unknown concentration → full size (no data to scale on)."""
        sched = getattr(self.cfg.trading, "size_by_concentration", None)
        if not sched or concentration is None:
            return 1.0
        for threshold, mult in sched:   # high→low; first tier met wins
            if concentration >= threshold:
                return float(mult)
        return 1.0

    def _risk_size_multiplier(self, address: str, concentration) -> float:
        """Bet-size multiplier: sized by the token's concentration (riskier token →
        smaller bet, per size_by_concentration)."""
        return self._concentration_size_mult(concentration)

    def adopt_orphan_positions(self) -> int:
        """On (re)start: adopt any on-chain token holding that ISN'T already an open
        position — an orphaned buy from stopping mid-trade before it was recorded.
        Tracks it (so TP/SL manage it and we never re-buy the same token) at its REAL
        entry cost when we can find the buy on-chain, else at current market value.
        Live only; returns how many were adopted."""
        from ..logging_setup import log_activity, utcnow_iso
        holdings_fn = getattr(self.executor, "token_holdings", None)
        if holdings_fn is None:
            return 0   # paper executor has no on-chain wallet
        try:
            holdings = holdings_fn()
        except Exception:  # noqa: BLE001
            return 0
        with self._lock:
            open_mints = {r["token_mint"] for r in self.conn.execute(
                "SELECT token_mint FROM positions WHERE session_id=? AND status='open'",
                (self.session_id,))}
        price_fn = getattr(self.executor, "position_price_usd", None)
        find_buy = getattr(self.executor, "find_recent_buy", None)
        adopted = 0
        for mint, base, dec in holdings:
            if mint in open_mints:
                continue
            qty = base / (10 ** dec) if dec else float(base)
            entry_price = entry_amount = None
            if find_buy is not None:      # real cost from the on-chain buy, if found
                try:
                    res = find_buy(mint)
                except Exception:  # noqa: BLE001
                    res = None
                if res and qty > 0:
                    usd, _recv = res
                    entry_amount, entry_price = usd, usd / qty
            cur_price = None
            if price_fn is not None:
                try:
                    cur_price = price_fn(mint, qty)
                except Exception:  # noqa: BLE001
                    cur_price = None
            if entry_price is None:        # fall back to current market value
                if not cur_price or cur_price <= 0:
                    continue               # can't price (dust/rugged) → leave for rent-reclaim
                entry_price = cur_price
                entry_amount = qty * cur_price
            if (entry_amount or 0) < 0.20:  # skip dust
                continue
            mark = cur_price if (cur_price and cur_price > 0) else entry_price
            tp, sl = self._scaled_tp(None), self._scaled_sl(None)   # unknown risk → conservative
            ts = utcnow_iso()
            with self._lock:
                self.conn.execute(
                    """INSERT INTO positions (session_id, mode, token_mint, trigger_trader, status,
                       entry_price, entry_qty, entry_amount_usd, opened_ts, tp_pct, sl_pct,
                       current_price, peak_price) VALUES (?,?,?,?,'open',?,?,?,?,?,?,?,?)""",
                    (self.session_id, self.executor.name, mint, "adopted", entry_price, qty,
                     entry_amount, ts, tp, sl, mark, max(entry_price, mark)))
                log_activity(self.conn, "system",
                             f"Adopted untracked holding {mint[:8]}… (~${entry_amount:.2f}) "
                             f"— orphaned buy, now tracked + managed", mint=mint)
                self.conn.commit()
            adopted += 1
        return adopted

    def reconcile_sold_positions(self) -> int:
        """On (re)start: any OPEN live position whose tokens are GONE from the wallet
        was sold on-chain but never recorded — the owner sold it (e.g. 'Sell now')
        and the app closed before the DB write landed. That leaves it stuck showing
        as 'trading' forever. Here we detect the zero balance, read the REAL proceeds
        from the sell tx, and close it through the normal path (correct P&L, trader
        policing, equity marker, activity log). The mirror of adopt_orphan_positions.
        Live only; returns how many were reconciled."""
        from ..logging_setup import log_activity
        bal_fn = getattr(self.executor, "token_balance_base", None)
        if bal_fn is None:
            return 0   # paper executor — no on-chain wallet
        find_sell = getattr(self.executor, "find_recent_sell", None)
        with self._lock:
            opens = self.conn.execute(
                "SELECT * FROM positions WHERE session_id=? AND status='open'",
                (self.session_id,)).fetchall()
        n = 0
        for p in opens:
            mint = p["token_mint"]
            try:
                if bal_fn(mint) > 0:
                    continue   # wallet still holds the token → genuinely open
            except Exception:  # noqa: BLE001 - a balance hiccup must not close a live position
                continue
            # Tokens are gone → it was sold. Recover the real proceeds from the tx;
            # if the sell can't be found, fall back to the last mark so it still closes.
            proceeds = qty = fee_usd = sig = None
            if find_sell is not None:
                try:
                    res = find_sell(mint)
                except Exception:  # noqa: BLE001
                    res = None
                if res:
                    proceeds, qty, fee_usd, sig = res
            if proceeds is None:
                qty = p["entry_qty"] or 0.0
                mark = p["current_price"] or p["entry_price"] or 0.0
                proceeds, fee_usd = qty * mark, 0.0
            qty = qty or (p["entry_qty"] or 0.0)
            price_usd = (proceeds / qty) if qty else 0.0
            fill = Fill(side="sell", token_mint=mint, qty=qty, price_usd=price_usd,
                        usd_value=proceeds, fees_usd=fee_usd or 0.0, slippage_bps=0,
                        executor=self.executor.name, tx_sig=sig)
            with self._lock:
                cr = self.positions.record_close(p, "manual", fill)
                self._handle_close(cr)
                log_activity(self.conn, "system",
                             f"Reconciled #{p['id']} {mint[:8]}… — sold on-chain but "
                             f"unrecorded; closed at ${proceeds:.2f} ({cr.realized_pnl_pct:+.1f}%)",
                             mint=mint)
            n += 1
        return n

    def ensure_trader(self, address: str) -> None:
        """Make sure a trader row exists (manual/unknown signal sources)."""
        from ..logging_setup import utcnow_iso
        now = utcnow_iso()
        self.conn.execute(
            """INSERT INTO traders(wallet_address, source, active, created_ts, updated_ts)
               VALUES(?, 'manual', 1, ?, ?)
               ON CONFLICT(wallet_address) DO NOTHING""",
            (address, now, now),
        )
        self.conn.commit()

    # --- signal handling -----------------------------------------------------

    def handle_signal(self, signal: BuySignal) -> SignalOutcome:
        """Process a buy signal. DB touches are locked (brief); the slow network
        parts (safety filters, the swap) run UNLOCKED so buys and concurrent
        sells never stall on each other. An in-flight guard prevents a duplicate
        buy of the same token while its filters/swap are in progress."""
        trader = signal.trader_wallet
        token = signal.token_mint

        # 1. Guarded pre-checks + reserve the token (DB, locked).
        with self._lock:
            self.ensure_trader(trader)
            if not self.investigation.is_copyable(trader):
                return self._skip(signal, "trader not copyable (paused/investigation/dropped)")
            if self.cfg.engine.dedupe_same_token and (
                    token in self._inflight
                    or self.positions.has_open_position(self.session_id, token)):
                return self._skip(signal, "already holding this token")
            # Pump-and-dump guard: a trader who keeps buying the SAME token is often
            # pumping their own coin — we win small copies until they rug it. Cap how
            # many times we'll trade one token (0 = unlimited).
            cap = self.cfg.engine.max_buys_per_token
            if cap and self.conn.execute(
                    "SELECT COUNT(*) FROM positions WHERE session_id=? AND token_mint=?",
                    (self.session_id, token)).fetchone()[0] >= cap:
                return self._skip(signal, f"token already traded {cap}× (pump-and-dump guard)")
            self._inflight.add(token)
        try:
            # 2. Safety filters (§5.3) — NETWORK, unlocked.
            concentration = None
            if not self.skip_filters:
                if self.filters is None:
                    with self._lock:
                        return self._skip(signal, "safety filters unavailable")
                report = self.filters.check(token, self.cfg)
                if not report.passed:
                    names = ", ".join(c.name for c in report.failures)
                    readings = {c.name: c.detail for c in report.failures if c.detail}
                    with self._lock:
                        log_activity(self.conn, "filter", f"token rejected: {names}",
                                     level="INFO", mint=token, trader=trader, readings=readings)
                    return SignalOutcome(False, f"failed safety filters: {names}")
                concentration = report.concentration

            # 3. Sizing (§5.5). Feed reads are network-safe (no lock); only the
            # open-positions DB read is locked. Gas reserve auto-tracks SOL price.
            # Risk-scaled bet size (§5.5): a FULLY-TRUSTED trader bets full; everyone
            # else is sized by the token's concentration (riskier token → smaller bet).
            size_mult = self._risk_size_multiplier(trader, concentration)
            sol_price = self.feed.get_price_usd(SOL_MINT)
            if self.live_cash_usd is not None:
                cash = self.live_cash_usd()
                with self._lock:
                    opens = self.account.open_positions(self.session_id)
                open_value = sum((p["entry_qty"] or 0.0) *
                                 (self.feed.get_price_usd(p["token_mint"]) or p["entry_price"] or 0.0)
                                 for p in opens)
                decision = decide_size(
                    equity_usd=cash + open_value, available_cash_usd=cash,
                    open_positions=len(opens), cfg=self.cfg, sol_price_usd=sol_price,
                    size_multiplier=size_mult)
            else:
                with self._lock:
                    eq = self.account.equity_stored(self.session_id)
                decision = decide_size(
                    equity_usd=eq.equity_usd, available_cash_usd=eq.cash_usd,
                    open_positions=eq.open_positions, cfg=self.cfg, sol_price_usd=sol_price,
                    size_multiplier=size_mult)
            if not decision.approved:
                with self._lock:
                    return self._skip(signal, f"not sized: {decision.reason}")

            # Pool-depth cap: never trade more than max_pct_of_liquidity of the
            # token's pool, so our own buy/sell can't move the price or signal
            # others. No effect at small size; the guardrail that lets us scale.
            cap_pct = self.cfg.trading.max_pct_of_liquidity
            liq = getattr(report, "liquidity_usd", None) if not self.skip_filters else None
            if cap_pct and liq:
                liq_cap = liq * cap_pct / 100.0
                if decision.usd_amount > liq_cap:
                    if liq_cap < self.cfg.trading.min_trade_usd:
                        with self._lock:
                            return self._skip(signal, f"pool too shallow: cap ${liq_cap:,.2f} "
                                              f"(<{cap_pct:.1f}% of ${liq:,.0f} liq)")
                    decision.usd_amount = liq_cap

            # 4. Buy (§5.4) — NETWORK, unlocked.
            try:
                fill = self.executor.buy(token, decision.usd_amount)
            except ExecutionError as e:
                with self._lock:
                    return self._skip(signal, f"execution skipped: {e}")

            # 5. Open the position (DB, locked).
            with self._lock:
                tp = self._scaled_tp(concentration)
                sl = self._scaled_sl(concentration)
                position_id = self.positions.open_from_fill(
                    self.session_id, signal, fill, tp_pct=tp, sl_pct=sl,
                    token_symbol=signal.token_symbol)
                eq = self._current_equity()
                self.account.snapshot(self.session_id, eq, marker="buy",
                                      trigger_trader=trader, note=f"buy {token}")
                conc_txt = f"{concentration:.0f}%" if concentration is not None else "n/a"
                log_activity(self.conn, "buy",
                             f"opened #{position_id} {fill.qty:,.2f} @ ${fill.price_usd:.6g} "
                             f"(${decision.usd_amount:,.2f})",
                             mint=token, trader=trader, position_id=position_id,
                             fee=fill.fees_usd, concentration=conc_txt,
                             take_profit=f"{tp:.1f}%", stop_loss=f"{sl:.1f}%")
            return SignalOutcome(True, decision.reason, position_id)
        finally:
            with self._lock:
                self._inflight.discard(token)

    def _scaled_tp(self, concentration: Optional[float]) -> float:
        """Pick the take-profit target from the concentration schedule (§5.6):
        higher top-10 concentration → smaller/faster TP → exit before the rug.
        Unknown concentration is treated as risky (mid-high). Empty schedule →
        the flat take_profit_pct."""
        sched = self.cfg.trading.tp_by_concentration
        if not sched:
            return self.cfg.trading.take_profit_pct
        conc = 100.0 if concentration is None else concentration
        for threshold, tp in sorted(sched, key=lambda x: -x[0]):
            if conc >= threshold:
                return tp
        return min(sched, key=lambda x: x[0])[1]

    def _scaled_sl(self, concentration: Optional[float]) -> float:
        """Pick the stop-loss from the concentration schedule (§5.6): the mirror
        of _scaled_tp — higher top-10 concentration → tighter stop → cut a risky
        trade fast. Unknown concentration treated as risky. Empty schedule → the
        flat stop_loss_pct."""
        sched = self.cfg.trading.sl_by_concentration
        if not sched:
            return self.cfg.trading.stop_loss_pct
        conc = 100.0 if concentration is None else concentration
        for threshold, sl in sorted(sched, key=lambda x: -x[0]):
            if conc >= threshold:
                return sl
        return min(sched, key=lambda x: x[0])[1]

    def _skip(self, signal: BuySignal, reason: str) -> SignalOutcome:
        log_activity(self.conn, "skip", reason, level="INFO",
                     mint=signal.token_mint, trader=signal.trader_wallet)
        return SignalOutcome(False, reason)

    # --- position management (TP/SL + investigation) -------------------------

    def manage_once(self) -> List[CloseResult]:
        with self._lock:
            closes = self.positions.evaluate(self.session_id, self.cfg)
            for c in closes:
                self._handle_close(c)
            return closes

    # Concurrent close path: plan (fast, locked) → sell each concurrently
    # (network, UNLOCKED) → finalize (DB, locked). Lets many exits fire at once
    # instead of one-at-a-time, and lets a slow buy overlap the sells.

    def plan_closes(self) -> list:
        """Re-price + decide which positions to close. Returns [(position, reason)]."""
        with self._lock:
            return self.positions.plan_closes(self.session_id, self.cfg)

    def execute_close(self, position, reason: str) -> Optional[CloseResult]:
        """Sell ONE planned position. The Jupiter swap runs WITHOUT the lock so
        many run concurrently; only the DB finalize + bookkeeping is locked."""
        mint = position["token_mint"]
        try:
            fill = self.executor.sell(mint, position["entry_qty"])   # network, no lock
        except Exception as e:  # noqa: BLE001 - rug / no sell route
            with self._lock:
                price = (self.feed.get_price_usd(mint) or position["current_price"]
                         or position["entry_price"] or 0.0)
                entry = position["entry_price"] or 0.0
                pct = ((price - entry) / entry * 100.0) if entry else 0.0
                if pct <= -90.0:   # catastrophic + unsellable → write off
                    cr = self.positions.write_off(position, price)
                    if cr is not None:
                        self._handle_close(cr)
                        return cr
            self.log.warning("execute_close #%s (%s) failed: %s", position["id"], reason, e)
            return None
        with self._lock:
            cr = self.positions.record_close(position, reason, fill)
            self._handle_close(cr)
            return cr

    def sell_position(self, position_id: int) -> Optional[CloseResult]:
        """Manually close a position (Sell now / test sell) with the SAME
        post-close handling as TP/SL: investigation, equity snapshot, activity
        log. Returns the CloseResult, or None if the position wasn't open."""
        with self._lock:
            c = self.positions.close_by_id(position_id, "manual")
            if c is not None:
                self._handle_close(c)
            return c

    def snapshot_now(self) -> None:
        """Write a plain equity point (marker=None) so the equity curve stays
        continuous between trades. Called periodically by the controller."""
        with self._lock:
            eq = self._current_equity()
            self.account.snapshot(self.session_id, eq)

    def _current_equity(self) -> Equity:
        """Equity for snapshots. LIVE: REAL wallet cash (SOL×price, gas already
        reflected) + open positions at their last mark — so the equity curve
        matches the real wallet, not the DB trade-sum (which ignores gas). PAPER:
        DB equity_stored. No network (cached cash + DB marks)."""
        if self.live_cash_usd is not None:
            cash = self.live_cash_usd()
            opens = self.account.open_positions(self.session_id)
            open_value = sum((p["entry_qty"] or 0.0) * (p["current_price"] or p["entry_price"] or 0.0)
                             for p in opens)
            return Equity(
                starting_balance_usd=self.account.starting_balance(self.session_id),
                cash_usd=cash, open_value_usd=open_value,
                realized_pnl_usd=self.account.realized_pnl(self.session_id),
                unrealized_pnl_usd=0.0, open_positions=len(opens))
        return self.account.equity_stored(self.session_id)

    def _handle_close(self, c: CloseResult) -> None:
        events = self.investigation.record_outcome(
            c.trigger_trader, c.win, c.realized_pnl_pct, c.realized_pnl_usd
        ) if c.trigger_trader else []
        eq = self._current_equity()   # live: real wallet; paper: DB (no network)
        self.account.snapshot(self.session_id, eq, marker="sell",
                              trigger_trader=c.trigger_trader,
                              note=f"{c.reason} {c.token_mint}")
        # Honest labelling: a TP/trailing exit that slipped to a LOSS by the time
        # it filled is not a clean win — show it slipped (the win/loss stats
        # already use the real P&L, so it's counted as a loss regardless).
        if c.reason == "manual":
            verb = "SOLD"
        elif c.reason == "tp" and c.realized_pnl_usd < 0:
            verb = "TP→SLIPPED"
        else:
            verb = c.reason.upper()
        log_activity(
            self.conn, "sell" if c.reason == "manual" else c.reason,
            f"closed #{c.position_id} {verb} {c.realized_pnl_pct:+.1f}% "
            f"(${c.realized_pnl_usd:+,.2f})",
            mint=c.token_mint, trader=c.trigger_trader,
            win=c.win, pnl_usd=c.realized_pnl_usd, pnl_pct=c.realized_pnl_pct)
        for ev in events:
            self._log_event(ev)

    def _log_event(self, ev: InvestigationEvent) -> None:
        msg = {
            "triggered": "UNDER INVESTIGATION",
            "probation_tripped": "probation tripped → investigation",
            "probation_passed": "passed probation → active",
            "second_chance": "second chance → probation",
            "dropped": "dropped",
            "kept_paused": "kept paused",
            "auto_dropped": "auto-dropped at ceiling",
            "readded": "re-added as fresh",
        }.get(ev.kind, ev.kind)
        badge = f" ×{ev.level}" if ev.level >= 2 else ""
        log_activity(self.conn, "investigation", f"{ev.trader[:8]}…: {msg}{badge} — {ev.detail}",
                     level="WARNING", trader=ev.trader, kind=ev.kind, level_badge=ev.level)
