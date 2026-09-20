"""DexScreener client — token liquidity + price + age (free, no API key).

Used by the safety filters (§5.3) for the min-liquidity check and as the price
source for sizing/marking positions. Chosen over Birdeye's token endpoints so we
don't spend Birdeye's rate-limited free quota on every candidate token.

Pricing is deliberately defensive: we only price a token from pairs where it is
the BASE token (so priceUsd is actually its price, not the quote token's) AND,
when possible, quoted against SOL/USDC/USDT — reputable references. This avoids a
scam/manipulated pair winning on liquidity and returning a wildly wrong price
(which would corrupt position sizing, P&L, and balance).
"""

from __future__ import annotations

from typing import Optional

from .http import HttpClient

TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE_QUOTES = frozenset({WSOL, USDC, USDT})


def _liq(p: dict) -> float:
    return float((p.get("liquidity") or {}).get("usd") or 0.0)


class DexScreenerClient:
    def __init__(self, http: HttpClient | None = None, *, min_interval: float = 0.3) -> None:
        self.http = http or HttpClient(min_interval=min_interval)

    def token_pairs(self, mint: str) -> list[dict]:
        resp = self.http.get(TOKENS_URL + mint)
        data = resp.json()
        return data.get("pairs") or []

    def _priceable_pairs(self, mint: str, pairs: list[dict]) -> list[dict]:
        """Pairs where `mint` is the base token and has a price. Prefer
        reputable (SOL/USDC/USDT) quotes; fall back to any base pair."""
        base = [p for p in pairs
                if (p.get("baseToken") or {}).get("address") == mint and p.get("priceUsd")]
        stable = [p for p in base if (p.get("quoteToken") or {}).get("address") in STABLE_QUOTES]
        return stable or base

    def best_pair(self, mint: str) -> Optional[dict]:
        """The deepest reputable pair where `mint` is the base token."""
        pairs = self._priceable_pairs(mint, self.token_pairs(mint))
        return max(pairs, key=_liq) if pairs else None

    def markets(self, mints: list[str]) -> dict[str, dict]:
        """Batch price/liquidity for many mints in ONE request (DexScreener
        accepts up to 30 comma-separated addresses). Lets us re-price all open
        positions every 0.5s with a single call instead of one-per-position.
        Returns {mint: {price_usd, liquidity_usd}}."""
        uniq = list(dict.fromkeys(mints))
        out: dict[str, dict] = {}
        for i in range(0, len(uniq), 30):
            chunk = uniq[i:i + 30]
            try:
                resp = self.http.get(TOKENS_URL + ",".join(chunk))
                pairs = resp.json().get("pairs") or []
            except Exception:  # noqa: BLE001 - a bad batch shouldn't kill polling
                pairs = []
            for m in chunk:
                base_pairs = [p for p in pairs
                              if (p.get("baseToken") or {}).get("address") == m]
                if not base_pairs:
                    out[m] = {"price_usd": None, "liquidity_usd": 0.0}
                    continue
                priceable = self._priceable_pairs(m, base_pairs)
                best = max(priceable, key=_liq) if priceable else None
                out[m] = {
                    "price_usd": float(best["priceUsd"]) if best and best.get("priceUsd") else None,
                    "liquidity_usd": float(sum(_liq(p) for p in base_pairs)),
                }
        return out

    def market(self, mint: str) -> dict:
        """Summarized market data.

        - liquidity_usd: total across pairs where `mint` is the base token.
        - price_usd: from the deepest reputable base pair (SOL/USDC/USDT quote).
        - pair_created_ms: earliest base-pair creation time.
        """
        pairs = self.token_pairs(mint)
        base_pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint]
        if not base_pairs:
            return {"liquidity_usd": 0.0, "price_usd": None, "pair_created_ms": None, "pairs": 0}

        total_liq = sum(_liq(p) for p in base_pairs)
        priceable = self._priceable_pairs(mint, pairs)
        best = max(priceable, key=_liq) if priceable else None
        created = [p.get("pairCreatedAt") for p in base_pairs if p.get("pairCreatedAt")]
        return {
            "liquidity_usd": float(total_liq),
            "price_usd": float(best["priceUsd"]) if best and best.get("priceUsd") else None,
            "pair_created_ms": min(created) if created else None,
            "pairs": len(base_pairs),
            "dex": best.get("dexId") if best else None,
        }
