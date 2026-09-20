"""Birdeye client — trader leaderboard for wallet ranking (§5.1).

Uses the `trader/gainers-losers` endpoint (verified live 2026-06-28), which
returns realized_pnl, unrealized_pnl, volume, and trade_count per wallet. The
ranker re-sorts by realized PnL and applies eligibility filters.

The endpoint's `type` (window) accepts a small set of values; we map the
configured day-window to the closest supported one. If Birdeye exposes a wider
set later, extend WINDOW_MAP.
"""

from __future__ import annotations

from typing import List

from ..models import TraderStat
from .http import HttpClient

BASE_URL = "https://public-api.birdeye.so"

# Map configured rank window (days) -> Birdeye gainers-losers `type` value.
# Verified supported: "today", "yesterday", "1W". Anything >1 day -> 1W.
WINDOW_MAP = {0: "today", 1: "today", 2: "1W", 7: "1W"}
PAGE_SIZE = 10  # gainers-losers returns up to ~10 per page


class BirdeyeError(RuntimeError):
    pass


class BirdeyeClient:
    def __init__(self, api_key: str, http: HttpClient | None = None, *, min_interval: float = 1.5) -> None:
        if not api_key:
            raise BirdeyeError("Birdeye API key is not configured (store it in the Keychain).")
        self.api_key = api_key
        # Birdeye free tier is strict; default to a conservative interval.
        self.http = http or HttpClient(min_interval=min_interval)

    def _headers(self) -> dict:
        return {
            "X-API-KEY": self.api_key,
            "accept": "application/json",
            "x-chain": "solana",
        }

    @staticmethod
    def window_param(days: int) -> str:
        return WINDOW_MAP.get(days, "1W")

    def fetch_leaderboard(self, *, window_days: int = 7, pool_size: int = 100) -> List[TraderStat]:
        """Page the trader leaderboard, sorted by PnL desc, up to `pool_size`.

        Returns raw TraderStat objects (unfiltered). The ranker applies §5.1
        eligibility and re-sorts by realized PnL.
        """
        window = self.window_param(window_days)
        collected: list[dict] = []
        offset = 0
        while len(collected) < pool_size:
            params = {
                "type": window,
                "sort_by": "PnL",
                "sort_type": "desc",
                "offset": offset,
                "limit": PAGE_SIZE,
            }
            resp = self.http.get(BASE_URL + "/trader/gainers-losers", headers=self._headers(), params=params)
            payload = resp.json()
            if not payload.get("success", False):
                raise BirdeyeError(f"Birdeye error: {payload.get('message')}")
            items = payload.get("data", {}).get("items", [])
            if not items:
                break
            collected.extend(items)
            offset += PAGE_SIZE
            if len(items) < PAGE_SIZE:
                break
        return [TraderStat.from_birdeye(d, window) for d in collected[:pool_size]]
