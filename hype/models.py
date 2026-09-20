"""Runtime data models shared across the engine.

Lightweight dataclasses (not pydantic) for hot-path objects like trader stats
and buy signals. Config uses pydantic (config.py); these are plain values that
flow between the ranker, monitor, and (later) the strategy/executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class TraderStat:
    """A ranked wallet from Birdeye's trader leaderboard (§5.1)."""

    address: str
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    volume: float
    trade_count: int
    window: str            # e.g. "1W"
    network: str = "solana"
    last_active_unix: Optional[float] = None   # real last on-chain tx time (from the activity prune)

    @classmethod
    def from_birdeye(cls, d: dict, window: str) -> "TraderStat":
        realized = float(d.get("realized_pnl") or 0.0)
        unreal = float(d.get("unrealized_pnl") or 0.0)
        total = d.get("pnl")
        total = float(total) if total is not None else realized + unreal
        return cls(
            address=d["address"],
            realized_pnl=realized,
            unrealized_pnl=unreal,
            total_pnl=total,
            volume=float(d.get("volume") or 0.0),
            trade_count=int(d.get("trade_count") or 0),
            window=window,
            network=d.get("network", "solana"),
        )

    @property
    def short(self) -> str:
        return f"{self.address[:4]}…{self.address[-4:]}"


@dataclass(slots=True)
class SafetyCheck:
    """Result of one safety filter (§5.3)."""

    name: str
    passed: bool
    required: bool          # required checks gate the buy; optional are informational
    detail: str = ""
    skipped: bool = False   # e.g. RugCheck unreachable, or optional check disabled

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        return "PASS" if self.passed else "FAIL"


@dataclass(slots=True)
class SafetyReport:
    """Aggregate verdict for a token across all filters (§5.3)."""

    token_mint: str
    checks: list = field(default_factory=list)
    concentration: Optional[float] = None   # top-10 holder % (for risk-scaled take-profit)
    liquidity_usd: Optional[float] = None    # pool liquidity (for the pool-depth size cap)

    def add(self, check: "SafetyCheck") -> None:
        self.checks.append(check)

    @property
    def passed(self) -> bool:
        """Buy is allowed only if every REQUIRED, non-skipped check passed."""
        return all(c.passed for c in self.checks if c.required and not c.skipped)

    @property
    def failures(self) -> list:
        return [c for c in self.checks if c.required and not c.skipped and not c.passed]

    @property
    def mint_short(self) -> str:
        return f"{self.token_mint[:4]}…{self.token_mint[-4:]}"

    def summary(self) -> str:
        verdict = "✅ PASS" if self.passed else "❌ FAIL"
        lines = [f"{verdict}  token {self.token_mint}"]
        for c in self.checks:
            tag = "req" if c.required else "opt"
            lines.append(f"   [{c.status}] ({tag}) {c.name}: {c.detail}")
        if not self.passed:
            lines.append(f"   → rejected by: {', '.join(c.name for c in self.failures)}")
        return "\n".join(lines)


@dataclass(slots=True)
class BuySignal:
    """A detected buy by a followed trader (§5.2).

    Emitted by the monitor; consumed by the strategy/executor in later phases.
    In P2 we only print these (read-only, no trading).
    """

    trader_wallet: str
    token_mint: str
    signature: str
    timestamp: str                       # UTC ISO-8601
    amount: float = 0.0                  # token units received
    token_symbol: Optional[str] = None
    spent_sol: float = 0.0               # SOL paid out, if detected
    source: str = "helius"               # which feed surfaced it
    raw_type: Optional[str] = None       # parsed tx type (e.g. "SWAP")

    @property
    def trader_short(self) -> str:
        return f"{self.trader_wallet[:4]}…{self.trader_wallet[-4:]}"

    @property
    def mint_short(self) -> str:
        return f"{self.token_mint[:4]}…{self.token_mint[-4:]}"

    @property
    def solscan_tx(self) -> str:
        return f"https://solscan.io/tx/{self.signature}"
