"""Config system — the live-editable source of truth (§9).

Storage format: TOML (hand-editable) validated by pydantic. The dashboard
(Phase 5) reads and writes this file; the engine reads it. Every threshold in
§9 is represented here as a default and can be changed live.

SECURITY (§6.3): `home_wallet_address` is the single allowlisted payout
destination. Changing it requires three factors (password + Touch ID + the
secret nickname). The generic `save_config()` path REFUSES to change it
silently — it can only be changed via the dedicated, auth-gated
`set_home_wallet_address()` (enforced in P6's security layer). This prevents any
config-injection from redirecting funds.
"""

from __future__ import annotations

import tomllib
from typing import Literal, Optional

import tomli_w
from pydantic import BaseModel, Field

from . import paths


# --- Grouped config sections (each maps to a [section] in config.toml) -------


class TradingConfig(BaseModel):
    """Sizing, exits, and execution params (§5.5, §5.6)."""

    trading_allowance_usd: float = Field(
        1000.0, description="Max capital (USD) Hype is allowed to trade with — a hard ceiling on "
        "total deployed at once AND the base that per_trade_pct sizes off. E.g. 1000 = up to "
        "10 trades of $100. Wallet funds above this sit untouched as reserve (panic drain still "
        "sweeps everything home). 0 = unlimited (trade with the full wallet balance). While the "
        "balance is below the allowance, Hype simply trades with whatever it has.")
    per_trade_pct: float = Field(10.0, description="% of total balance per trade")
    per_trade_cap_usd: float = Field(5000.0, description="Hard cap per trade (USD)")
    min_trade_usd: float = Field(
        3.0, description="Skip a buy if the sized amount is below this — tiny trades "
        "can't overcome the ~$0.16 round-trip gas, so they bleed the wallet.")
    max_open_positions: int = Field(10, description="Max simultaneous open positions")
    # Risk-scaled position SIZE by token concentration (mirrors tp/sl): the MORE
    # concentrated a token's top-10 holders, the smaller the bet — so a risky token
    # gets less exposure. [min_top10_%, size_multiplier], high→low. Loosened now
    # that recruiting screens the traders. Empty / unknown concentration → full size.
    size_by_concentration: list[tuple[float, float]] = Field(
        default_factory=lambda: [(150.0, 0.5), (100.0, 0.7), (70.0, 0.85), (0.0, 1.0)],
        description="[top10% threshold, size multiplier] tiers, high→low (risk-scaled bet size).")
    max_pct_of_liquidity: float = Field(
        2.0, description="Never let a single trade exceed this % of the token's pool liquidity "
        "(0 = off). Keeps price impact tiny so big trades don't move the token / signal others — "
        "the key to scaling capital without your own trades eating the edge.")
    gas_reserve_sol: float = Field(0.05, description="SOL kept untouched for fees")
    take_profit_pct: float = Field(10.0, description="Hard take-profit ceiling (%)")
    # Risk-scaled take-profit: the MORE concentrated a token's top-10 holders, the
    # riskier it is (higher rug odds), so we grab a smaller/faster profit and exit
    # before the ~2-min rug window. List of [min_top10_%, take_profit_%], evaluated
    # high→low; the token's concentration picks the first tier it meets. Empty list
    # → fall back to the flat take_profit_pct. Unknown concentration → treated as risky.
    tp_by_concentration: list[tuple[float, float]] = Field(
        default_factory=lambda: [(150.0, 2.0), (100.0, 3.0), (70.0, 5.0), (0.0, 100.0)],
        description="[top10% threshold, take-profit%] tiers, high→low (risk-scaled TP)")
    trail_activate_pct: float = Field(5.0, description="Arm the trailing stop once profit ≥ this % (0 = off)")
    trail_gap_pct: float = Field(3.0, description="Trailing stop: sell if profit drops this % below its peak")
    stop_loss_pct: float = Field(-15.0, description="Stop-loss trigger (%); fallback when sl_by_concentration empty")
    # Risk-scaled stop-loss — the mirror of tp_by_concentration. The MORE
    # concentrated a token's top-10 holders, the tighter the stop, so a risky
    # ("crazy") trade is cut fast instead of bleeding to the full stop. List of
    # [min_top10_%, stop_loss_%] (stop % negative), evaluated high→low; the
    # token's concentration picks the first tier it meets. Empty → flat stop_loss_pct.
    # Unknown concentration → treated as risky (mid).
    sl_by_concentration: list[tuple[float, float]] = Field(
        default_factory=lambda: [(150.0, -5.0), (100.0, -8.0), (70.0, -11.0), (0.0, -15.0)],
        description="[top10% threshold, stop-loss%] tiers, high→low (risk-scaled SL)")
    slippage_bps: int = Field(200, description="Max slippage in basis points (200 = 2%); live cap")
    exit_slippage_bps: int = Field(
        500, description="Slippage for SELLS/exits (higher than buys — on a fast dump you want "
        "to get OUT, not have the swap fail and drop further)")
    max_priority_lamports: int = Field(
        1_000_000, description="Live: cap on the per-swap priority fee (1e6 = 0.001 SOL ≈ a few ¢). "
        "Lower saves fees but may land slower in congestion.")
    max_buy_impact_pct: float = Field(
        1.5, description="Live: reject a BUY if Jupiter's quoted price impact exceeds this % "
        "(thin pool → the fill would slip badly). 0 = off. Bounds per-trade slippage at the source.")
    dynamic_slippage: bool = Field(
        True, description="Live: let Jupiter's RTSE pick the OPTIMAL slippage per swap (capped at "
        "slippage_bps/exit_slippage_bps) instead of a fixed tolerance — better realized fills on exits.")
    use_sender: bool = Field(
        True, description="Live: submit swaps via Helius Sender (multi-path, faster landing) with an "
        "automatic fallback to normal RPC on any error. Costs a small Jito tip per tx.")
    jito_tip_lamports: int = Field(
        5_000, description="Live: Jito tip for Sender (5000 = SWQOS tier, ~$0.0004). Raise for the "
        "priority 'Max' tier (1e6 = 0.001 SOL) once trades are larger.")
    sender_url: str = Field(
        "https://sender.helius-rpc.com/fast", description="Helius Sender endpoint (regional http://…-sender…/fast is faster).")
    paper_starting_balance_usd: float = Field(1000.0, description="Paper-mode start balance")
    paper_slippage_bps: int = Field(50, description="Modeled slippage per paper fill (bps)")
    paper_fee_bps: int = Field(25, description="Modeled swap+priority fee per paper fill (bps)")


