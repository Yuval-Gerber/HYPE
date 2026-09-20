"""EngineController — runs the paper engine in a background thread.

The engine is async (Helius WebSocket + a TP/SL manage loop). This runs it in a
dedicated thread with its own asyncio loop and its own SQLite *writer* connection
(WAL mode lets the UI read concurrently). Communicates with the UI via Qt signals
(thread-safe, queued to the main thread).

Phase 6 swaps PaperExecutor for LiveExecutor; nothing here changes otherwise.
"""

from __future__ import annotations

import asyncio
import threading
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .. import db, nicknames, secrets
from ..power import KeepAwake
from ..config import load_config
from ..data.birdeye import BirdeyeClient
from ..data.dexscreener import DexScreenerClient
from ..data.helius import HeliusClient
from ..data.jupiter import JupiterClient
from ..data.rugcheck import RugCheckClient
from ..engine.engine import PaperEngine
from ..engine.executor import PaperExecutor
from ..engine.pricing import DexScreenerPriceFeed
from ..logging_setup import get_logger, log_activity, utcnow_iso
from ..models import BuySignal
from ..monitor import LiveMonitor
from ..ranker import fetch_candidates, rank_and_store, select_and_store
from ..safety import SafetyFilters

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
SOL = "So11111111111111111111111111111111111111112"


