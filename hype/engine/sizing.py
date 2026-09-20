"""Position sizing (§5.5).

Per trade = per_trade_pct of total balance (equity), capped at per_trade_cap_usd,
never spending the gas reserve, and only while under max_open_positions. All
values come from config — no hardcoded numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import HypeConfig


@dataclass(slots=True)
class SizingDecision:
    approved: bool
    usd_amount: float
    reason: str


def gas_reserve_usd(cfg: HypeConfig, sol_price_usd: Optional[float] = None) -> float:
    """USD value of the untouchable gas reserve. Auto-tracks the live SOL price
    when one is available (so it's never over/under-reserved); falls back to the
    configured price only when the feed is unavailable."""
    price = sol_price_usd if sol_price_usd and sol_price_usd > 0 else cfg.engine.gas_reserve_sol_price_usd
    return cfg.trading.gas_reserve_sol * price


def decide_size(
    *,
    equity_usd: float,
    available_cash_usd: float,
    open_positions: int,
    cfg: HypeConfig,
    sol_price_usd: Optional[float] = None,
    size_multiplier: float = 1.0,
) -> SizingDecision:
    t = cfg.trading
    if open_positions >= t.max_open_positions:
        return SizingDecision(False, 0.0, f"max open positions reached ({t.max_open_positions})")

    # Trading allowance (§5.5): a hard ceiling on the capital Hype may trade with.
    # It caps BOTH the base that per_trade_pct sizes off AND total deployed capital,
    # so the "$1000 = 10×$100" ceiling holds even if max_open_positions/per_trade_pct
    # change. Funds above it sit untouched. 0 = unlimited. While the balance is under
    # the allowance it has no effect — Hype just trades with what it has.
    allowance = t.trading_allowance_usd
    base_equity = equity_usd
    if allowance and allowance > 0:
        base_equity = min(base_equity, allowance)

    # size_multiplier < 1 shrinks the trade for unproven traders (probation).
    target = min(base_equity * (t.per_trade_pct / 100.0) * size_multiplier, t.per_trade_cap_usd)
    spendable = available_cash_usd - gas_reserve_usd(cfg, sol_price_usd)
    if spendable <= 0:
        return SizingDecision(False, 0.0, "insufficient cash above gas reserve")

    if allowance and allowance > 0:
        # Already-deployed capital = equity minus free cash. Cap the buy to what's
        # left of the allowance so total exposure never exceeds it.
        deployed = max(0.0, equity_usd - available_cash_usd)
        remaining = allowance - deployed
        if remaining <= 0:
            return SizingDecision(False, 0.0, f"trading allowance ${allowance:,.0f} fully deployed")
        spendable = min(spendable, remaining)

    amount = min(target, spendable)
    if amount <= 0:
        return SizingDecision(False, 0.0, "computed size is zero")
    if amount < t.min_trade_usd:
        return SizingDecision(
            False, 0.0,
            f"below min trade ${t.min_trade_usd:,.2f} (tiny trades bleed gas) — fund more")
    note = "" if amount >= target else " (sized down to available cash)"
    capped = " (capped by allowance)" if (allowance and allowance > 0 and base_equity < equity_usd) else ""
    return SizingDecision(True, amount, f"{t.per_trade_pct:.0f}% of ${base_equity:,.0f}{capped}{note}")