class RankerConfig(BaseModel):
    """Wallet ranking via Birdeye (§5.1)."""

    birdeye_rank_window_days: int = Field(7, description="Realized-PnL window (days)")
    wallet_min_trades: int = Field(10, description="Min trades to be eligible")
    follow_top_n: int = Field(50, description="How many top wallets to follow (auto-maintained active)")
    wallet_max_idle_hours: float = Field(
        12.0, description="Drop wallets with no on-chain tx in this many hours (0 = off). "
        "Keeps the watch-set to traders active NOW, not just high 7d-PnL but dormant.")
    rank_refresh_minutes: int = Field(60, description="Birdeye leaderboard refresh interval (minutes). "
        "The expensive (Birdeye) call — keep it hourly to respect quota; roster maintenance "
        "below keeps the active set fresh cheaply between fetches.")
    roster_refresh_minutes: int = Field(
        5, description="How often to re-select the top-N ACTIVE roster from the cached "
        "pool (cheap Helius recency checks; keeps N active traders online at all times)")
    rank_refresh_hours: int = Field(4, description="(Deprecated) old hours-based interval")
    rank_pool_size: int = Field(200, description="Leaderboard entries to scan (deep enough to find N active)")


class FiltersConfig(BaseModel):
    """Pre-buy safety filters (§5.3). Run before every buy; fail = skip."""

    mint_authority_revoked: bool = Field(True, description="Require mint authority revoked")
    freeze_authority_revoked: bool = Field(True, description="Require freeze authority revoked")
    min_liquidity_usd: float = Field(30000.0, description="Min liquidity (USD)")
    max_top10_holders_pct: float = Field(65.0, description="Max top-10 holder concentration % (0 = filter off)")
    sellability_sim: bool = Field(True, description="Require reverse-quote sellability check")
    sellability_notional_usd: float = Field(100.0, description="USD size used to test sellability")
    sellability_max_price_impact_pct: float = Field(30.0, description="Max sell price impact % (honeypot guard)")
    min_token_age_minutes: Optional[int] = Field(None, description="Optional min token age (min)")
    rugcheck_enabled: bool = Field(True, description="Use RugCheck verdict if API available")
    require_lp_locked: bool = Field(
        True, description="Anti-rug: reject a token whose LP is flagged UNLOCKED (dev can still "
        "pull liquidity) or already rugged, per RugCheck. Best-effort (skips if RugCheck is down).")


