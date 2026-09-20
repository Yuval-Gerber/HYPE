"""Executor interface + PaperExecutor (§5.4).

`Executor` is the single seam between strategy and execution. Two implementations:
  - PaperExecutor (this phase): simulates fills at the current mark price with
    modeled slippage + fees. No chain interaction.
  - LiveExecutor (P6): builds/signs/sends a Jupiter swap.

The engine, sizing, and position manager all talk to `Executor` only, so flipping
paper↔live changes nothing else (§11).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from .pricing import PriceFeed


class ExecutionError(RuntimeError):
    pass


@dataclass(slots=True)
class Fill:
    """The result of a simulated/real fill."""

    side: str            # 'buy' | 'sell'
    token_mint: str
    qty: float           # token units transacted
    price_usd: float     # effective fill price per token (incl. slippage)
    usd_value: float     # buy: total USD spent (incl. fee); sell: net USD proceeds
    fees_usd: float
    slippage_bps: int
    executor: str        # 'paper' | 'live'
    tx_sig: Optional[str] = None


class Executor(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    def get_price_usd(self, token_mint: str) -> Optional[float]:
        ...

    @abc.abstractmethod
    def buy(self, token_mint: str, usd_to_spend: float) -> Fill:
        """Spend `usd_to_spend` USD to acquire the token."""

    @abc.abstractmethod
    def sell(self, token_mint: str, qty: float) -> Fill:
        """Sell `qty` token units back to USD/SOL."""


class PaperExecutor(Executor):
    name = "paper"

    def __init__(self, price_feed: PriceFeed, *, slippage_bps: int = 50, fee_bps: int = 25) -> None:
        self.feed = price_feed
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps

    def get_price_usd(self, token_mint: str) -> Optional[float]:
        return self.feed.get_price_usd(token_mint)

    def buy(self, token_mint: str, usd_to_spend: float) -> Fill:
        price = self.feed.get_price_usd(token_mint)
        if not price or price <= 0:
            raise ExecutionError(f"no price for {token_mint}; cannot simulate buy")
        slip = self.slippage_bps / 10_000.0
        fee = self.fee_bps / 10_000.0
        effective_price = price * (1 + slip)        # buys fill slightly worse
        fee_usd = usd_to_spend * fee
        qty = (usd_to_spend - fee_usd) / effective_price
        return Fill(
            side="buy", token_mint=token_mint, qty=qty, price_usd=effective_price,
            usd_value=usd_to_spend, fees_usd=fee_usd, slippage_bps=self.slippage_bps,
            executor=self.name,
        )

    def sell(self, token_mint: str, qty: float) -> Fill:
        price = self.feed.get_price_usd(token_mint)
        if not price or price <= 0:
            raise ExecutionError(f"no price for {token_mint}; cannot simulate sell")
        slip = self.slippage_bps / 10_000.0
        fee = self.fee_bps / 10_000.0
        effective_price = price * (1 - slip)        # sells fill slightly worse
        gross = qty * effective_price
        fee_usd = gross * fee
        proceeds = gross - fee_usd
        return Fill(
            side="sell", token_mint=token_mint, qty=qty, price_usd=effective_price,
            usd_value=proceeds, fees_usd=fee_usd, slippage_bps=self.slippage_bps,
            executor=self.name,
        )
