"""Trader investigation engine — the §5.7 state machine.

Each followed trader carries a record of Hype's *copied* outcomes. States:

  active              normal; copied + judged after the grace period
  under_investigation paused (no new copies); awaiting owner decision
  probation           re-copying but watched over a short window
  paused              benched by owner ("Keep Paused")
  dropped             archived ("Drop"); owner may re-add as fresh

Copyability is decided by the engine as:  active==1 (in ranked set) AND
investigation_state in {active, probation}.  This engine ONLY mutates the
investigation fields — it never touches the ranking `active` flag, so a ranking
refresh can't accidentally un-pause a trader.

Level / probation interpretation (expands on SPEC §5.7):
  - First investigation (from active) sets level = 1.
  - "Second Chance" increments the level (→ ×2, ×3 …) and enters probation,
    resetting the win/loss + grace counters for a fresh life.
  - During probation we watch the next `probation_window_trades`; if
    `probation_losses_to_trip`+ losses occur, the trader trips back to
    under_investigation (same level; the next Second Chance bumps the ×).
  - Surviving the probation window returns the trader to active.
  - auto_drop_ceiling (default off): if a Second Chance would reach the ceiling
    level, the trader is auto-dropped instead.

Triggers from `active` (only after grace, §5.7):
  - `invest_consecutive_losses` consecutive losses, OR
  - cumulative losses > wins (when `invest_losses_gt_wins`).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional

from ..config import HypeConfig
from ..logging_setup import utcnow_iso

ACTIVE = "active"
UNDER_INVESTIGATION = "under_investigation"
PROBATION = "probation"
PAUSED = "paused"
DROPPED = "dropped"
BLACKLISTED = "blacklisted"   # owner blacklist — NEVER auto-followed again

COPYABLE_STATES = frozenset({ACTIVE, PROBATION})


@dataclass(slots=True)
class InvestigationEvent:
    trader: str
    kind: str        # 'triggered' | 'probation_tripped' | 'probation_passed'
                     # | 'second_chance' | 'dropped' | 'kept_paused' | 'auto_dropped' | 'readded'
    level: int
    detail: str = ""


@dataclass(slots=True)
class TraderState:
    address: str
    copied_wins: int
    copied_losses: int
    consecutive_losses: int
    grace_trades_done: int
    state: str
    level: int
    probation_results: List[str] = field(default_factory=list)
    copied_pnl_usd: float = 0.0


class InvestigationEngine:
    def __init__(self, conn: sqlite3.Connection, cfg: HypeConfig) -> None:
        self.conn = conn
        self.cfg = cfg

    # --- persistence ---------------------------------------------------------

    def load(self, address: str) -> Optional[TraderState]:
        r = self.conn.execute("SELECT * FROM traders WHERE wallet_address=?", (address,)).fetchone()
        if not r:
            return None
        return TraderState(
            address=address,
            copied_wins=r["copied_wins"], copied_losses=r["copied_losses"],
            consecutive_losses=r["consecutive_losses"], grace_trades_done=r["grace_trades_done"],
            state=r["investigation_state"], level=r["investigation_level"],
            probation_results=json.loads(r["probation_results"]) if r["probation_results"] else [],
            copied_pnl_usd=r["copied_pnl_usd"] if "copied_pnl_usd" in r.keys() else 0.0,
        )

    def save(self, s: TraderState) -> None:
        self.conn.execute(
            """UPDATE traders SET copied_wins=?, copied_losses=?, copied_pnl_usd=?,
               consecutive_losses=?, grace_trades_done=?, investigation_state=?,
               investigation_level=?, probation_results=?, updated_ts=? WHERE wallet_address=?""",
            (s.copied_wins, s.copied_losses, s.copied_pnl_usd, s.consecutive_losses,
             s.grace_trades_done, s.state, s.level, json.dumps(s.probation_results),
             utcnow_iso(), s.address),
        )
        self.conn.commit()

    def is_copyable(self, address: str) -> bool:
        r = self.conn.execute(
            "SELECT active, investigation_state FROM traders WHERE wallet_address=?", (address,)
        ).fetchone()
        if not r:
            return False
        return bool(r["active"]) and r["investigation_state"] in COPYABLE_STATES

    # --- core: record a copied-trade outcome --------------------------------

    def record_outcome(self, address: str, win: bool, pnl_pct: Optional[float] = None,
                       pnl_usd: Optional[float] = None) -> List[InvestigationEvent]:
        s = self.load(address)
        if s is None:
            return []
        events: List[InvestigationEvent] = []

        # Update counters for every outcome.
        s.grace_trades_done += 1
        if win:
            s.consecutive_losses = 0
            s.copied_wins += 1
        else:
            s.consecutive_losses += 1
            s.copied_losses += 1
        if pnl_usd is not None:
            s.copied_pnl_usd += pnl_usd

        inv = self.cfg.investigation
        # Owner disabled AUTOMATIC policing → keep the win/loss/PnL record up to date
        # (so the Traders tab and manual review still work) but NEVER auto-pause,
        # investigate, or blacklist. The raw top-N leaderboard stays followed.
        if not getattr(inv, "auto_police", False):
            self.save(s)
            return events

        # A single catastrophic loss = a rug. Bench the trader IMMEDIATELY,
        # even during the grace period — one rug erases ~10 wins, so we don't
        # wait for the win/loss-count triggers (which a high-win-rate rug-buyer
        # like an 82%-win trader never trips).
        is_rug = pnl_pct is not None and pnl_pct <= inv.rug_loss_pct

        # A rug can PERMANENTLY blacklist the trader (owner opt-in) — they led us
        # into a liquidity-pull scam, so never copy or re-rank them again.
        if is_rug and getattr(inv, "blacklist_on_rug", False) and s.state in (ACTIVE, PROBATION):
            s.state = BLACKLISTED
            s.probation_results = []
            events.append(InvestigationEvent(address, "blacklisted", s.level,
                                             f"auto-blacklist: rug ({pnl_pct:.0f}%)"))
            self.save(s)
            self.conn.execute("UPDATE traders SET active=0 WHERE wallet_address=?", (address,))
            self.conn.commit()
            return events

        if s.state == ACTIVE:
            trip = False
            why = ""
            if is_rug:
                trip, why = True, f"rug ({pnl_pct:.0f}%)"
            elif s.grace_trades_done >= inv.grace_period_trades:
                if s.consecutive_losses >= inv.invest_consecutive_losses:
                    trip, why = True, "3+ consecutive losses"
                elif inv.invest_losses_gt_wins and s.copied_losses > s.copied_wins:
                    trip, why = True, "cumulative losses > wins"
                elif inv.invest_net_negative and s.copied_pnl_usd < 0:
                    trip, why = True, f"net-negative copied P&L (${s.copied_pnl_usd:,.0f})"
            if trip:
                s.state = UNDER_INVESTIGATION
                s.level = max(s.level, 1)
                events.append(InvestigationEvent(address, "triggered", s.level, why))

        elif s.state == PROBATION:
            s.probation_results.append("W" if win else "L")
            losses = s.probation_results.count("L")
            if is_rug or losses >= inv.probation_losses_to_trip:
                s.state = UNDER_INVESTIGATION
                s.probation_results = []
                why = f"rug ({pnl_pct:.0f}%)" if is_rug else f"{losses} losses in probation window"
                events.append(InvestigationEvent(address, "probation_tripped", s.level, why))
            elif len(s.probation_results) >= inv.probation_window_trades:
                # survived the window → back to active, fresh counters
                s.state = ACTIVE
                s.probation_results = []
                s.consecutive_losses = 0
                s.copied_wins = 0
                s.copied_losses = 0
                s.copied_pnl_usd = 0.0
                s.grace_trades_done = 0
                events.append(InvestigationEvent(address, "probation_passed", s.level,
                                                 "survived probation window"))
        # under_investigation / paused / dropped: record stats only, no transition.

        self.save(s)
        return events

    # --- owner actions -------------------------------------------------------

    def second_chance(self, address: str) -> InvestigationEvent:
        s = self._require(address)
        new_level = s.level + 1 if s.level >= 1 else 2
        ceiling = self.cfg.investigation.auto_drop_ceiling
        if ceiling is not None and new_level >= ceiling:
            s.state = DROPPED
            s.level = new_level
            self.save(s)
            return InvestigationEvent(address, "auto_dropped", new_level,
                                      f"hit auto-drop ceiling ×{ceiling}")
        s.state = PROBATION
        s.level = new_level
        s.probation_results = []
        s.consecutive_losses = 0
        s.copied_wins = 0
        s.copied_losses = 0
        s.copied_pnl_usd = 0.0
        s.grace_trades_done = 0
        self.save(s)
        return InvestigationEvent(address, "second_chance", new_level, f"probation ×{new_level}")

    def drop(self, address: str) -> InvestigationEvent:
        s = self._require(address)
        s.state = DROPPED
        self.save(s)
        return InvestigationEvent(address, "dropped", s.level, "archived by owner")

    def keep_paused(self, address: str) -> InvestigationEvent:
        s = self._require(address)
        s.state = PAUSED
        self.save(s)
        return InvestigationEvent(address, "kept_paused", s.level, "benched by owner")

    def blacklist(self, address: str) -> InvestigationEvent:
        """Permanently blacklist a trader: never copied AND never auto-added by
        the ranker again (survives re-ranks and live restarts) until the owner
        un-blacklists them."""
        s = self._require(address)
        s.state = BLACKLISTED
        self.save(s)
        self.conn.execute("UPDATE traders SET active=0 WHERE wallet_address=?", (address,))
        self.conn.commit()
        return InvestigationEvent(address, "blacklisted", s.level, "blacklisted — never auto-followed")

    def unblacklist(self, address: str) -> InvestigationEvent:
        """Remove from the blacklist and reset to a fresh active trader."""
        s = self._require(address)
        s.state = ACTIVE
        s.level = 0
        s.probation_results = []
        s.consecutive_losses = 0
        s.copied_wins = 0
        s.copied_losses = 0
        s.copied_pnl_usd = 0.0
        s.grace_trades_done = 0
        self.save(s)
        return InvestigationEvent(address, "unblacklisted", 0, "removed from blacklist")

    def readd(self, address: str) -> InvestigationEvent:
        """Re-add a dropped/paused trader as fresh (owner action)."""
        s = self._require(address)
        s.state = ACTIVE
        s.level = 0
        s.probation_results = []
        s.consecutive_losses = 0
        s.copied_wins = 0
        s.copied_losses = 0
        s.copied_pnl_usd = 0.0
        s.grace_trades_done = 0
        self.save(s)
        return InvestigationEvent(address, "readded", 0, "reset to fresh active")

    def _require(self, address: str) -> TraderState:
        s = self.load(address)
        if s is None:
            raise KeyError(f"unknown trader {address}")
        return s