class InvestigationConfig(BaseModel):
    """Trader investigation state machine (§5.7)."""

    grace_period_trades: int = Field(5, description="Trades before a trader is judged")
    auto_police: bool = Field(
        False, description="Master switch for AUTOMATIC trader benching/investigation/blacklisting. "
        "OFF: Hype never auto-pauses or blacklists a trader (it still records win/loss stats, and the "
        "owner can pause/drop manually). Chosen so the raw top-N leaderboard — including rug-prone "
        "wallets — stays followed and is used for its early pumps rather than benched after one rug.")
    invest_consecutive_losses: int = Field(3, description="Consecutive losses → investigate")
    invest_losses_gt_wins: bool = Field(True, description="Cumulative losses > wins → investigate")
    rug_loss_pct: float = Field(-50.0, description="A single loss this bad → bench trader NOW (rug)")
    blacklist_on_rug: bool = Field(
        False, description="A rug (loss ≤ rug_loss_pct) PERMANENTLY blacklists the trader — never "
        "copied or re-added by the ranker again — instead of just benching them.")
    invest_net_negative: bool = Field(True, description="Net-negative copied P&L (after grace) → investigate")
    probation_window_trades: int = Field(3, description="Probation watch window (trades)")
    probation_losses_to_trip: int = Field(2, description="Losses in window → re-investigate")
    auto_drop_ceiling: Optional[int] = Field(None, description="Auto-drop at level N (None = off)")


class PayoutConfig(BaseModel):
    """Auto-deposit / payout to home wallet (§5.8). Disabled by default."""

    enabled: bool = Field(False, description="Master switch for auto-deposit")
    interval_hours: int = Field(24, description="Sweep interval (hours)")
    fixed_amount_usd: Optional[float] = Field(None, description="Fixed daily sweep amount (owner-set)")
    asset: str = Field("USDC", description="Payout asset")
    min_working_balance_usd: float = Field(0.0, description="Working capital guard; never drain below")
    # SECURITY: the single allowlisted payout destination. 3-factor to change.
    home_wallet_address: str = Field("", description="Owner MetaMask SOLANA address (Base58)")


class TelegramConfig(BaseModel):
    """Telegram bot (§8). Owner-only control + alerts. The bot TOKEN is NOT here
    — it lives in the Keychain (KeyVault.TELEGRAM_BOT_TOKEN). Only the owner's
    numeric chat id and the alert toggles are config."""

    enabled: bool = Field(False, description="Master switch for the Telegram bot")
    owner_chat_id: Optional[int] = Field(None, description="Owner's Telegram chat id (auto-captured via Link)")
    alert_trades: bool = Field(True, description="Alert on position opened/closed")
    alert_daily: bool = Field(True, description="Send a daily P&L summary")
    alert_investigations: bool = Field(True, description="Alert on trader investigations")
    alert_errors: bool = Field(True, description="Alert on engine errors")
    alert_panic: bool = Field(True, description="Alert on panic-drain / kill events")


