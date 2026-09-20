"""Jupiter client — quote API (sellability simulation in P3; swaps in P6).

P3 uses only the read-only Quote API to simulate selling a token back to SOL
(§5.3 sellability check / honeypot guard). The actual swap build+send (Swap API)
is added in P6 with the wallet.

Verified live: GET https://api.jup.ag/swap/v1/quote with header `x-api-key`.
"""

from __future__ import annotations

from typing import Optional

from .http import HttpClient

QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
LITE_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"  # keyless fallback
SWAP_URL = "https://api.jup.ag/swap/v1/swap"
LITE_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterError(RuntimeError):
    pass


class JupiterClient:
    def __init__(self, api_key: Optional[str] = None, http: HttpClient | None = None,
                 *, min_interval: float = 0.3) -> None:
        self.api_key = api_key
        self.http = http or HttpClient(min_interval=min_interval)

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key} if self.api_key else {}

    def _url(self) -> str:
        return QUOTE_URL if self.api_key else LITE_QUOTE_URL

    def quote(self, input_mint: str, output_mint: str, amount: int,
              *, slippage_bps: int = 200) -> Optional[dict]:
        """Return a Jupiter quote dict, or None if no route exists.

        `amount` is in the input token's base units. A None return means the
        router found no path (a strong honeypot / illiquid signal).
        """
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount),
            "slippageBps": slippage_bps,
        }
        resp = self.http.get(self._url(), params=params, headers=self._headers())
        data = resp.json()
        # No-route responses come back as an error object or empty outAmount.
        if not data or data.get("error") or not data.get("outAmount"):
            return None
        return data

    def _swap_url(self) -> str:
        return SWAP_URL if self.api_key else LITE_SWAP_URL

    def build_swap(self, quote: dict, user_public_key: str, *,
                   max_priority_lamports: int = 1_000_000,
                   dynamic_slippage: bool = False,
                   dynamic_slippage_max_bps: Optional[int] = None,
                   jito_tip_lamports: Optional[int] = None) -> tuple[str, Optional[dict]]:
        """POST /swap — returns (base64 VersionedTransaction, dynamicSlippageReport).

        - `dynamic_slippage`: let Jupiter's RTSE estimate the OPTIMAL slippage at
          build time (capped at `dynamic_slippage_max_bps`) and bake it into the tx,
          overriding the quote's fixed slippageBps. This is the fix for realized
          exit slippage — Jupiter picks a tighter/safer bound per token.
        - `jito_tip_lamports`: when set, Jupiter adds a Jito tip transfer so the tx
          can be sent via Helius Sender / Jito for faster landing (else a normal
          dynamic priority fee capped at `max_priority_lamports`).
        The tx has the caller's pubkey as the only signer."""
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
        }
        if jito_tip_lamports:
            body["prioritizationFeeLamports"] = {"jitoTipLamports": int(jito_tip_lamports)}
        else:
            body["prioritizationFeeLamports"] = {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(max_priority_lamports),
                    "priorityLevel": "high",
                }
            }
        if dynamic_slippage:
            body["dynamicSlippage"] = (
                {"maxBps": int(dynamic_slippage_max_bps)} if dynamic_slippage_max_bps else True)
        resp = self.http.post(self._swap_url(), json=body, headers=self._headers())
        data = resp.json()
        tx = data.get("swapTransaction")
        if not tx:
            raise JupiterError(f"no swapTransaction returned: {data.get('error') or data}")
        return tx, data.get("dynamicSlippageReport")

    def simulate_sell(self, token_mint: str, token_amount_base_units: int,
                      *, slippage_bps: int = 200) -> tuple[bool, dict]:
        """Reverse quote token -> SOL (§5.3). Returns (route_exists, details).

        `details` carries out_sol and price_impact_pct when a route exists.
        """
        q = self.quote(token_mint, SOL_MINT, token_amount_base_units, slippage_bps=slippage_bps)
        if q is None:
            return False, {"reason": "no route to SOL"}
        out_lamports = int(q.get("outAmount") or 0)
        impact = float(q.get("priceImpactPct") or 0.0) * 100.0  # Jupiter returns a fraction
        return True, {
            "out_sol": out_lamports / 1e9,
            "price_impact_pct": impact,
            "routes": len(q.get("routePlan", [])),
        }
