"""RugCheck client — token risk verdict (free public API, no key).

Optional filter (§5.3): reject tokens whose RugCheck report contains a
'danger'-level risk. Treated as best-effort: if the API is unreachable, the
filter is SKIPPED (not failed), so RugCheck downtime never blocks trading on its
own. The hard on-chain checks (authorities, liquidity, sellability) stand alone.
"""

from __future__ import annotations

from typing import Optional

from .http import HttpClient

SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"


class RugCheckClient:
    def __init__(self, http: HttpClient | None = None, *, min_interval: float = 0.3) -> None:
        self.http = http or HttpClient(min_interval=min_interval)

    def report_summary(self, mint: str) -> Optional[dict]:
        """Return the summary dict, or None if unavailable."""
        try:
            resp = self.http.get(SUMMARY_URL.format(mint=mint))
            return resp.json()
        except Exception:
            return None

    def report_full(self, mint: str) -> Optional[dict]:
        """Return the full report (includes topHolders), or None if unavailable."""
        try:
            resp = self.http.get(REPORT_URL.format(mint=mint))
            return resp.json()
        except Exception:
            return None

    def top_holder_concentration_pct(self, mint: str, *, top_n: int = 10) -> Optional[float]:
        """Top-N holder concentration % from RugCheck's topHolders.

        Used as a fallback when Helius getTokenLargestAccounts is overloaded
        (common on very-high-holder tokens). Returns None if unavailable.
        """
        rep = self.report_full(mint)
        if not rep:
            return None
        holders = rep.get("topHolders") or []
        if not holders:
            return None
        return float(sum((h.get("pct") or 0) for h in holders[:top_n]))

    def has_danger(self, mint: str) -> tuple[Optional[bool], str]:
        """Return (is_danger, detail).

        is_danger is None when the verdict couldn't be fetched (→ skip the
        filter). True/False otherwise, with the worst risk level in detail.
        """
        summary = self.report_summary(mint)
        if summary is None:
            return None, "rugcheck unavailable"
        risks = summary.get("risks") or []
        levels = [str(r.get("level", "")).lower() for r in risks]
        danger = any(lvl == "danger" for lvl in levels)
        worst = "danger" if danger else ("warn" if "warn" in levels else "none")
        names = ", ".join(r.get("name", "?") for r in risks) or "no flagged risks"
        return danger, f"worst={worst}; score={summary.get('score')}; {names}"