class EngineConfig(BaseModel):
    """Engine runtime behavior (not a trading threshold; still editable)."""

    position_poll_seconds: float = Field(5.0, description="How often to re-price open positions for TP/SL")
    price_cache_ttl_seconds: float = Field(3.0, description="Price feed cache TTL to limit API calls")
    dedupe_same_token: bool = Field(True, description="Don't open a 2nd position in a token already held")
    max_buys_per_token: int = Field(
        1, description="Max times to trade the SAME token in a session (0 = unlimited). A trader "
        "repeatedly buying one token is often pumping their own coin before rugging it.")
    gas_reserve_sol_price_usd: float = Field(150.0, description="SOL price used to value the gas reserve in paper mode")
    prevent_sleep: bool = Field(
        True, description="Hold a macOS caffeinate assertion so system sleep can't suspend the "
        "engine. Turn OFF if your Mac never sleeps on its own — avoids any display/Space quirk.")


class HypeConfig(BaseModel):
    """Root config object. Mirrors the §9 table."""

    mode: Literal["paper", "live"] = Field("paper", description="paper | live")
    trading: TradingConfig = Field(default_factory=TradingConfig)
    ranker: RankerConfig = Field(default_factory=RankerConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    investigation: InvestigationConfig = Field(default_factory=InvestigationConfig)
    payout: PayoutConfig = Field(default_factory=PayoutConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)


# --- Load / save -------------------------------------------------------------


class HomeWalletChangeError(PermissionError):
    """Raised when something tries to change the home wallet via the generic
    save path. The home wallet may only be changed via set_home_wallet_address()
    (which is 3-factor gated in the security layer)."""


def load_config() -> HypeConfig:
    """Load and validate config.toml. Creates it with defaults if missing."""
    path = paths.config_path()
    if not path.exists():
        cfg = HypeConfig()
        _write(cfg)
        return cfg
    with path.open("rb") as f:
        data = tomllib.load(f)
    return HypeConfig.model_validate(data)


def save_config(cfg: HypeConfig, *, _allow_home_wallet_change: bool = False) -> None:
    """Persist config to TOML.

    Refuses to change `home_wallet_address` unless `_allow_home_wallet_change`
    is set (only the auth-gated set_home_wallet_address() does that). This is a
    structural guard against config-injection redirecting funds (§6.2).
    """
    path = paths.config_path()
    if path.exists() and not _allow_home_wallet_change:
        with path.open("rb") as f:
            current = tomllib.load(f)
        current_addr = current.get("payout", {}).get("home_wallet_address", "")
        if cfg.payout.home_wallet_address != current_addr:
            raise HomeWalletChangeError(
                "home_wallet_address can only be changed via the 3-factor "
                "set_home_wallet_address() path (§6.3)."
            )
    _write(cfg)


def set_home_wallet_address(cfg: HypeConfig, new_address: str, *, authorized: bool) -> HypeConfig:
    """Change the home wallet — ONLY callable with `authorized=True`.

    In P1 this enforces the structural gate. The actual 3-factor check
    (password + Touch ID + secret nickname) is wired in P6's security layer,
    which is the only caller permitted to pass authorized=True.
    """
    if not authorized:
        raise HomeWalletChangeError("Changing the home wallet requires 3-factor authorization (§6.3).")
    cfg.payout.home_wallet_address = new_address
    save_config(cfg, _allow_home_wallet_change=True)
    return cfg


def _write(cfg: HypeConfig) -> None:
    """Serialize config to TOML on disk."""
    path = paths.config_path()
    data = cfg.model_dump()
    # tomli_w cannot serialize None; omit Optional keys that are unset so the
    # file stays clean and re-loads to the same defaults.
    data = _drop_none(data)
    header = (
        "# Hype configuration (§9). Live-editable.\n"
        "# Every value here is a default you can change. The dashboard reads\n"
        "# and writes this file. SECURITY: do NOT put API keys or private keys\n"
        "# here — those live only in the macOS Keychain. The home wallet address\n"
        "# below is 3-factor gated to change (§6.3).\n\n"
    )
    with path.open("wb") as f:
        f.write(header.encode("utf-8"))
        tomli_w.dump(data, f)


def _drop_none(obj):
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    return obj
