"""WalletController — the trading wallet's dashboard state (§3, Phase 6 CP1).

Owns the WalletManager, polls the live SOL balance (deposit auto-detect), and
drives the sidebar 'Wallet' light + the top-bar live balance via Qt signals.
Key material never passes through a signal — only the public address and the
balance. Backup/restore are explicit, auth-gated method calls.
"""

from __future__ import annotations

import threading
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .. import secrets
from ..config import load_config
from ..data.dexscreener import DexScreenerClient
from ..data.helius import HeliusClient
from ..logging_setup import get_logger
from ..wallet import WalletManager, WalletError

SOL_MINT = "So11111111111111111111111111111111111111112"


class WalletController(QObject):
    lightChanged = pyqtSignal(str, str)   # ("Wallet", state)
    notify = pyqtSignal(str, str)         # (message, kind)
    changed = pyqtSignal()                # wallet created/deleted/balance updated

    def __init__(self) -> None:
        super().__init__()
        self.log = get_logger()
        self.wallet = WalletManager()
        self.sol_balance = 0.0
        self.home_balance = 0.0          # MetaMask home wallet SOL (payout destination)
        self.home_address: Optional[str] = None
        self.sol_price_usd: Optional[float] = None
        self._dex = DexScreenerClient()
        # Tiered polling: SOL PRICE every 1s (DexScreener, free → USD ticks live),
        # trading-wallet balance every 5s (Helius, catches trades fast), home
        # wallet every 30s (rarely changes). Keeps Helius usage to ~6% of quota
        # while the displayed balance feels real-time.
        self._tick = 0
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._on_tick)
        self._poll.start(1_000)

    # --- state ---------------------------------------------------------------

    def start(self) -> None:
        """Set the initial light + kick a first full read (price + balances)."""
        self._emit_light()
        self._refresh_async()

    def exists(self) -> bool:
        return self.wallet.exists()

    def address(self) -> Optional[str]:
        return self.wallet.address()

    def usd_balance(self) -> Optional[float]:
        if self.sol_price_usd is None:
            return None
        return self.sol_balance * self.sol_price_usd

    def home_usd_balance(self) -> Optional[float]:
        if self.sol_price_usd is None:
            return None
        return self.home_balance * self.sol_price_usd

    def stats(self) -> dict:
        return {
            "exists": self.wallet.exists(),
            "address": self.wallet.address(),
            "sol_balance": self.sol_balance,
            "sol_price_usd": self.sol_price_usd,
            "usd_balance": self.usd_balance(),
            "home_address": self.home_address,
            "home_balance": self.home_balance,
            "home_usd_balance": self.home_usd_balance(),
        }

    def _emit_light(self) -> None:
        self.lightChanged.emit("Wallet", "ok" if self.wallet.exists() else "grey")

    # --- lifecycle actions (called from the Settings UI) ---------------------

    def create_wallet(self) -> Optional[str]:
        try:
            addr = self.wallet.create()
        except WalletError as e:
            self.notify.emit(str(e), "stop")
            return None
        self.notify.emit("Trading wallet created — fund it, then back it up", "ok")
        self._emit_light()
        self.changed.emit()
        self._refresh_async()
        return addr

    def restore_wallet(self, secret: str) -> Optional[str]:
        try:
            addr = self.wallet.import_secret(secret)
        except WalletError as e:
            self.notify.emit(f"Restore failed: {e}", "stop")
            return None
        self.notify.emit("Trading wallet restored", "ok")
        self._emit_light()
        self.changed.emit()
        self._refresh_async()
        return addr

    def delete_wallet(self) -> None:
        self.wallet.delete()
        self.sol_balance = 0.0
        self.notify.emit("Trading wallet removed from this Mac", "info")
        self._emit_light()
        self.changed.emit()

    def reveal_backup(self) -> Optional[str]:
        """Auth-gated (Touch ID) reveal of the base58 backup key."""
        try:
            return self.wallet.reveal_backup(reason="Reveal your trading wallet backup key")
        except Exception as e:  # noqa: BLE001
            self.notify.emit(f"Backup unavailable: {e}", "stop")
            return None

    def test_live_buy(self, usd_amount: float, mint: str = SOL_MINT) -> Optional[str]:
        """Do ONE real Jupiter swap (SOL→token) to prove live execution on-chain.
        Loads the key (Touch ID), builds a LiveExecutor, buys ~`usd_amount` of the
        token, logs the tx + a Solscan link. Returns the signature or None."""
        from .. import db
        from ..data.jupiter import JupiterClient
        from ..data.helius import HeliusClient
        from ..engine.executor import ExecutionError
        from ..engine.live_executor import LiveExecutor
        from ..engine.pricing import DexScreenerPriceFeed
        from ..logging_setup import log_activity

        BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        if mint == SOL_MINT:
            mint = BONK   # can't buy SOL with SOL; default the test to a liquid token
        if not self.wallet.exists():
            self.notify.emit("Create + fund the trading wallet first", "stop")
            return None
        cfg = load_config()
        try:
            keypair = self.wallet.load_keypair(reason="Authorize a live test trade")
            jup = JupiterClient(secrets.jupiter_api_key())
            helius = HeliusClient(secrets.helius_api_key())
            feed = DexScreenerPriceFeed(self._dex, ttl_seconds=5)
            ex = LiveExecutor(keypair, jup, helius, feed, slippage_bps=cfg.trading.slippage_bps)
        except Exception as e:  # noqa: BLE001
            self.notify.emit(f"Live test blocked: {e}", "stop")
            return None
        try:
            fill = ex.buy(mint, usd_amount)
        except ExecutionError as e:
            self.notify.emit(f"Live buy failed: {e}", "stop")
            return None
        try:
            conn = db.connect()
            log_activity(conn, "live",
                         f"LIVE test buy: {fill.qty:,.4g} of {mint[:6]}… for ~${usd_amount:.2f}",
                         mint=mint, signature=fill.tx_sig,
                         solscan=f"https://solscan.io/tx/{fill.tx_sig}", executor="live")
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.notify.emit(f"Live buy sent ✅ — verify on Solscan (tx {fill.tx_sig[:8]}…)", "ok")
        self._refresh_async()
        return fill.tx_sig

    def sell_all_to_sol(self) -> int:
        """Sell every SPL token the trading wallet holds back to SOL (recovers a
        test-buy or any stray holdings). Returns how many tokens were sold."""
        from .. import db
        from ..data.jupiter import JupiterClient
        from ..data.helius import HeliusClient
        from ..engine.executor import ExecutionError
        from ..engine.live_executor import LiveExecutor
        from ..engine.pricing import DexScreenerPriceFeed
        from ..logging_setup import log_activity

        if not self.wallet.exists():
            self.notify.emit("No trading wallet", "stop")
            return 0
        cfg = load_config()
        try:
            keypair = self.wallet.load_keypair(reason="Authorize selling tokens back to SOL")
            jup = JupiterClient(secrets.jupiter_api_key())
            helius = HeliusClient(secrets.helius_api_key())
            feed = DexScreenerPriceFeed(self._dex, ttl_seconds=5)
            ex = LiveExecutor(keypair, jup, helius, feed, slippage_bps=cfg.trading.slippage_bps)
            holdings = ex.token_holdings()
        except Exception as e:  # noqa: BLE001
            self.notify.emit(f"Sell blocked: {e}", "stop")
            return 0
        if not holdings:
            self.notify.emit("No tokens to sell — wallet holds only SOL", "info")
            return 0
        sold = 0
        conn = db.connect()
        for mint, base, dec in holdings:
            try:
                fill = ex.sell_base(mint, base, dec)
                log_activity(conn, "live",
                             f"LIVE sell → SOL: {fill.qty:,.4g} of {mint[:6]}… for ~${fill.usd_value:.2f}",
                             mint=mint, signature=fill.tx_sig,
                             solscan=f"https://solscan.io/tx/{fill.tx_sig}", executor="live")
                sold += 1
                self.notify.emit(f"Sold {mint[:6]}… back to SOL ✅ (tx {fill.tx_sig[:8]}…)", "ok")
            except ExecutionError as e:
                self.notify.emit(f"Sell failed for {mint[:6]}…: {e}", "stop")
        conn.close()
        self._refresh_async()
        return sold

    def reclaim_rent(self) -> int:
        """Fully recover locked money to THIS wallet (never an external address):
        (1) SELL any dust that still has value back to SOL, (2) burn worthless dust,
        (3) close all empty accounts to refund their rent. Open positions are never
        touched. Loads the key (Touch ID) on the main thread, then works off-thread
        so the UI never freezes. Returns 1 if it started, 0 on failure."""
        import threading
        from .. import db
        from ..data.helius import HeliusClient
        from ..data.jupiter import JupiterClient
        from ..engine.executor import ExecutionError
        from ..engine.live_executor import LiveExecutor
        from ..engine.pricing import DexScreenerPriceFeed
        from ..engine.rent import reclaim_locked, RentError

        if not self.wallet.exists():
            self.notify.emit("No trading wallet", "stop")
            return 0
        try:
            keypair = self.wallet.load_keypair(reason="Authorize reclaiming locked money")
        except Exception as e:  # noqa: BLE001
            self.notify.emit(f"Reclaim blocked: {e}", "stop")
            return 0

        def _worker() -> None:
            try:
                helius = HeliusClient(secrets.helius_api_key())
                jup = JupiterClient(secrets.jupiter_api_key())
                # Never touch tokens that belong to an OPEN live position.
                conn = db.connect()
                open_mints = {r["token_mint"] for r in conn.execute(
                    "SELECT token_mint FROM positions WHERE mode='live' AND status='open'").fetchall()}
                conn.close()
                # 1) sell any dust that still has value (skip open positions). Dust
                #    with no route just raises here → burned in step 2.
                feed = DexScreenerPriceFeed(self._dex, ttl_seconds=5)
                ex = LiveExecutor(keypair, jup, helius, feed,
                                  slippage_bps=load_config().trading.slippage_bps)
                sold = 0
                for mint, base, dec in ex.token_holdings():
                    if mint in open_mints:
                        continue
                    try:
                        ex.sell_base(mint, base, dec)
                        sold += 1
                    except ExecutionError:
                        pass   # no route → worthless; step 2 burns it
                # 2+3) burn worthless dust + close every empty account (refund rent).
                res = reclaim_locked(helius, keypair, jupiter=jup,
                                     sol_price_usd=self.sol_price_usd or 0.0,
                                     skip_mints=open_mints)
                if res.closed == 0 and sold == 0:
                    self.notify.emit("Nothing locked to reclaim right now", "info")
                else:
                    parts = []
                    if sold:
                        parts.append(f"sold {sold} dust")
                    if res.closed:
                        parts.append(f"reclaimed {res.reclaimed_sol:.4f} SOL rent from {res.closed} acct(s)")
                    self.notify.emit("✅ " + " · ".join(parts), "ok")
                    self._refresh_async()
            except (RentError, Exception) as e:  # noqa: BLE001
                self.notify.emit(f"Reclaim failed: {e}", "stop")

        threading.Thread(target=_worker, daemon=True).start()
        return 1

    def funds_breakdown(self) -> Optional[dict]:
        """Live locked-money snapshot for the System → Funds tab (read-only)."""
        from ..data.helius import HeliusClient
        from ..data.jupiter import JupiterClient
        from ..engine.rent import funds_breakdown
        addr = self.wallet.address()
        if not addr:
            return None
        try:
            return funds_breakdown(HeliusClient(secrets.helius_api_key()), addr,
                                   jupiter=JupiterClient(secrets.jupiter_api_key()),
                                   sol_price_usd=self.sol_price_usd or 0.0)
        except Exception:  # noqa: BLE001
            return None

    def panic_drain(self) -> bool:
        """Emergency: sell every token to SOL, then sweep ALL SOL (bar a tiny fee
        buffer) to the home wallet — the only allowed destination. Loads the key
        (Touch ID) on the main thread, then runs the swaps + sweep off-thread so
        the UI never freezes. Returns True if it started."""
        home = load_config().payout.home_wallet_address
        if not home:
            self.notify.emit("Set your home wallet before draining", "stop")
            return False
        if not self.wallet.exists():
            self.notify.emit("No trading wallet", "stop")
            return False
        try:
            keypair = self.wallet.load_keypair(reason="Authorize PANIC DRAIN to your home wallet")
        except Exception as e:  # noqa: BLE001
            self.notify.emit(f"Panic drain blocked: {e}", "stop")
            return False
        return self.panic_drain_with_key(keypair)

    def panic_drain_with_key(self, keypair) -> bool:
        """Drain using an already-unlocked keypair (dashboard after Touch ID, or
        Telegram /panic reusing the live engine's in-memory key — no re-prompt)."""
        home = load_config().payout.home_wallet_address
        if not home:
            self.notify.emit("Set your home wallet before draining", "stop")
            return False
        self.notify.emit("🚨 Panic drain started — selling + sweeping to home…", "stop")
        threading.Thread(target=self._panic_worker, args=(keypair, home), daemon=True).start()
        return True

    def _panic_worker(self, keypair, home: str) -> None:
        import time as _t
        from .. import db
        from ..data.jupiter import JupiterClient
        from ..data.helius import HeliusClient
        from ..engine.executor import ExecutionError
        from ..engine.live_executor import LiveExecutor
        from ..engine.pricing import DexScreenerPriceFeed
        from ..engine.transfer import send_sol, TransferError, TX_FEE_BUFFER_LAMPORTS
        from ..logging_setup import log_activity

        try:
            cfg = load_config()
            jup = JupiterClient(secrets.jupiter_api_key())
            helius = HeliusClient(secrets.helius_api_key())
            feed = DexScreenerPriceFeed(self._dex, ttl_seconds=5)
            ex = LiveExecutor(keypair, jup, helius, feed, slippage_bps=cfg.trading.slippage_bps,
                              max_priority_lamports=cfg.trading.max_priority_lamports)
            conn = db.connect()
            # 1. sell every held token → SOL
            sigs = []
            for mint, base, dec in ex.token_holdings():
                try:
                    f = ex.sell_base(mint, base, dec)
                    sigs.append(f.tx_sig)
                    log_activity(conn, "panic", f"drain: sold {mint[:6]}… → SOL", mint=mint,
                                 signature=f.tx_sig)
                except ExecutionError as e:
                    log_activity(conn, "panic", f"drain: sell {mint[:6]}… FAILED: {e}", level="WARNING")
            # 2. wait (best-effort) for the sells to confirm so their SOL is present
            if sigs:
                self._wait_confirm(helius, sigs, timeout=25)
            # 3. sweep all SOL (minus a fee buffer) to the home wallet
            sol = self.wallet.sol_balance(helius)
            lamports = int(sol * 1_000_000_000) - TX_FEE_BUFFER_LAMPORTS * 3
            if lamports <= 0:
                self.notify.emit("Panic drain: nothing to sweep after sells", "info")
                conn.close(); return
            res = send_sol(helius, keypair, home, lamports)
            log_activity(conn, "panic", f"DRAINED {res.sol:.4f} SOL to home wallet",
                         home=home, signature=res.signature,
                         solscan=f"https://solscan.io/tx/{res.signature}")
            conn.close()
            self.notify.emit(f"🚨 Panic drain complete — {res.sol:.4f} SOL swept to MetaMask ✅", "ok")
        except (TransferError, Exception) as e:  # noqa: BLE001
            self.notify.emit(f"Panic drain error: {e}", "stop")
        self._refresh_async()

    @staticmethod
    def _wait_confirm(helius, sigs: list, timeout: float = 25.0) -> None:
        import time as _t
        deadline = _t.monotonic() + timeout
        pending = [s for s in sigs if s]
        while pending and _t.monotonic() < deadline:
            try:
                res = helius._rpc("getSignatureStatuses", [pending, {"searchTransactionHistory": False}])
                statuses = (res or {}).get("value", [])
                still = []
                for sig, st in zip(pending, statuses):
                    cs = (st or {}).get("confirmationStatus")
                    if cs not in ("confirmed", "finalized"):
                        still.append(sig)
                pending = still
            except Exception:  # noqa: BLE001
                pass
            if pending:
                _t.sleep(1.5)

    def max_withdrawable_sol(self) -> float:
        """Trading-wallet SOL that can be sent out, keeping the gas reserve."""
        reserve = load_config().trading.gas_reserve_sol
        return max(0.0, self.sol_balance - reserve)

    def send_to_home(self, amount_sol: float) -> Optional[str]:
        """Send SOL from the trading wallet to the ALLOWLISTED home wallet only.
        The destination is read from config here — never passed in from the UI —
        so there is no arbitrary-send path. Returns the tx signature or None.

        Logs the withdrawal to the activity feed and drops a 'withdraw' marker on
        the live equity curve, so payouts show on both the graph and the log."""
        from .. import db
        from ..data.helius import HeliusClient
        from ..engine.account import Account, Equity
        from ..engine.transfer import send_sol, TransferError
        from ..logging_setup import log_activity
        from ..wallet import LAMPORTS_PER_SOL

        cfg = load_config()
        home = cfg.payout.home_wallet_address
        if not home:
            self.notify.emit("Set your home wallet in Settings before withdrawing", "stop")
            return None
        cap = self.max_withdrawable_sol()
        if amount_sol <= 0 or amount_sol > cap + 1e-9:
            self.notify.emit(f"Amount must be between 0 and {cap:.4f} SOL (keeps gas reserve)", "stop")
            return None
        lamports = int(round(amount_sol * LAMPORTS_PER_SOL))
        try:
            helius = HeliusClient(secrets.helius_api_key())
            keypair = self.wallet.load_keypair(reason="Authorize withdrawal to your home wallet")
        except Exception as e:  # noqa: BLE001
            self.notify.emit(f"Withdrawal blocked: {e}", "stop")
            return None
        try:
            result = send_sol(helius, keypair, home, lamports)
        except TransferError as e:
            self.notify.emit(f"Withdrawal failed: {e}", "stop")
            return None

        # Record it: activity log + a 'withdraw' marker on the live equity curve.
        try:
            conn = db.connect()
            usd = (result.sol * self.sol_price_usd) if self.sol_price_usd else None
            usd_txt = f" (~${usd:,.2f})" if usd is not None else ""
            log_activity(conn, "withdraw",
                         f"sent {result.sol:.4f} SOL{usd_txt} to home wallet",
                         mint=None, home=home, signature=result.signature, sol=result.sol)
            self._mark_withdraw_on_curve(conn, Account, Equity, result.sol, usd)
            conn.close()
        except Exception as e:  # noqa: BLE001
            self.log.debug("withdraw logging failed: %s", e)

        self.notify.emit(f"Withdrew {result.sol:.4f} SOL to MetaMask ✅", "ok")
        self._refresh_async()
        return result.signature

    def _mark_withdraw_on_curve(self, conn, Account, Equity, sol: float, usd) -> None:
        """Ensure a live session exists and drop a withdraw marker + point so the
        Live performance graph shows the payout."""
        acct = Account(conn, "live")
        start_usd = (self.sol_balance * self.sol_price_usd) if self.sol_price_usd else self.sol_balance
        sid = acct.get_or_start_session(start_usd)
        bal_usd = (self.sol_balance * self.sol_price_usd) if self.sol_price_usd else self.sol_balance
        eq = Equity(starting_balance_usd=start_usd, cash_usd=bal_usd, open_value_usd=0.0,
                    realized_pnl_usd=0.0, unrealized_pnl_usd=0.0, open_positions=0)
        acct.snapshot(sid, eq, marker="withdraw",
                      note=f"withdraw {sol:.4f} SOL to home wallet")

    # --- balance polling -----------------------------------------------------

    def _on_tick(self) -> None:
        """1s timer: refresh the SOL PRICE every tick (free, DexScreener → the USD
        balance ticks live), the trading balance every 5s and the home wallet
        every 30s (Helius — kept light on quota)."""
        self._tick += 1
        self.home_address = load_config().payout.home_wallet_address or None
        do_balance = (self._tick % 5 == 0) and self.wallet.exists()
        do_home = (self._tick % 30 == 0) and bool(self.home_address)
        threading.Thread(target=self._refresh, args=(True, do_balance, do_home), daemon=True).start()

    def _refresh_async(self) -> None:
        """Force a full refresh now (after a trade / wallet action)."""
        self.home_address = load_config().payout.home_wallet_address or None
        threading.Thread(target=self._refresh,
                         args=(True, self.wallet.exists(), bool(self.home_address)),
                         daemon=True).start()

    def _refresh(self, do_price: bool = True, do_balance: bool = True, do_home: bool = True) -> None:
        bal = self.sol_balance
        home = self.home_balance
        price = self.sol_price_usd
        if do_balance or do_home:
            try:
                helius = HeliusClient(secrets.helius_api_key())
            except Exception:  # noqa: BLE001
                helius = None
            if helius is not None:
                if do_balance and self.wallet.exists():
                    try:
                        bal = self.wallet.sol_balance(helius)
                    except Exception:  # noqa: BLE001
                        pass
                if do_home and self.home_address:
                    try:
                        home = self.wallet.sol_balance_of(helius, self.home_address)
                    except Exception:  # noqa: BLE001
                        pass
        if do_price:
            try:
                p = self._dex.market(SOL_MINT).get("price_usd")
                if p:
                    price = p
            except Exception:  # noqa: BLE001
                pass
        changed = (abs(bal - self.sol_balance) > 1e-9 or abs(home - self.home_balance) > 1e-9
                   or price != self.sol_price_usd)
        self.sol_balance = bal
        self.home_balance = home
        if price:
            self.sol_price_usd = price
        if changed:
            self.changed.emit()
