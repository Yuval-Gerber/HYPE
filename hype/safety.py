"""Safety filters — run BEFORE every buy; any required failure = skip (§5.3).

Checks (all config-driven, §9):
  1. Mint authority revoked        (required when configured) — Helius
  2. Freeze authority revoked      (required when configured) — Helius
  3. Min liquidity >= threshold    (required)                 — DexScreener
  4. Top-10 holder concentration   (required)                 — Helius
  5. Sellability simulation        (required when configured) — Jupiter reverse quote
  6. Min token age                 (optional, if configured)  — DexScreener pair age
  7. RugCheck verdict not 'danger' (best-effort, skip if down)— RugCheck

The orchestrator returns a SafetyReport; `report.passed` is True only if every
required, non-skipped check passed. This module performs READ-ONLY checks — it
never sends a transaction.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .config import HypeConfig
from .data.dexscreener import DexScreenerClient
from .data.helius import HeliusClient
from .data.jupiter import JupiterClient
from .data.rugcheck import RugCheckClient
from .logging_setup import get_logger
from .models import SafetyCheck, SafetyReport


class SafetyFilters:
    def __init__(
        self,
        helius: HeliusClient,
        jupiter: JupiterClient,
        dexscreener: DexScreenerClient | None = None,
        rugcheck: RugCheckClient | None = None,
    ) -> None:
        self.helius = helius
        self.jupiter = jupiter
        self.dex = dexscreener or DexScreenerClient()
        self.rugcheck = rugcheck or RugCheckClient()
        self.log = get_logger()

    def check(self, token_mint: str, cfg: HypeConfig) -> SafetyReport:
        """Run all filters CONCURRENTLY and assemble the report in order.

        The checks are independent network calls, so latency = the slowest
        single call, not the sum. This matters: a sequential run took ~20–30s,
        far too slow to copy a memecoin buy before it moves.
        """
        f = cfg.filters
        report = SafetyReport(token_mint=token_mint)

        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="safety") as pool:
            fut_mint = pool.submit(self._mint_info, token_mint)
            fut_market = pool.submit(self._market, token_mint)
            # One RugCheck full-report fetch serves BOTH concentration (primary)
            # and the danger verdict — and avoids Helius's slow overloaded
            # getTokenLargestAccounts on established tokens.
            fut_report = pool.submit(self.rugcheck.report_full, token_mint)
            fut_conc = pool.submit(self._concentration, token_mint, fut_report)
            fut_sell = pool.submit(self._sellability_task, token_mint, fut_mint, fut_market, cfg) \
                if f.sellability_sim else None

            info, mint_err = fut_mint.result()
            market = fut_market.result()

            # 1–2. Authorities
            if mint_err is None:
                mint_auth = info.get("mintAuthority")
                freeze_auth = info.get("freezeAuthority")
                report.add(SafetyCheck("mint_authority_revoked", mint_auth is None,
                                       f.mint_authority_revoked,
                                       "revoked" if mint_auth is None else f"ACTIVE: {mint_auth}"))
                report.add(SafetyCheck("freeze_authority_revoked", freeze_auth is None,
                                       f.freeze_authority_revoked,
                                       "revoked" if freeze_auth is None else f"ACTIVE: {freeze_auth}"))
            else:
                report.add(SafetyCheck("mint_authority_revoked", False,
                                       f.mint_authority_revoked, f"read error: {mint_err}"))
                report.add(SafetyCheck("freeze_authority_revoked", False,
                                       f.freeze_authority_revoked, f"read error: {mint_err}"))

            # 3. Liquidity
            liq = market.get("liquidity_usd") or 0.0
            report.liquidity_usd = liq   # exposed for the pool-depth size cap
            report.add(SafetyCheck("min_liquidity", liq >= f.min_liquidity_usd, True,
                                   f"${liq:,.0f} (min ${f.min_liquidity_usd:,.0f})"))

            # 4. Concentration ( max_top10_holders_pct <= 0  →  gate disabled, but we
            #    still compute + expose the value so the engine can risk-scale the TP )
            conc, source = fut_conc.result()
            report.concentration = conc
            if f.max_top10_holders_pct <= 0:
                report.add(SafetyCheck("top10_concentration", True, True,
                                       "disabled (max=0)", skipped=True))
            elif conc is None:
                report.add(SafetyCheck("top10_concentration", False, True,
                                       "unavailable (Helius + RugCheck both failed)"))
            else:
                report.add(SafetyCheck("top10_concentration", conc < f.max_top10_holders_pct, True,
                                       f"{conc:.1f}% (max {f.max_top10_holders_pct:.0f}%) via {source}"))

            # 5. Sellability
            if fut_sell is not None:
                report.add(fut_sell.result())
            else:
                report.add(SafetyCheck("sellability", True, False, "disabled", skipped=True))

            # 6. Optional token age
            if f.min_token_age_minutes is not None:
                report.add(self._age(market, f.min_token_age_minutes))

            # 7. RugCheck danger verdict (best-effort; from the same report)
            if f.rugcheck_enabled:
                is_danger, detail = self._danger(fut_report.result())
                if is_danger is None:
                    report.add(SafetyCheck("rugcheck", True, False, detail, skipped=True))
                else:
                    report.add(SafetyCheck("rugcheck", not is_danger, True, detail))

            # 8. LP locked/burned (anti-rug): reject if LP is flagged UNLOCKED (dev
            # can still pull it) or the token is already rugged. Best-effort.
            if f.require_lp_locked:
                ok, detail = self._lp_locked(fut_report.result())
                if ok is None:
                    report.add(SafetyCheck("lp_locked", True, False, detail, skipped=True))
                else:
                    report.add(SafetyCheck("lp_locked", ok, True, detail))

        return report

    @staticmethod
    def _lp_locked(report: Optional[dict]) -> tuple[Optional[bool], str]:
        """(ok, detail). ok None → skip (unavailable). False → LP pullable / already
        rugged (reject). Uses RugCheck risks: an unlocked-LP danger means the dev
        can still drain the pool — the exact rug that hit us."""
        if not report:
            return None, "rugcheck unavailable"
        if report.get("rugged"):
            return False, "already rugged"
        for r in (report.get("risks") or []):
            name = (r.get("name") or "").lower()
            if r.get("level") == "danger" and "lp" in name and "unlock" in name:
                return False, r.get("name") or "LP unlocked"
        return True, "LP not flagged unlocked"

    # --- helpers -------------------------------------------------------------

    def _mint_info(self, token_mint: str) -> tuple[Optional[dict], Optional[str]]:
        try:
            return self.helius.get_mint_info(token_mint), None
        except Exception as e:
            return None, str(e)

    def _market(self, token_mint: str) -> dict:
        try:
            return self.dex.market(token_mint)
        except Exception as e:
            self.log.debug("DexScreener error for %s: %s", token_mint, e)
            return {"liquidity_usd": 0.0, "price_usd": None, "pair_created_ms": None}

    def _sellability_task(self, token_mint, fut_mint, fut_market, cfg) -> SafetyCheck:
        info, _ = fut_mint.result()
        decimals = int(info.get("decimals")) if info else None
        return self._sellability(token_mint, decimals, fut_market.result(), cfg)

    def _concentration(self, token_mint: str, fut_report) -> tuple[Optional[float], str]:
        """Top-10 holder concentration.

        Primary: RugCheck topHolders from the already-fetched report (fast,
        reliable, and avoids Helius's overloaded getTokenLargestAccounts which
        can hang ~10s on very-high-holder tokens). Fallback: Helius with a single
        attempt (fast for brand-new/low-holder tokens RugCheck hasn't indexed).
        Returns (pct, source) or (None, '').
        """
        report = fut_report.result()
        if report:
            holders = report.get("topHolders") or []
            if holders:
                return float(sum((h.get("pct") or 0) for h in holders[:10])), "rugcheck"
        try:
            conc = self.helius.top_holder_concentration_pct(token_mint, top_n=10, retries=0)
            if conc is not None:
                return conc, "helius"
        except Exception as e:
            self.log.debug("concentration via Helius failed for %s: %s", token_mint, e)
        return None, ""

    @staticmethod
    def _danger(report: Optional[dict]) -> tuple[Optional[bool], str]:
        """Danger verdict from a RugCheck full report. (None → unavailable.)"""
        if report is None:
            return None, "rugcheck unavailable"
        risks = report.get("risks") or []
        levels = [str(r.get("level", "")).lower() for r in risks]
        danger = any(lvl == "danger" for lvl in levels)
        worst = "danger" if danger else ("warn" if "warn" in levels else "none")
        names = ", ".join(r.get("name", "?") for r in risks) or "no flagged risks"
        return danger, f"worst={worst}; score={report.get('score')}; {names}"

    def _sellability(self, token_mint, decimals, market, cfg) -> SafetyCheck:
        f = cfg.filters
        price = market.get("price_usd")
        if not decimals or not price or price <= 0:
            # Without price/decimals we can't size a realistic sell; fail safe.
            return SafetyCheck("sellability", False, True,
                               "cannot size sell (missing price/decimals)")
        tokens = f.sellability_notional_usd / price
        base_units = max(int(tokens * (10 ** decimals)), 1)
        try:
            ok, details = self.jupiter.simulate_sell(
                token_mint, base_units, slippage_bps=cfg.trading.slippage_bps)
        except Exception as e:
            return SafetyCheck("sellability", False, True, f"quote error: {e}")
        if not ok:
            return SafetyCheck("sellability", False, True,
                               f"no sell route for ${f.sellability_notional_usd:.0f}")
        impact = details["price_impact_pct"]
        return SafetyCheck(
            "sellability", impact <= f.sellability_max_price_impact_pct, True,
            f"sellable, impact {impact:.2f}% (max {f.sellability_max_price_impact_pct:.0f}%), "
            f"{details['routes']} routes")

    def _age(self, market, min_minutes) -> SafetyCheck:
        created_ms = market.get("pair_created_ms")
        if not created_ms:
            return SafetyCheck("min_token_age", False, True, "pair age unknown")
        import time
        age_min = (time.time() * 1000 - created_ms) / 60000.0
        return SafetyCheck("min_token_age", age_min >= min_minutes, True,
                           f"{age_min:.0f}m old (min {min_minutes}m)")
