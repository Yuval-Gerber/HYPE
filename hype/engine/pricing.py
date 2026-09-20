"""Price feed for marks and TP/SL polling.

Uses DexScreener (free, no key) with a short cache so polling many open
positions doesn't hammer the API. The interface is tiny (`get_price_usd`) so the
paper engine and tests can swap in a scripted feed.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from ..data.dexscreener import DexScreenerClient


class PriceFeed:
    def get_price_usd(self, token_mint: str) -> Optional[float]:  # pragma: no cover - interface
        raise NotImplementedError

    def prime(self, token_mints) -> None:
        """Optionally pre-fetch prices for many mints at once (batch). Default:
        no-op; the DexScreener feed overrides it so TP/SL polling stays to one
        request per cycle no matter how many positions are open."""
        return None


class DexScreenerPriceFeed(PriceFeed):
    def __init__(self, client: DexScreenerClient | None = None, *, ttl_seconds: float = 3.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client = client or DexScreenerClient()
        self.ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, Optional[float]]] = {}
        # Test-only price overrides: force a mint's mark price (used by the
        # dashboard "Test stop-loss" to crash a paper position on demand). An
        # override always wins over cache/API. Paper mode only.
        self._overrides: dict[str, float] = {}

    def set_override(self, token_mint: str, price: float) -> None:
        self._overrides[token_mint] = price

    def clear_override(self, token_mint: str) -> None:
        self._overrides.pop(token_mint, None)

    def get_price_usd(self, token_mint: str) -> Optional[float]:
        if token_mint in self._overrides:
            return self._overrides[token_mint]
        now = self._clock()
        hit = self._cache.get(token_mint)
        if hit and (now - hit[0]) < self.ttl:
            return hit[1]
        price = self.client.market(token_mint).get("price_usd")
        self._cache[token_mint] = (now, price)
        return price

    def prime(self, token_mints) -> None:
        """Batch-refresh the cache for all given mints in one request, so a fast
        (e.g. 0.5s) TP/SL poll costs one API call regardless of position count."""
        mints = [m for m in dict.fromkeys(token_mints) if m]
        if not mints:
            return
        now = self._clock()
        for mint, info in self.client.markets(mints).items():
            self._cache[mint] = (now, info.get("price_usd"))


class ScriptedPriceFeed(PriceFeed):
    """Test/sim feed: returns prices from a dict you mutate over time."""

    def __init__(self, prices: Optional[dict[str, float]] = None) -> None:
        self.prices = prices or {}

    def set(self, token_mint: str, price: float) -> None:
        self.prices[token_mint] = price

    def get_price_usd(self, token_mint: str) -> Optional[float]:
        return self.prices.get(token_mint)


class LivePriceFeed(PriceFeed):
    """LIVE TP/SL marks via real Jupiter quotes — the actual on-chain price.

    DexScreener lags badly and returns nothing for many fresh memecoins, which
    would freeze a live position's mark (so its TP/SL never fires). Here we price
    a token by quoting a small fixed SOL amount into it (SOL→token) and inverting
    — a route exists for anything we bought (it passed the sellability filter).
    SOL itself is priced via DexScreener (reliable). Cached with a short TTL so
    the fast poll doesn't hammer Jupiter."""

    def __init__(self, jupiter, helius, dex_feed: "DexScreenerPriceFeed", *,
                 ref_sol: float = 0.02, ttl_seconds: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        from ..data.jupiter import SOL_MINT
        self._SOL = SOL_MINT
        self.jup = jupiter
        self.helius = helius
        self.dex = dex_feed
        self.ref_sol = ref_sol
        self.ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, Optional[float]]] = {}
        self._decimals: dict[str, int] = {}

    def _decimals_of(self, mint: str) -> int:
        if mint not in self._decimals:
            try:
                self._decimals[mint] = int(self.helius.get_mint_info(mint).get("decimals", 0))
            except Exception:  # noqa: BLE001
                self._decimals[mint] = 0
        return self._decimals[mint]

    def seed(self, marks) -> None:
        """Pre-load last-known marks (e.g. open positions' current_price from the
        DB) so that right after a (re)start — before the first live quote lands —
        every open position already has a price. Prevents a cold cache + a
        transient quote failure from leaving a position UNPRICED (which would
        freeze its P&L and stop its TP/SL from evaluating). A real quote replaces
        the seed on the next poll."""
        now = self._clock()
        for mint, price in dict(marks).items():
            if mint and mint != self._SOL and price:
                self._cache.setdefault(mint, (now, float(price)))

    def get_price_usd(self, token_mint: str) -> Optional[float]:
        if token_mint == self._SOL:
            return self.dex.get_price_usd(self._SOL)   # SOL is reliable on DexScreener
        now = self._clock()
        hit = self._cache.get(token_mint)
        if hit and (now - hit[0]) < self.ttl:
            return hit[1]
        price = self._quote_price(token_mint)
        if price is not None:
            self._cache[token_mint] = (now, price)
            return price
        if hit is not None:
            return hit[1]   # keep last-good price through a transient quote failure
        return None

    def _quote_price(self, mint: str) -> Optional[float]:
        # Primary: real Jupiter price (SOL→token, inverted). Fresh memecoins are
        # routable the moment we buy them, before DexScreener indexes them.
        sol_price = self.dex.get_price_usd(self._SOL)
        if sol_price and sol_price > 0:
            dec = self._decimals_of(mint)
            try:
                q = self.jup.quote(self._SOL, mint, int(self.ref_sol * 1e9), slippage_bps=500)
                if q and q.get("outAmount"):
                    token_out = int(q["outAmount"]) / (10 ** dec) if dec else int(q["outAmount"])
                    if token_out > 0:
                        return (self.ref_sol * sol_price) / token_out
            except Exception:  # noqa: BLE001
                pass
        # Fallback: DexScreener (covers pump.fun/bonding-curve tokens Jupiter
        # won't route a buy for, but which DexScreener tracks).
        try:
            return self.dex.get_price_usd(mint)
        except Exception:  # noqa: BLE001
            return None

    def prime(self, token_mints) -> None:
        for m in dict.fromkeys(token_mints):
            if m and m != self._SOL:
                self.get_price_usd(m)
