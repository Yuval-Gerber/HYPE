"""LiveExecutor (Phase 6 CP2) — real Jupiter swaps for the `Executor` seam.

Same interface as PaperExecutor, so the engine/sizing/position-manager are
unchanged (§11). Trades against SOL (Solana memecoins pair with SOL): a buy is
SOL→token, a sell is token→SOL. The wallet's Keypair is loaded ONCE at live
start (Touch ID) and held here for the session so autonomous trading doesn't
re-prompt (§6.1: decrypted into memory once at authorized startup).

Every trade: Jupiter quote → build swap → SIGN → **simulateTransaction** →
sendTransaction (Helius). We never send a tx the RPC rejects in simulation.
"""

from __future__ import annotations

import base64
import time
from typing import Optional

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from ..data.jupiter import SOL_MINT, JupiterClient
from ..logging_setup import get_logger
from .executor import ExecutionError, Executor, Fill
from .pricing import PriceFeed

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class LiveExecutor(Executor):
    name = "live"

    def __init__(self, keypair: Keypair, jupiter: JupiterClient, helius, price_feed: PriceFeed,
                 *, slippage_bps: int = 200, exit_slippage_bps: int = 500,
                 max_priority_lamports: int = 1_000_000, max_buy_impact_pct: float = 1.5,
                 dynamic_slippage: bool = True, use_sender: bool = True,
                 sender_url: str = "https://sender.helius-rpc.com/fast",
                 jito_tip_lamports: int = 5_000) -> None:
        self.keypair = keypair
        self.pubkey = str(keypair.pubkey())
        self.jupiter = jupiter
        self.helius = helius
        self.feed = price_feed
        self.slippage_bps = slippage_bps
        self.exit_slippage_bps = exit_slippage_bps
        self.max_priority_lamports = max_priority_lamports
        self.max_buy_impact_pct = max_buy_impact_pct
        self.dynamic_slippage = dynamic_slippage
        self.use_sender = use_sender
        self.sender_url = sender_url
        self.jito_tip_lamports = jito_tip_lamports
        self.log = get_logger()
        self._decimals: dict[str, int] = {}
        self._pcache: dict[str, tuple[float, float]] = {}   # sell-side mark cache
        self._pcache_ttl = 1.0
        self._warm: dict[str, dict] = {}   # mint -> pre-signed exit tx (armed runners)

    # --- marks (for TP/SL polling) -------------------------------------------

    def get_price_usd(self, token_mint: str) -> Optional[float]:
        return self.feed.get_price_usd(token_mint)

    def position_price_usd(self, token_mint: str, qty: float) -> Optional[float]:
        """The TRUE per-token exit price = a real Jupiter SELL quote of the held
        qty (token→SOL), so TP/SL trigger on what you'd ACTUALLY realize — not an
        optimistic buy-side mark. Cached ~1s. Returns None → caller falls back to
        the feed (which then handles rugs)."""
        if not qty or qty <= 0:
            return None
        now = time.monotonic()
        hit = self._pcache.get(token_mint)
        if hit and (now - hit[0]) < self._pcache_ttl:
            return hit[1]
        dec = self.decimals(token_mint)
        base = int(round(qty * (10 ** dec))) if dec else int(round(qty))
        if base <= 0:
            return None
        try:
            q = self.jupiter.quote(token_mint, SOL_MINT, base, slippage_bps=self.exit_slippage_bps)
        except Exception:  # noqa: BLE001
            return None
        if not q or not q.get("outAmount"):
            return None
        sol_out = int(q["outAmount"]) / 1e9
        price = (sol_out * self._sol_price()) / qty
        self._pcache[token_mint] = (now, price)
        return price

    def _sol_price(self) -> float:
        p = self.feed.get_price_usd(SOL_MINT)
        if not p or p <= 0:
            raise ExecutionError("no SOL price available")
        return p

    def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            info = self.helius.get_mint_info(mint)
            self._decimals[mint] = int(info.get("decimals", 0))
        return self._decimals[mint]

    # --- trading -------------------------------------------------------------

    def buy(self, token_mint: str, usd_to_spend: float) -> Fill:
        sol_price = self._sol_price()
        lamports_in = int(round((usd_to_spend / sol_price) * 1e9))
        if lamports_in <= 0:
            raise ExecutionError("buy amount rounds to zero SOL")
        quote = self.jupiter.quote(SOL_MINT, token_mint, lamports_in, slippage_bps=self.slippage_bps)
        if quote is None:
            raise ExecutionError("no swap route SOL→token")
        # Cap price impact at the source: a thin pool that would slip the fill badly
        # is rejected here (uses the same quote — no extra network call, no latency).
        if self.max_buy_impact_pct > 0:
            impact = float(quote.get("priceImpactPct") or 0.0) * 100.0
            if impact > self.max_buy_impact_pct:
                raise ExecutionError(
                    f"buy impact {impact:.2f}% > {self.max_buy_impact_pct:.2f}% cap (thin pool)")
        # out_amount = ACTUAL tokens received on-chain (from the confirmed tx),
        # not the optimistic quote — so entry qty/price reflect the real fill.
        sig, out_amount, fee_lamports = self._execute(quote, f"buy {token_mint}", token_mint)
        dec = self.decimals(token_mint)
        qty = int(out_amount) / (10 ** dec) if dec else float(out_amount)
        if qty <= 0:
            raise ExecutionError("buy confirmed but received 0 tokens")
        price = usd_to_spend / qty
        fee_usd = (fee_lamports / 1e9) * sol_price
        return Fill(side="buy", token_mint=token_mint, qty=qty, price_usd=price,
                    usd_value=usd_to_spend, fees_usd=fee_usd, slippage_bps=self.slippage_bps,
                    executor=self.name, tx_sig=sig)

    def _build_signed_sell(self, mint: str, base_units: int):
        """Quote token→SOL and build+sign a full-exit tx (dynamic slippage + Jito
        tip). Returns (raw_b64, report). Shared by sell() and prewarm_exit()."""
        quote = self.jupiter.quote(mint, SOL_MINT, base_units, slippage_bps=self.exit_slippage_bps)
        if quote is None:
            raise ExecutionError("no swap route token→SOL")
        swap_b64, report = self.jupiter.build_swap(
            quote, self.pubkey, max_priority_lamports=self.max_priority_lamports,
            dynamic_slippage=self.dynamic_slippage,
            dynamic_slippage_max_bps=self.exit_slippage_bps if self.dynamic_slippage else None,
            jito_tip_lamports=self.jito_tip_lamports if self.use_sender else None)
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        return base64.b64encode(bytes(signed)).decode(), report

    def prewarm_exit(self, mint: str) -> None:
        """Pre-build+sign the full-position exit while a runner is armed (climbing
        past trail_activate), so the trailing sell fires INSTANTLY — capturing near
        the peak instead of 1% + build latency below it. Throttled (rebuild ≤ every
        3s) and best-effort. Call UNLOCKED (it hits the network)."""
        try:
            w = self._warm.get(mint)
            if w and (time.monotonic() - w["ts"]) < 3.0:
                return   # still fresh — don't re-quote every cycle
            held = self.token_balance_base(mint)
            if held <= 0:
                return
            raw, report = self._build_signed_sell(mint, held)
            self._warm[mint] = {"raw": raw, "ts": time.monotonic(), "base": held, "report": report}
        except Exception as e:  # noqa: BLE001 - prewarm failure just means no warm tx
            self.log.debug("prewarm_exit(%s) skipped: %s", mint[:6], e)

    def sell(self, token_mint: str, qty: float) -> Fill:
        # Sell the ACTUAL full on-chain balance (a buy lands slightly less than
        # quoted, so selling the recorded qty could exceed the balance and fail).
        dec = self.decimals(token_mint)
        held = self.token_balance_base(token_mint)
        if held <= 0:
            raise ExecutionError("no token balance to sell")

        # 1) Try a PRE-SIGNED warm exit if one is fresh and matches the current
        # balance (armed runner) — skips the ~300-500ms build+sign at trigger time.
        w = self._warm.pop(token_mint, None)
        if w and (time.monotonic() - w["ts"]) < 30.0 and w["base"] == held:
            try:
                return self._finish_sell(token_mint, held, dec, w["raw"], w["report"], qty)
            except Exception as e:  # noqa: BLE001 - pre-signed failed → regular exit
                self.log.warning("pre-signed exit failed (%s), using regular exit: %s",
                                 token_mint[:6], e)
                # Re-read balance: if the warm tx actually landed, we're already out.
                held = self.token_balance_base(token_mint)
                if held <= 0:
                    raise ExecutionError("pre-signed exit already closed the position") from e

        # 2) REGULAR exit — build a fresh sell now (the original path, always the
        # reliable fallback).
        raw_b64, report = self._build_signed_sell(token_mint, held)
        return self._finish_sell(token_mint, held, dec, raw_b64, report, qty)

    def _finish_sell(self, token_mint: str, base_units: int, dec: int,
                     raw_b64: str, report, qty: float) -> Fill:
        """Dual-submit a signed exit tx, confirm on-chain, and record the REAL
        proceeds (what actually landed — no fake wins)."""
        sig = self._send(raw_b64, f"sell {token_mint}", dual=True)
        applied = (report or {}).get("slippageBps")
        self.log.info("live sell sent: %s%s", sig,
                      f" [dyn {applied}bps]" if applied is not None else "")
        out_lamports, fee_lamports = self._confirm_fill(sig, SOL_MINT, f"sell {token_mint}")
        sol_price = self._sol_price()
        sol_out = int(out_lamports) / 1e9
        proceeds = sol_out * sol_price
        sold_qty = base_units / (10 ** dec) if dec else float(base_units)
        price = proceeds / sold_qty if sold_qty > 0 else 0.0
        fee_usd = (fee_lamports / 1e9) * sol_price
        return Fill(side="sell", token_mint=token_mint, qty=qty, price_usd=price,
                    usd_value=proceeds, fees_usd=fee_usd, slippage_bps=self.exit_slippage_bps,
                    executor=self.name, tx_sig=sig)

    def tx_fee_usd(self, sig: str) -> Optional[float]:
        """Real on-chain fee (base + priority lamports) paid for a confirmed swap,
        in USD. Returns None if the tx isn't confirmed/visible yet (the caller
        retries on a later cycle). Used to back-fill trades.fees_usd so the
        Performance tab shows the true gas bleed — decoupled from the trade path,
        so it adds no latency to buying/selling."""
        try:
            res = self.helius._rpc(
                "getTransaction",
                [sig, {"maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(res, dict):
            return None
        fee_lamports = (res.get("meta") or {}).get("fee")
        if fee_lamports is None:
            return None
        price = self._sol_price()
        return (int(fee_lamports) / 1e9) * price if price else None

    def find_recent_buy(self, mint: str, *, lookback: int = 40):
        """The wallet's most recent on-chain BUY of `mint` → (usd_spent, qty_base_units),
        else None. Used to ADOPT an orphaned holding (bought then the engine stopped
        before recording it) at its real entry cost so P&L is accurate."""
        try:
            sigs = self.helius._rpc("getSignaturesForAddress",
                                    [self.pubkey, {"limit": lookback}], retries=1) or []
        except Exception:  # noqa: BLE001
            return None
        sol_price = self._sol_price()
        for s in sigs:
            sig = s.get("signature") if isinstance(s, dict) else None
            if not sig:
                continue
            tx = self.helius._rpc(
                "getTransaction",
                [sig, {"maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
            if not isinstance(tx, dict):
                continue
            meta = tx.get("meta") or {}
            pre = {(b.get("owner"), b.get("mint")): int(b["uiTokenAmount"]["amount"])
                   for b in meta.get("preTokenBalances", [])}
            post = {(b.get("owner"), b.get("mint")): int(b["uiTokenAmount"]["amount"])
                    for b in meta.get("postTokenBalances", [])}
            recv = post.get((self.pubkey, mint), 0) - pre.get((self.pubkey, mint), 0)
            if recv <= 0:
                continue   # not a buy of this token
            keys = [k["pubkey"] if isinstance(k, dict) else k
                    for k in tx["transaction"]["message"]["accountKeys"]]
            try:
                j = keys.index(self.pubkey)
            except ValueError:
                continue
            spent = (meta["preBalances"][j] - meta["postBalances"][j]) / 1e9   # SOL out (incl fee+rent)
            if spent <= 0:
                continue
            return spent * sol_price, recv
        return None

    def find_recent_sell(self, mint: str, *, lookback: int = 60):
        """The wallet's most recent on-chain SELL of `mint` →
        (usd_proceeds, qty_ui_sold, fee_usd, sig), else None. Used to RECONCILE a
        position that was sold on-chain but not recorded (owner sold it, then the
        app closed before the DB was written) at its REAL proceeds so the stuck
        position can be closed accurately. Mirror of find_recent_buy."""
        try:
            sigs = self.helius._rpc("getSignaturesForAddress",
                                    [self.pubkey, {"limit": lookback}], retries=1) or []
        except Exception:  # noqa: BLE001
            return None
        sol_price = self._sol_price()
        for s in sigs:
            sig = s.get("signature") if isinstance(s, dict) else None
            if not sig:
                continue
            tx = self.helius._rpc(
                "getTransaction",
                [sig, {"maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
            if not isinstance(tx, dict):
                continue
            meta = tx.get("meta") or {}
            if meta.get("err"):
                continue
            pre = {(b.get("owner"), b.get("mint")): int(b["uiTokenAmount"]["amount"])
                   for b in meta.get("preTokenBalances", [])}
            post = {(b.get("owner"), b.get("mint")): int(b["uiTokenAmount"]["amount"])
                    for b in meta.get("postTokenBalances", [])}
            decs = {b.get("mint"): int(b["uiTokenAmount"]["decimals"])
                    for b in meta.get("preTokenBalances", []) + meta.get("postTokenBalances", [])}
            sold_base = pre.get((self.pubkey, mint), 0) - post.get((self.pubkey, mint), 0)
            if sold_base <= 0:
                continue   # not a sell of this token in this tx
            dec = decs.get(mint, 0)
            qty_ui = sold_base / (10 ** dec) if dec else float(sold_base)
            keys = [k["pubkey"] if isinstance(k, dict) else k
                    for k in tx["transaction"]["message"]["accountKeys"]]
            try:
                j = keys.index(self.pubkey)
            except ValueError:
                continue
            gained = (meta["postBalances"][j] - meta["preBalances"][j]) / 1e9   # net SOL (incl -fee)
            fee = meta.get("fee", 0) / 1e9
            proceeds_sol = gained + fee   # gross of the network fee (fee tracked separately)
            if proceeds_sol <= 0:
                continue   # not SOL-positive → a transfer/burn, not a sell
            return proceeds_sol * sol_price, qty_ui, fee * sol_price, sig
        return None

    def token_balance_base(self, mint: str) -> int:
        """Actual base-unit balance of `mint` in the trading wallet (0 if none)."""
        try:
            res = self.helius._rpc("getTokenAccountsByOwner",
                                   [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed"}])
            total = 0
            for v in (res or {}).get("value", []):
                total += int(v["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            return total
        except Exception:  # noqa: BLE001
            return 0

    # --- holdings / sweep-sell (recover any tokens the wallet holds) ---------

    def token_holdings(self) -> list[tuple[str, int, int]]:
        """Every non-empty SPL token the trading wallet holds → [(mint, base_units, decimals)]."""
        res = self.helius._rpc("getTokenAccountsByOwner",
                               [self.pubkey, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}])
        out = []
        for v in (res or {}).get("value", []):
            info = v["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"]
            base = int(amt["amount"])
            if base > 0:
                out.append((info["mint"], base, int(amt["decimals"])))
        return out

    def sell_base(self, mint: str, base_units: int, decimals: int) -> Fill:
        """Sell an exact base-unit amount of a token back to SOL (used to recover
        holdings — no float round-trip through a human qty)."""
        if base_units <= 0:
            raise ExecutionError("nothing to sell")
        quote = self.jupiter.quote(mint, SOL_MINT, base_units, slippage_bps=self.exit_slippage_bps)
        if quote is None:
            raise ExecutionError("no swap route token→SOL")
        sig, out_lamports, fee_lamports = self._execute(quote, f"sell {mint}", SOL_MINT)
        sol_price = self._sol_price()
        sol_out = int(out_lamports) / 1e9
        proceeds = sol_out * sol_price
        qty = base_units / (10 ** decimals) if decimals else float(base_units)
        price = proceeds / qty if qty > 0 else 0.0
        fee_usd = (fee_lamports / 1e9) * sol_price
        return Fill(side="sell", token_mint=mint, qty=qty, price_usd=price, usd_value=proceeds,
                    fees_usd=fee_usd, slippage_bps=self.exit_slippage_bps, executor=self.name, tx_sig=sig)

    # --- build → sign → send → CONFIRM → read real fill ----------------------

    def _execute(self, quote: dict, label: str, out_mint: str) -> tuple[str, int, int]:
        """Build (dynamic slippage + optional Jito tip), sign, send (Sender→RPC
        fallback), CONFIRM on-chain, and return (sig, actual_out_base_units,
        fee_lamports) read from the confirmed transaction. Raising here means the
        swap did NOT land — so a buy opens no position and a sell leaves the
        position open to retry, instead of recording a phantom/quoted fill."""
        is_sell = out_mint == SOL_MINT
        slip_cap = self.exit_slippage_bps if is_sell else self.slippage_bps
        swap_b64, report = self.jupiter.build_swap(
            quote, self.pubkey,
            max_priority_lamports=self.max_priority_lamports,
            dynamic_slippage=self.dynamic_slippage,
            dynamic_slippage_max_bps=slip_cap if self.dynamic_slippage else None,
            jito_tip_lamports=self.jito_tip_lamports if self.use_sender else None)
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        raw_b64 = base64.b64encode(bytes(signed)).decode()

        sig = self._send(raw_b64, label)
        applied = (report or {}).get("slippageBps")
        self.log.info("live swap sent (%s): %s%s", label, sig,
                      f" [dyn slippage {applied}bps]" if applied is not None else "")
        out_amount, fee_lamports = self._confirm_fill(sig, out_mint, label)
        return sig, out_amount, fee_lamports

    def _sender_submit(self, raw_b64: str):
        """POST the tx to Helius Sender (skipPreflight + maxRetries=0 required).
        Returns a signature string or None."""
        try:
            resp = self.helius.http.post(
                self.sender_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                      "params": [raw_b64, {"encoding": "base64",
                                           "skipPreflight": True, "maxRetries": 0}]})
            sig = (resp.json() or {}).get("result")
            return sig if isinstance(sig, str) else None
        except Exception:  # noqa: BLE001
            return None

    def _rpc_submit(self, raw_b64: str, *, preflight: bool):
        """Submit via normal Helius RPC. preflight=True runs a simulate-before-send
        (safety); False is faster (for the parallel exit leg). Returns sig or None."""
        try:
            sig = self.helius._rpc(
                "sendTransaction",
                [raw_b64, {"encoding": "base64", "skipPreflight": not preflight,
                           "maxRetries": 3, "preflightCommitment": "confirmed"}])
            return sig if isinstance(sig, str) else None
        except Exception:  # noqa: BLE001
            return None

    def _send(self, raw_b64: str, label: str, *, dual: bool = False) -> str:
        """Submit the signed tx and return its signature.

        dual=True (EXITS): fire Sender AND direct RPC *concurrently* — first to
        respond wins. Both submit the identical signed tx, so Solana dedupes by
        signature and only one instance actually lands; this just minimizes the
        chance of a slow single path. dual=False (BUYS): Sender→RPC fallback with a
        preflight simulation (§6.5 simulate-before-send)."""
        if dual and self.use_sender and self.sender_url:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [pool.submit(self._sender_submit, raw_b64),
                        pool.submit(self._rpc_submit, raw_b64, preflight=False)]
                for f in as_completed(futs):
                    sig = f.result()
                    if isinstance(sig, str):
                        return sig
            # both legs failed → one more safe RPC attempt with preflight
            sig = self._rpc_submit(raw_b64, preflight=True)
            if isinstance(sig, str):
                return sig
            raise ExecutionError(f"dual send failed ({label})")

        # Non-dual (buys): Sender first, then RPC-with-preflight fallback.
        if self.use_sender and self.sender_url:
            sig = self._sender_submit(raw_b64)
            if isinstance(sig, str):
                return sig
            self.log.warning("sender failed (%s), falling back to RPC", label)
        sig = self._rpc_submit(raw_b64, preflight=True)
        if not isinstance(sig, str):
            raise ExecutionError(f"send failed ({label})")
        return sig

    def _confirm_fill(self, sig: str, out_mint: str, label: str,
                      timeout: float = 30.0) -> tuple[int, int]:
        """Poll until the tx is confirmed, then read the ACTUAL output amount and
        fee from its on-chain meta. Returns (out_base_units, fee_lamports).
        out is lamports for a SOL-out (sell) or token base units for a token-out
        (buy). Raises if the tx failed or never confirmed within `timeout`."""
        deadline = time.monotonic() + timeout
        confirmed = False
        while time.monotonic() < deadline:
            res = self.helius._rpc("getSignatureStatuses",
                                   [[sig], {"searchTransactionHistory": True}])
            v = ((res or {}).get("value") or [None])[0] if isinstance(res, dict) else None
            if v:
                if v.get("err"):
                    raise ExecutionError(f"tx failed on-chain ({label}): {v['err']}")
                if v.get("confirmationStatus") in ("confirmed", "finalized"):
                    confirmed = True
                    break
            time.sleep(0.6)
        if not confirmed:
            raise ExecutionError(f"tx not confirmed within {timeout:.0f}s ({label}): {sig}")
        tx = self.helius._rpc(
            "getTransaction",
            [sig, {"maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
        if not isinstance(tx, dict):
            raise ExecutionError(f"could not read confirmed tx ({label}): {sig}")
        meta = tx.get("meta") or {}
        fee = int(meta.get("fee") or 0)
        if out_mint == SOL_MINT:
            keys = [k["pubkey"] if isinstance(k, dict) else k
                    for k in tx["transaction"]["message"]["accountKeys"]]
            i = keys.index(self.pubkey)
            # native SOL received = balance delta + fee (fee also debits native)
            return (meta["postBalances"][i] - meta["preBalances"][i]) + fee, fee
        pre = {(b.get("owner"), b.get("mint")): int(b["uiTokenAmount"]["amount"])
               for b in meta.get("preTokenBalances", [])}
        post = {(b.get("owner"), b.get("mint")): int(b["uiTokenAmount"]["amount"])
                for b in meta.get("postTokenBalances", [])}
        k = (self.pubkey, out_mint)
        return post.get(k, 0) - pre.get(k, 0), fee
