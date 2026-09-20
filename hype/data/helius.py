"""Helius client — RPC, enhanced-transaction parsing, and buy detection (§5.2).

Two roles:
  - `parse_transactions()` calls the Enhanced Transactions API to turn signatures
    into human-readable parsed transactions (type, tokenTransfers, ...).
  - `detect_buy()` inspects a parsed tx for a given wallet and returns a
    BuySignal if that wallet BOUGHT a token (received a non-base token while
    paying out SOL or a stablecoin). Sells are ignored (§5.2).

The live WebSocket subscription itself lives in `hype.monitor` (async); this
client provides the URLs and the synchronous parse/detection helpers it uses.
"""

from __future__ import annotations

from typing import List, Optional

from ..logging_setup import utcnow_iso
from ..models import BuySignal
from .http import HttpClient

# Base/quote tokens — receiving these is NOT a memecoin buy.
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
BASE_TOKENS = frozenset({WSOL, USDC, USDT})


class HeliusError(RuntimeError):
    pass


class HeliusClient:
    def __init__(self, api_key: str, http: HttpClient | None = None, *, min_interval: float = 0.2) -> None:
        if not api_key:
            raise HeliusError("Helius API key is not configured (store it in the Keychain).")
        self.api_key = api_key
        self.http = http or HttpClient(min_interval=min_interval)

    # --- URLs (also used by the async monitor) -------------------------------

    @property
    def rpc_url(self) -> str:
        return f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"

    @property
    def parse_url(self) -> str:
        return f"https://api.helius.xyz/v0/transactions/?api-key={self.api_key}"

    @property
    def ws_url(self) -> str:
        return f"wss://mainnet.helius-rpc.com/?api-key={self.api_key}"

    # --- RPC (token on-chain reads for safety filters, §5.3) -----------------

    # JSON-RPC errors that are transient (server load) vs. permanent (bad input).
    _TRANSIENT_RPC_CODES = {-32603, -32005, -32000}
    _TRANSIENT_MARKERS = ("overloaded", "try again", "rate limit", "timeout", "busy")

    def _rpc(self, method: str, params: list, *, retries: int = 4) -> dict:
        """JSON-RPC call with retry on TRANSIENT errors (HTTP 200 + error body).

        The shared HttpClient already retries HTTP 429/5xx, but Helius returns
        load errors (e.g. -32603 "account index service overloaded") as a 200
        with an error body, so we retry those here. Permanent errors (bad mint,
        wrong size) raise immediately — no point retrying.
        """
        import time

        last_err = None
        for attempt in range(retries + 1):
            resp = self.http.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
            })
            data = resp.json()
            err = data.get("error")
            if not err:
                return data["result"]
            last_err = err
            msg = str(err.get("message", "")).lower()
            transient = err.get("code") in self._TRANSIENT_RPC_CODES or any(
                m in msg for m in self._TRANSIENT_MARKERS)
            if transient and attempt < retries:
                time.sleep(min(0.5 * (2 ** attempt), 6))
                continue
            raise HeliusError(f"{method}: {err}")
        raise HeliusError(f"{method}: {last_err}")

    def get_mint_info(self, mint: str) -> dict:
        """Return the parsed SPL mint info: mintAuthority, freezeAuthority,
        decimals, supply. Authorities are None when revoked."""
        res = self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        value = (res or {}).get("value")
        if not value:
            raise HeliusError(f"mint {mint} not found")
        return value["data"]["parsed"]["info"]

    def get_token_supply_ui(self, mint: str) -> float:
        res = self._rpc("getTokenSupply", [mint])
        return float(res["value"]["uiAmount"] or 0.0)

    def get_largest_accounts_ui(self, mint: str, *, retries: int = 1) -> list[float]:
        """uiAmount of the largest token accounts (up to ~20), descending.

        Defaults to few retries: this call gets overloaded on very-high-holder
        tokens, and the safety layer has a fast RugCheck fallback — so we fail
        fast instead of burning backoff time (latency matters for copy-trading).
        """
        res = self._rpc("getTokenLargestAccounts", [mint], retries=retries)
        return [float(a.get("uiAmount") or 0.0) for a in res.get("value", [])]

    def top_holder_concentration_pct(self, mint: str, *, top_n: int = 10, retries: int = 1) -> Optional[float]:
        """Sum of the top-N token accounts as a % of total supply.

        Standard quick heuristic (token accounts, not deduped owners). Returns
        None if supply is unknown. Note: includes pool/CEX accounts.
        """
        supply = self.get_token_supply_ui(mint)
        if supply <= 0:
            return None
        largest = self.get_largest_accounts_ui(mint, retries=retries)[:top_n]
        return 100.0 * sum(largest) / supply

    def last_activity_ts(self, address: str, *, retries: int = 1) -> Optional[float]:
        """Unix timestamp of a wallet's most recent on-chain transaction, or None
        if it has never transacted / is unknown. Used by the ranker to prune
        wallets that ranked well on 7-day PnL but have since gone dormant (a
        dormant wallet emits no live buys, so following it is dead weight)."""
        res = self._rpc("getSignaturesForAddress", [address, {"limit": 1}], retries=retries)
        if not res:
            return None
        bt = res[0].get("blockTime")
        return float(bt) if bt else None

    # --- parsing -------------------------------------------------------------

    def parse_transactions(self, signatures: List[str]) -> List[dict]:
        """Parse up to 100 signatures via the Enhanced Transactions API."""
        if not signatures:
            return []
        resp = self.http.post(self.parse_url, json={"transactions": signatures[:100]})
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise HeliusError(str(data["error"]))
        return data

    # --- buy detection -------------------------------------------------------

    @staticmethod
    def detect_buy(tx: dict, wallet: str) -> Optional[BuySignal]:
        """Return a BuySignal if `wallet` bought a token in this tx, else None.

        Heuristic (§5.2): the wallet RECEIVES a non-base token and PAYS OUT SOL
        or a stablecoin (or the tx is a SWAP). Picks the largest received
        non-base token as the bought mint.
        """
        token_transfers = tx.get("tokenTransfers") or []
        native_transfers = tx.get("nativeTransfers") or []

        received = [
            t for t in token_transfers
            if t.get("toUserAccount") == wallet
            and t.get("mint") not in BASE_TOKENS
            and float(t.get("tokenAmount") or 0) > 0
        ]
        if not received:
            return None

        paid_sol = any(n.get("fromUserAccount") == wallet for n in native_transfers)
        paid_base = any(
            t.get("fromUserAccount") == wallet and t.get("mint") in BASE_TOKENS
            for t in token_transfers
        )
        is_swap = tx.get("type") == "SWAP"
        if not (paid_sol or paid_base or is_swap):
            return None  # token came in but nothing went out → airdrop/transfer, not a buy

        bought = max(received, key=lambda t: float(t.get("tokenAmount") or 0))

        spent_sol = sum(
            float(n.get("amount") or 0) for n in native_transfers
            if n.get("fromUserAccount") == wallet
        ) / 1e9  # lamports → SOL

        ts = tx.get("timestamp")
        ts_iso = utcnow_iso() if not ts else _epoch_to_iso(ts)

        return BuySignal(
            trader_wallet=wallet,
            token_mint=bought["mint"],
            signature=tx.get("signature", ""),
            timestamp=ts_iso,
            amount=float(bought.get("tokenAmount") or 0),
            spent_sol=spent_sol,
            raw_type=tx.get("type"),
        )


def _epoch_to_iso(epoch: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat(timespec="seconds")