class EngineController(QObject):
    stateChanged = pyqtSignal(bool)        # running
    lightChanged = pyqtSignal(str, str)    # name, state
    notify = pyqtSignal(str, str)          # message, kind
    tradersChanged = pyqtSignal()          # followed list / ranking changed

    def __init__(self) -> None:
        super().__init__()
        self.log = get_logger()
        self.running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task | None = None
        self._monitor: LiveMonitor | None = None
        self._engine: PaperEngine | None = None
        self._stop = threading.Event()
        self._sell_lock = threading.Lock()
        self._sells: set[int] = set()
        self._test_buys: list[str] = []
        self._test_sls: list[str] = []
        self._rerank = threading.Event()          # request a Birdeye re-rank
        self._watch_refresh = threading.Event()   # request a cheap watch-set refresh
        self._rank_thread: threading.Thread | None = None     # detached startup Birdeye pull (fast start)
        self._pool: list = []                     # cached Birdeye candidate pool (TraderStat)
        self.wallet_ctrl = None                    # set by the main window (live keypair + balance)
        self.telegram = None                       # set by the main window (live alerts)
        self._live_keypair = None                  # loaded once at live start (Touch ID)
        self._last_alert_id = 0                     # activity_log id watermark for Telegram alerts
        # System-tab metrics.
        self._started_at: float | None = None     # monotonic; uptime
        self._last_heartbeat: float | None = None  # wall clock; EVENT-LOOP liveness (independent beat)
        self._manage_beat: float | None = None     # wall clock; MANAGE-LOOP progress (catches a hung loop)
        self._helius_ms: float | None = None
        self._jupiter_ms: float | None = None
        # Reliability (Problem 1): prevent the Mac from sleeping while running,
        # and a watchdog that restarts the engine if the manage loop goes stale.
        self._should_run = False          # owner intent (survives auto-restart)
        self._keepawake = KeepAwake()
        self._watchdog = QTimer(self)
        self._watchdog.timeout.connect(self._watchdog_tick)
        self._watchdog.start(20_000)

    # --- control -------------------------------------------------------------

    def start(self) -> None:
        if self.running:
            return
        if not (secrets.birdeye_api_key() and secrets.helius_api_key()):
            self.notify.emit("Birdeye/Helius API keys not configured", "stop")
            return
        # LIVE: load the wallet key ONCE here (main thread → Touch ID) and hold it
        # for the session so autonomous trading doesn't re-prompt (§6.1). Abort if
        # the wallet is missing or auth fails.
        self._live_keypair = None
        if load_config().mode == "live":
            wc = self.wallet_ctrl
            if wc is None or not wc.exists():
                self.notify.emit("Create + fund a trading wallet before going live", "stop")
                return
            try:
                self._live_keypair = wc.wallet.load_keypair(reason="Authorize live trading")
            except Exception as e:  # noqa: BLE001
                self.notify.emit(f"Live start blocked: {e}", "stop")
                return
        self._should_run = True
        self._begin()

    def _reconcile_fees(self, executor) -> None:
        """Back-fill trades.fees_usd with the real on-chain fee for live trades
        recorded at 0. Uses its own short-lived DB connection so it never contends
        with the engine's connection. Best-effort — a tx not yet confirmed is left
        for the next cycle."""
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT id, tx_sig FROM trades WHERE mode='live' "
                "AND COALESCE(fees_usd, 0) = 0 AND tx_sig IS NOT NULL "
                "ORDER BY id DESC LIMIT 30").fetchall()
            updated = 0
            for r in rows:
                fee = executor.tx_fee_usd(r["tx_sig"])
                if fee is not None and fee > 0:
                    conn.execute("UPDATE trades SET fees_usd=? WHERE id=?", (fee, r["id"]))
                    updated += 1
            if updated:
                conn.commit()
        finally:
            conn.close()

    def _run_initial_rank(self) -> None:
        """Full Birdeye leaderboard pull + roster select — run in a DETACHED thread
        so the ~30s, 20-page, rate-limited pull never blocks the monitor from
        watching or the manage loop from managing exits at startup. Greens the
        Birdeye light within ~1s via a cheap single-page probe, then does the full
        pull and refreshes the live watch set. Own DB connection + clients."""
        conn = db.connect()
        try:
            cfg = load_config()
            be = BirdeyeClient(secrets.birdeye_api_key())
            # Quick probe (1 page) → the Birdeye light goes green fast, well before
            # the full 20-page pull finishes.
            try:
                be.fetch_leaderboard(window_days=cfg.ranker.birdeye_rank_window_days, pool_size=1)
                self.lightChanged.emit("Birdeye", "ok")
            except Exception:  # noqa: BLE001
                self.lightChanged.emit("Birdeye", "down")
            helius = HeliusClient(secrets.helius_api_key())
            pool = fetch_candidates(be, cfg)
            self._pool = pool
            ranked = select_and_store(pool, cfg, conn, helius)
            nicknames.ensure(conn)
            self.lightChanged.emit("Birdeye", "ok")
            self._watch_refresh.set()   # let the manage loop refresh the monitor safely
            self.notify.emit(
                f"Ranking ready — {len(ranked)} active traders online (pool {len(pool)})", "ok")
            self.tradersChanged.emit()
        except Exception as e:  # noqa: BLE001
            self.lightChanged.emit("Birdeye", "warn")
            self.log.warning("background initial rank failed: %s", e)
        finally:
            conn.close()

    def _spawn_initial_rank(self) -> None:
        if self._rank_thread is not None and self._rank_thread.is_alive():
            return
        self._rank_thread = threading.Thread(
            target=self._run_initial_rank, daemon=True, name="hype-rank")
        self._rank_thread.start()

    def _prewarm_armed(self, executor, cfg) -> None:
        """Pre-sign the exit for EVERY open live position — so both the STOP-LOSS
        and the (trailing) take-profit fire instantly, skipping the ~300-500ms
        build+sign at trigger time. Own DB connection → no contention with the
        engine. prewarm_exit is throttled internally (rebuild ≤ every 3s per mint),
        so warming every position each cycle is cheap.

        NOTE: a pre-signed exit only helps when a sell route still exists. It does
        NOT save a token whose liquidity was pulled (an LP-pull rug) — there is
        nothing to sell into at any speed. It shaves latency on ordinary declines."""
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT token_mint FROM positions "
                "WHERE mode='live' AND status='open'").fetchall()
        finally:
            conn.close()
        for r in rows:
            executor.prewarm_exit(r["token_mint"])

    def _live_cash_usd(self) -> float:
        """Real spendable USD for live sizing = trading-wallet SOL × SOL price."""
        wc = self.wallet_ctrl
        if wc is None or not wc.sol_price_usd:
            return 0.0
        return wc.sol_balance * wc.sol_price_usd

    _ALERT_CATS = ("buy", "sell", "tp", "sl", "rug", "investigation", "error", "panic", "payout")
    _ALERT_EMOJI = {"buy": "🟢", "sell": "💰", "tp": "✅", "sl": "🛑", "rug": "☠️",
                    "investigation": "🔎", "error": "⚠️", "panic": "🚨", "payout": "🏦"}

    def _forward_alerts(self, conn) -> None:
        """Tail the activity log and push new significant events to Telegram,
        respecting the per-category alert toggles. Decoupled from the engine — it
        catches everything logged (trades, investigations, errors, panic, payout)."""
        tg = self.telegram
        if tg is None:
            return
        cfg = load_config()
        if not cfg.telegram.enabled:
            return
        t = cfg.telegram
        rows = conn.execute(
            "SELECT id, category, message, data_json FROM activity_log WHERE id > ? ORDER BY id",
            (self._last_alert_id,)).fetchall()
        for r in rows:
            self._last_alert_id = r["id"]
            cat = r["category"]
            if cat not in self._ALERT_CATS:
                continue
            if cat in ("buy", "sell", "tp", "sl", "rug") and not t.alert_trades:
                continue
            # Trade-alert noise control: buys are routine → no alert;
            # closes only ping on a real WIN ≥ +20% or ANY loss.
            if cat == "buy":
                continue
            if cat in ("sell", "tp", "sl", "rug"):
                import json as _json
                d = _json.loads(r["data_json"] or "{}")
                pnl = d.get("pnl_usd")
                pct = d.get("pnl_pct")
                if pnl is not None and not (pnl < 0 or (pct is not None and pct >= 20)):
                    continue
            if cat == "investigation" and not t.alert_investigations:
                continue
            if cat == "error" and not t.alert_errors:
                continue
            if cat in ("panic", "payout") and not t.alert_panic:
                continue
            emoji = self._ALERT_EMOJI.get(cat, "•")
            import html as _html
            tg.alert(f"{emoji} {_html.escape(r['message'])}")

    def _maybe_payout(self, conn) -> None:
        """Auto-deposit sweep (§5.8) — LIVE only, off by default. Every
        `interval_hours`, sweep a fixed USD amount of SOL to the home wallet,
        never touching the working balance + gas reserve. Skips (retries next
        cycle) if funds are short — never drains."""
        from datetime import datetime, timezone
        from ..engine.account import Account, Equity
        from ..engine.transfer import send_sol, TransferError

        cfg = load_config()
        p = cfg.payout
        if not p.enabled or self._live_keypair is None or not p.home_wallet_address:
            return
        wc = self.wallet_ctrl
        price = wc.sol_price_usd if wc else None
        if not price:
            return
        # Due? (last successful payout, else the live-session start.)
        row = conn.execute("SELECT MAX(ts) t FROM activity_log "
                           "WHERE category='payout' AND message LIKE 'PAID%'").fetchone()
        last_ts = row["t"] if row else None
        sess = conn.execute("SELECT id, starting_balance_usd FROM sessions "
                            "WHERE mode='live' AND ended_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if not last_ts and sess:
            last_ts = sess["started_ts"] if "started_ts" in sess.keys() else None
        if last_ts:
            try:
                age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last_ts)).total_seconds() / 3600.0
            except Exception:  # noqa: BLE001
                age_h = 1e9
            if age_h < max(1, p.interval_hours):
                return
        # Amount: min(fixed, available above working balance + gas reserve).
        wallet_usd = wc.sol_balance * price
        gas_usd = cfg.trading.gas_reserve_sol * price
        available = wallet_usd - (p.min_working_balance_usd or 0.0) - gas_usd
        fixed = p.fixed_amount_usd or 0.0
        amount_usd = min(fixed, available) if fixed > 0 else 0.0
        if amount_usd <= 0:
            return   # not enough yet — retry next cycle, never drain
        lamports = int((amount_usd / price) * 1e9)
        try:
            helius = HeliusClient(secrets.helius_api_key())
            res = send_sol(helius, self._live_keypair, p.home_wallet_address, lamports)
        except (TransferError, Exception) as e:  # noqa: BLE001
            self.log.warning("payout failed: %s", e)
            return
        log_activity(conn, "payout", f"PAID ${amount_usd:.2f} ({res.sol:.4f} SOL) to home wallet",
                     home=p.home_wallet_address, signature=res.signature,
                     solscan=f"https://solscan.io/tx/{res.signature}")
        self.notify.emit(f"Auto-payout: ${amount_usd:.2f} swept to MetaMask ✅", "ok")
        if sess:   # drop a withdraw marker on the live equity curve
            try:
                Account(conn, "live").snapshot(
                    sess["id"], Equity(sess["starting_balance_usd"], wallet_usd - amount_usd,
                                       0.0, 0.0, 0.0, 0), marker="withdraw",
                    note=f"payout ${amount_usd:.2f}")
            except Exception:  # noqa: BLE001
                pass

    def _begin(self) -> None:
        """Actually spin up the engine thread (also used by the watchdog)."""
        self._stop.clear()
        self.running = True
        self._started_at = time.monotonic()
        self._last_heartbeat = time.time()
        self._manage_beat = time.time()
        if load_config().engine.prevent_sleep:
            self._keepawake.start()
        self.stateChanged.emit(True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._should_run = False
        self._shutdown()

    def _shutdown(self) -> None:
        self._stop.set()
        if self._monitor:
            try:
                self._monitor.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._loop and self._main_task:
            try:
                self._loop.call_soon_threadsafe(self._main_task.cancel)
            except Exception:  # noqa: BLE001
                pass
        self.running = False
        self._started_at = None
        self._keepawake.stop()
        self.stateChanged.emit(False)
        for n in ("Birdeye", "Helius", "Jupiter"):
            self.lightChanged.emit(n, "grey")

    def _watchdog_tick(self) -> None:
        """Every 20s: restart if the owner wants it running but the engine (a) died,
        (b) the EVENT LOOP froze (independent heartbeat stale >90s), or (c) the
        MANAGE LOOP hung — stopped completing iterations for >4 min. (c) is the key
        addition: the independent heartbeat proves only that the event loop is alive,
        so a manage loop stuck on a bad await (or starved by a DB lock) would freeze
        trading SILENTLY without it. The 4-min window is generous enough that a slow
        network call / ranking pull never trips it, but a true hang self-recovers."""
        if not self._should_run:
            return
        hb_age = (time.time() - self._last_heartbeat) if self._last_heartbeat else None
        mg_age = (time.time() - self._manage_beat) if self._manage_beat else None
        hb_stalled = hb_age is not None and hb_age > 90
        mg_stalled = mg_age is not None and mg_age > 240
        if not self.running or hb_stalled or mg_stalled:
            why = ("died" if not self.running
                   else f"event loop stalled {int(hb_age)}s" if hb_stalled
                   else f"manage loop hung {int(mg_age)}s")
            self.log.warning("Watchdog: engine %s — restarting", why)
            self.notify.emit("Engine auto-recovering (watchdog)…", "stop")
            self._shutdown()
            QTimer.singleShot(2500, self._restart_if_wanted)

    def _restart_if_wanted(self) -> None:
        if self._should_run and not self.running:
            self._begin()

    def request_sell(self, position_id: int) -> None:
        with self._sell_lock:
            self._sells.add(position_id)

    def request_rerank(self) -> None:
        """Refresh the Birdeye ranking now. If the engine is running the manage
        loop handles it (and updates the live watch set); otherwise run a one-off
        refresh against the DB so the Traders tab still updates."""
        if self.running:
            self._rerank.set()
            return

        def _oneoff() -> None:
            try:
                conn = db.connect()
                cfg = load_config()
                rank_and_store(BirdeyeClient(secrets.birdeye_api_key()), cfg, conn,
                               HeliusClient(secrets.helius_api_key()))
                conn.close()
                self.notify.emit("Ranking refreshed", "ok")
                self.tradersChanged.emit()
            except Exception as e:  # noqa: BLE001
                self.notify.emit(f"Ranking refresh failed: {e}", "stop")

        threading.Thread(target=_oneoff, daemon=True).start()

    def request_watch_refresh(self) -> None:
        """Cheap DB-only refresh of the followed/watched set (after a manual
        add/remove, enable/disable, or investigation action). No Birdeye call."""
        if self.running:
            self._watch_refresh.set()

    def request_test_buy(self, mint: str = BONK) -> bool:
        """Queue a forced paper buy on a real liquid token (proves the pipeline).
        Returns False if the engine isn't running."""
        if not self.running:
            return False
        with self._sell_lock:
            self._test_buys.append(mint)
        return True

    def request_test_sl(self, mint: str = BONK) -> bool:
        """Queue a forced LOSING paper trade to prove the stop-loss: opens a paper
        position on a liquid token, then crashes its mark price below the −15%
        stop so the real engine fires the SL — you watch the balance and win rate
        drop live. Paper only; refused in live mode. False if not running."""
        if not self.running or load_config().mode != "paper":
            return False
        with self._sell_lock:
            self._test_sls.append(mint)
        return True

    def stats(self) -> dict:
        m = self._monitor
        up = (time.monotonic() - self._started_at) if (self.running and self._started_at) else None
        hb = (time.time() - self._last_heartbeat) if self._last_heartbeat else None
        return {
            "running": self.running,
            "wallets": len(m.wallets) if m else 0,
            "events": m.events_seen if m else 0,
            "buys": m.buys_emitted if m else 0,
            "uptime_seconds": up,
            "heartbeat_age": hb,
            "helius_ms": self._helius_ms,
            "jupiter_ms": self._jupiter_ms,
            "feed_idle": m.seconds_since_message() if (m and self.running) else None,
        }

    def _drain_sells(self) -> list[int]:
        with self._sell_lock:
            out = list(self._sells)
            self._sells.clear()
        return out

    def _drain_test_buys(self) -> list[str]:
        with self._sell_lock:
            out = list(self._test_buys)
            self._test_buys.clear()
        return out

    def _drain_test_sls(self) -> list[str]:
        with self._sell_lock:
            out = list(self._test_sls)
            self._test_sls.clear()
        return out

    # --- background loop -----------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._main_task = loop.create_task(self._main())
        try:
            loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            self.log.warning("Engine thread error: %s", e)
            self.notify.emit(f"Engine stopped: {e}", "stop")
        finally:
            loop.close()
            self._loop = None
            self.running = False
            self._keepawake.stop()
            # If we exited without an explicit Stop, tell the truth (no false
            # "Running") — the watchdog will bring it back if the owner wants it.
            if not self._stop.is_set():
                self.stateChanged.emit(False)
                self.notify.emit("Engine stopped unexpectedly — recovering…", "stop")

    async def _main(self) -> None:
        cfg = load_config()
        conn = db.connect()  # engine writer connection (this thread)
        try:  # only alert on events from NOW on, not the whole backlog
            self._last_alert_id = conn.execute(
                "SELECT COALESCE(MAX(id),0) m FROM activity_log").fetchone()["m"]
        except Exception:  # noqa: BLE001
            pass
        for n in ("Birdeye", "Helius", "Jupiter"):
            self.lightChanged.emit(n, "warn")
        self.lightChanged.emit("Wallet", "grey")
        self.lightChanged.emit("Telegram", "grey")

        helius = HeliusClient(secrets.helius_api_key())
        jupiter = JupiterClient(secrets.jupiter_api_key())
        dex = DexScreenerClient()
        feed = DexScreenerPriceFeed(dex, ttl_seconds=cfg.engine.price_cache_ttl_seconds)
        filters = SafetyFilters(helius, jupiter, dex, RugCheckClient())

        live = cfg.mode == "live" and self._live_keypair is not None
        if live:
            from ..engine.live_executor import LiveExecutor
            from ..engine.pricing import LivePriceFeed
            # Live TP/SL marks come from real Jupiter quotes (not DexScreener,
            # which lags and can't price many memecoins → frozen positions).
            live_feed = LivePriceFeed(jupiter, helius, feed)
            executor = LiveExecutor(self._live_keypair, jupiter, helius, live_feed,
                                    slippage_bps=cfg.trading.slippage_bps,
                                    exit_slippage_bps=cfg.trading.exit_slippage_bps,
                                    max_priority_lamports=cfg.trading.max_priority_lamports,
                                    max_buy_impact_pct=cfg.trading.max_buy_impact_pct,
                                    dynamic_slippage=cfg.trading.dynamic_slippage,
                                    use_sender=cfg.trading.use_sender,
                                    sender_url=cfg.trading.sender_url,
                                    jito_tip_lamports=cfg.trading.jito_tip_lamports)
            engine = PaperEngine(conn, cfg, executor, live_feed, filters=filters,
                                 live_cash_usd=self._live_cash_usd)
            # Seed the price cache from open positions' last DB marks so a (re)start
            # never leaves a position unpriced/frozen before the first live quote
            # lands (a real quote replaces the seed on the next poll).
            try:
                marks = {r["token_mint"]: r["current_price"] for r in conn.execute(
                    "SELECT token_mint, current_price FROM positions "
                    "WHERE mode='live' AND status='open' AND current_price IS NOT NULL").fetchall()}
                live_feed.seed(marks)
            except Exception as e:  # noqa: BLE001
                self.log.debug("price seed skipped: %s", e)
            self.lightChanged.emit("Wallet", "ok")
            self.notify.emit("⚠️ LIVE trading — real funds", "stop")
        else:
            executor = PaperExecutor(feed, slippage_bps=cfg.trading.paper_slippage_bps,
                                     fee_bps=cfg.trading.paper_fee_bps)
            engine = PaperEngine(conn, cfg, executor, feed, filters=filters)

        await self._check_connections(helius, jupiter)

        # LIVE start: adopt any on-chain token the wallet holds that isn't already an
        # open position — an orphaned buy from stopping mid-trade. This guarantees no
        # bought token goes untracked (it'd be invisible AND could get re-bought).
        if live:
            try:
                n = await asyncio.to_thread(engine.adopt_orphan_positions)
                if n:
                    self.notify.emit(f"Adopted {n} untracked holding(s) into Positions", "info")
            except Exception as e:  # noqa: BLE001
                self.log.warning("position adoption failed: %s", e)
            # Inverse of adoption: close any OPEN position whose tokens are gone from
            # the wallet — sold on-chain but not recorded (sold then the app closed
            # too fast), which otherwise stays stuck showing as 'trading'.
            try:
                m = await asyncio.to_thread(engine.reconcile_sold_positions)
                if m:
                    self.notify.emit(
                        f"Reconciled {m} sold position(s) that weren't recorded", "info")
            except Exception as e:  # noqa: BLE001
                self.log.warning("sold-position reconciliation failed: %s", e)

        # LIVE start: wipe the paper-era policing slate so live trades with the
        # CURRENT Birdeye active traders — fresh policing counters. But PRESERVE
        # the owner's benches: paused / under_investigation / dropped / blacklisted
        # traders STAY benched across live restarts (only healthy traders reset).
        if live:
            try:
                conn.execute(
                    "UPDATE traders SET consecutive_losses=0, copied_wins=0, "
                    "copied_losses=0, copied_pnl_usd=0 "
                    "WHERE source='ranked' AND investigation_state IN ('active','probation')")
                conn.commit()
                self.notify.emit("Live start: fresh policing counters (benches kept)", "info")
            except Exception as e:  # noqa: BLE001
                self.log.warning("live trader reset failed: %s", e)

        wallets = self._followed(conn)
        # STARTUP RANKING. The Birdeye leaderboard pull is ~20 rate-limited pages
        # (~30s) — far too slow to make trading wait on it. So if we already have a
        # saved roster, start watching it IMMEDIATELY and pull the fresh ranking in
        # the BACKGROUND (own thread + DB conn); the Birdeye light greens within ~1s
        # via a quick probe. Only a first-ever run (no saved roster) blocks on the
        # pull, since there'd be nothing to watch otherwise.
        if wallets:
            self.notify.emit(
                f"Fast start — watching {len(wallets)} saved wallets; refreshing "
                f"ranking in the background…", "ok")
            self._spawn_initial_rank()
        else:
            self.notify.emit(f"Selecting top {cfg.ranker.follow_top_n} active wallets…", "info")
            try:
                self._pool = await asyncio.to_thread(
                    fetch_candidates, BirdeyeClient(secrets.birdeye_api_key()), cfg)
                ranked = await asyncio.to_thread(select_and_store, self._pool, cfg, conn, helius)
                wallets = [t.address for t in ranked]
                self.lightChanged.emit("Birdeye", "ok")
                self.notify.emit(
                    f"{len(wallets)} active traders online (from {len(self._pool)} ranked)", "ok")
            except Exception as e:  # noqa: BLE001
                self.log.warning("ranking failed: %s", e)
                self.lightChanged.emit("Birdeye", "down")

        if not wallets:
            self.notify.emit("No wallets to watch yet", "stop")
            conn.close()
            return

        nicknames.ensure(conn)   # every followed wallet gets a friendly handle
        watch = self._watch_set(conn) or wallets

        async def on_buy(sig) -> None:
            # Handle each buy in a worker thread so it NEVER blocks the event loop
            # (the manage/sell loop keeps running). The engine's fine-grained lock
            # + in-flight guard keep this safe. Buys are awaited here so they stay
            # serialized (no over-allocation of the wallet), but the await yields
            # the loop so sells run concurrently meanwhile.
            try:
                await asyncio.to_thread(engine.handle_signal, sig)
            except Exception as e:  # noqa: BLE001
                self.log.debug("buy handling error: %s", e)

        monitor = LiveMonitor(helius, watch, on_buy=on_buy)
        self._monitor = monitor
        self.notify.emit(f"Watching {len(watch)} wallets", "ok")

        async def manage() -> None:
            last_snap = 0.0
            loop = asyncio.get_event_loop()
            last_rank = loop.time()
            last_roster = loop.time()
            last_probe = loop.time()
            last_payout = loop.time()
            last_alertfwd = loop.time()
            # Feed heartbeat: prove the feed is alive during quiet stretches so a
            # lull can't be mistaken for a freeze. We record how many raw wallet
            # events flowed vs how many were copyable buys — the Activity Log only
            # shows BUYS, so without this a busy-but-no-buys feed looks dead.
            last_feed_hb = loop.time()
            last_fee_recon = loop.time()
            last_rent = loop.time()
            reclaim_now = False   # set right after an exit → reclaim its rent promptly
            hb_events = monitor.events_seen
            hb_buys = monitor.buys_emitted
            while not self._stop.is_set():
                await asyncio.sleep(cfg.engine.position_poll_seconds)
                self._manage_beat = time.time()   # manage-loop progress (watchdog watches this)
                # Re-measure RPC latency / refresh connection lights (~every 30s).
                if loop.time() - last_probe >= 30:
                    last_probe = loop.time()
                    await self._check_connections(helius, jupiter)
                for mint in self._drain_test_buys():
                    sig = BuySignal("HypeTestTrader1111111111111111111111111111", mint,
                                    "test", utcnow_iso())
                    try:
                        out = await asyncio.to_thread(engine.handle_signal, sig)
                        self.notify.emit(
                            f"Test trade: {'opened' if out.accepted else out.reason}",
                            "ok" if out.accepted else "info")
                    except Exception:  # noqa: BLE001
                        pass
                for pid in self._drain_sells():
                    try:
                        await asyncio.to_thread(engine.sell_position, pid)
                    except Exception:  # noqa: BLE001
                        pass
                # Test stop-loss: open a paper position, then crash its mark price
                # below the −15% stop via a price override so the SAME manage_once
                # below fires the real SL (balance + win rate drop live). Paper only.
                sl_armed: list[str] = []
                for mint in self._drain_test_sls():
                    sig = BuySignal("HypeTestTrader1111111111111111111111111111", mint,
                                    "test-sl", utcnow_iso())
                    try:
                        out = await asyncio.to_thread(engine.handle_signal, sig)
                        if out.accepted and out.position_id:
                            row = conn.execute("SELECT entry_price FROM positions WHERE id=?",
                                               (out.position_id,)).fetchone()
                            if row and row["entry_price"]:
                                feed.set_override(mint, row["entry_price"] * 0.83)  # −17% → trips −15% SL
                                sl_armed.append(mint)
                                self.notify.emit(
                                    "Test stop-loss armed — crashing the position −17%…", "info")
                        else:
                            self.notify.emit(f"Test stop-loss skipped: {out.reason}", "info")
                    except Exception:  # noqa: BLE001
                        pass
                # Manage exits CONCURRENTLY: plan (fast) → sell each in parallel
                # so many TP/SL/trailing exits fire at once, never one-at-a-time.
                try:
                    plan = await asyncio.to_thread(engine.plan_closes)
                    if plan:
                        await asyncio.gather(
                            *[asyncio.to_thread(engine.execute_close, pos, reason)
                              for pos, reason in plan],
                            return_exceptions=True)
                        reclaim_now = True   # an exit emptied an account → reclaim soon
                except Exception:  # noqa: BLE001
                    pass
                # Pre-sign exits for ARMED runners (profit past trail_activate) so the
                # trailing sell fires instantly and captures near the peak. Runs
                # UNLOCKED in a worker (it hits the network); throttled inside.
                if live and hasattr(executor, "prewarm_exit"):
                    try:
                        await asyncio.to_thread(self._prewarm_armed, executor, cfg)
                    except Exception:  # noqa: BLE001
                        pass
                for mint in sl_armed:   # one-shot: restore real pricing after the SL fired
                    feed.clear_override(mint)
                # Push new activity to Telegram (~every 3s) respecting toggles.
                if self.telegram is not None and loop.time() - last_alertfwd >= 3:
                    last_alertfwd = loop.time()
                    try:
                        await asyncio.to_thread(self._forward_alerts, conn)
                    except Exception:  # noqa: BLE001
                        pass
                # Auto-payout sweep (~every 5 min check; live only, off by default).
                if live and loop.time() - last_payout >= 300:
                    last_payout = loop.time()
                    try:
                        await asyncio.to_thread(self._maybe_payout, conn)
                    except Exception:  # noqa: BLE001
                        pass
                # Feed heartbeat (~every 2 min): only emit during a lull (no
                # copyable buys in the window) so it explains the quiet instead
                # of cluttering active periods. Distinguishes "wallets active but
                # nothing worth copying" (events > 0) from "feed truly dead"
                # (events = 0 → the idle-timeout will also be reconnecting).
                if loop.time() - last_feed_hb >= 120:
                    d_events = monitor.events_seen - hb_events
                    d_buys = monitor.buys_emitted - hb_buys
                    if d_buys == 0:
                        idle = monitor.seconds_since_message()
                        state = "alive" if d_events > 0 or idle < 90 else "SILENT"
                        log_activity(
                            conn, "feed",
                            f"feed {state} — {d_events} wallet events, 0 copyable "
                            f"buys in last 2m (idle {idle:.0f}s, watching "
                            f"{len(monitor.wallets)})")
                    last_feed_hb = loop.time()
                    hb_events = monitor.events_seen
                    hb_buys = monitor.buys_emitted
                # Periodic equity point (~every 30s) for a continuous curve.
                if loop.time() - last_snap >= 30:
                    last_snap = loop.time()
                    try:
                        await asyncio.to_thread(engine.snapshot_now)
                    except Exception:  # noqa: BLE001
                        pass
                # Live fee reconciliation (~every 45s): back-fill the REAL on-chain
                # fee (base + priority) for live trades still recorded at 0, so the
                # Performance tab's Fees tile shows the true gas bleed. Runs in a
                # worker with its own DB connection → zero added trade latency.
                if live and (loop.time() - last_fee_recon) >= 45:
                    last_fee_recon = loop.time()
                    try:
                        await asyncio.to_thread(self._reconcile_fees, executor)
                    except Exception:  # noqa: BLE001
                        pass
                # Auto-reclaim token-account rent (~every 15 min): each new-token buy
                # locks ~0.002 SOL in an ATA that the sell doesn't close. Closing the
                # empty ones refunds that SOL to the wallet — net hugely positive
                # (reclaims ~0.002 SOL/acct vs a ~0.000005 SOL tx fee). Refund goes
                # only to the wallet itself (no external transfer).
                # Fire right after an exit (throttled to 60s) so an emptied account's
                # rent comes back promptly, plus a 30-min baseline sweep.
                due = (loop.time() - last_rent) >= 1800 or (
                    reclaim_now and (loop.time() - last_rent) >= 60)
                if live and self._live_keypair is not None and due:
                    last_rent = loop.time()
                    reclaim_now = False
                    try:
                        from ..engine.rent import reclaim_locked
                        price = self.wallet_ctrl.sol_price_usd if self.wallet_ctrl else 0.0
                        # Never burn/close an OPEN position's token account.
                        open_mints = {r["token_mint"] for r in conn.execute(
                            "SELECT token_mint FROM positions WHERE mode='live' AND status='open'").fetchall()}
                        res = await asyncio.to_thread(
                            reclaim_locked, helius, self._live_keypair,
                            jupiter=jupiter, sol_price_usd=price or 0.0, skip_mints=open_mints)
                        if res.closed:
                            log_activity(conn, "system",
                                         f"Reclaimed {res.reclaimed_sol:.4f} SOL rent from "
                                         f"{res.closed} token account(s) (empties + dust)")
                            self.notify.emit(
                                f"Reclaimed {res.reclaimed_sol:.4f} SOL locked rent", "info")
                    except Exception as e:  # noqa: BLE001
                        self.log.debug("rent reclaim skipped: %s", e)
                # Cheap watch-set refresh (manual add/remove/enable/disable, or
                # an investigation action changed who we should be copying).
                if self._watch_refresh.is_set():
                    self._watch_refresh.clear()
                    try:
                        monitor.update_wallets(self._watch_set(conn))
                        self.tradersChanged.emit()
                    except Exception:  # noqa: BLE001
                        pass
                # Roster maintenance (~every roster_refresh_minutes): re-select the
                # top-N ACTIVE traders from the cached pool using cheap Helius
                # recency checks — NO Birdeye call. This is what keeps N traders
                # online at all times: a wallet that goes idle is demoted and the
                # next-ranked active one is promoted in its place.
                roster_iv = max(60, cfg.ranker.roster_refresh_minutes * 60)
                if self._pool and (loop.time() - last_roster) >= roster_iv:
                    last_roster = loop.time()
                    try:
                        ranked = await asyncio.to_thread(
                            select_and_store, self._pool, cfg, conn, helius)
                        nicknames.ensure(conn)
                        monitor.update_wallets(self._watch_set(conn))
                        self.tradersChanged.emit()
                        self.log.info("Roster: %d active traders online (pool %d).",
                                      len(ranked), len(self._pool))
                    except Exception:  # noqa: BLE001
                        pass
                # Birdeye leaderboard refresh (hourly / on request): refresh WHO is
                # eligible (the cached pool). Roster maintenance above then keeps
                # the active set fresh between these.
                interval = max(60, cfg.ranker.rank_refresh_minutes * 60)
                if self._rerank.is_set() or (loop.time() - last_rank) >= interval:
                    self._rerank.clear()
                    last_rank = loop.time()
                    try:
                        self._pool = await asyncio.to_thread(
                            fetch_candidates, BirdeyeClient(secrets.birdeye_api_key()), cfg)
                        ranked = await asyncio.to_thread(
                            select_and_store, self._pool, cfg, conn, helius)
                        last_roster = loop.time()
                        nicknames.ensure(conn)
                        monitor.update_wallets(self._watch_set(conn))
                        self.notify.emit(
                            f"Re-ranked — {len(ranked)} active traders online "
                            f"(pool {len(self._pool)})", "ok")
                        self.tradersChanged.emit()
                    except Exception as e:  # noqa: BLE001
                        self.log.warning("re-rank failed: %s", e)
                        self.lightChanged.emit("Birdeye", "warn")
                        self.notify.emit(
                            "Ranking refresh skipped (Birdeye quota) — still trading "
                            "current wallets", "info")

        mt = asyncio.create_task(manage())
        hb = asyncio.create_task(self._heartbeat_loop())
        try:
            await monitor.run()
        finally:
            mt.cancel()
            hb.cancel()
            conn.close()

    async def _heartbeat_loop(self) -> None:
        """Independent liveness beat — the ONLY thing the watchdog should measure.

        It bumps _last_heartbeat every few seconds for as long as the engine's
        event loop is alive. Because it's decoupled from the manage loop's heavy
        awaited work (Birdeye/Helius/Jupiter calls that can retry-backoff for well
        over a minute), a slow network
        call can NEVER make the heartbeat look stale and trigger a spurious full
        restart. If the event loop itself dies (thread crash), the beat stops and
        the watchdog correctly recovers. This is the fix for Hype stopping and
        reconnecting all three data feeds by itself."""
        try:
            while not self._stop.is_set():
                self._last_heartbeat = time.time()
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    async def _check_connections(self, helius: HeliusClient, jupiter: JupiterClient) -> None:
        # Hard-bound each probe (~15s). The underlying HTTP client can retry-backoff
        # for well over a minute on a hiccup; wait_for lets the manage loop move on
        # instead of hanging here (the probe's own thread finishes harmlessly). This
        # keeps exits responsive and the loop cycling during a network wobble.
        try:
            t = time.perf_counter()
            await asyncio.wait_for(asyncio.to_thread(helius._rpc, "getHealth", []), timeout=15)
            self._helius_ms = (time.perf_counter() - t) * 1000.0
            self.lightChanged.emit("Helius", "ok")
        except Exception:  # noqa: BLE001  (includes TimeoutError)
            self._helius_ms = None
            self.lightChanged.emit("Helius", "down")
        try:
            t = time.perf_counter()
            ok, _ = await asyncio.wait_for(
                asyncio.to_thread(jupiter.simulate_sell, BONK, 100_000_000, slippage_bps=200),
                timeout=15)
            self._jupiter_ms = (time.perf_counter() - t) * 1000.0
            self.lightChanged.emit("Jupiter", "ok" if ok else "down")
        except Exception:  # noqa: BLE001  (includes TimeoutError)
            self._jupiter_ms = None
            self.lightChanged.emit("Jupiter", "down")

    @staticmethod
    def _followed(conn) -> list[str]:
        rows = conn.execute(
            "SELECT wallet_address FROM traders WHERE active=1 AND source='ranked' "
            "ORDER BY realized_pnl_usd DESC").fetchall()
        return [r["wallet_address"] for r in rows]

    @staticmethod
    def _watch_set(conn) -> list[str]:
        """Wallets the monitor should subscribe to: every enabled trader that
        isn't dropped (ranked + manually added). Copyability is still enforced
        per-signal, so paused/investigated wallets are watched but not copied."""
        rows = conn.execute(
            "SELECT wallet_address FROM traders WHERE active=1 "
            "AND investigation_state NOT IN ('dropped','blacklisted') "
            "ORDER BY (realized_pnl_usd IS NULL), realized_pnl_usd DESC").fetchall()
        return [r["wallet_address"] for r in rows]
