"""Wallet ranker (§5.1).

Pulls the Birdeye trader leaderboard, applies eligibility filters, ranks by
realized PnL, takes the top-N, and follows them DIRECTLY — every top-N wallet is
active and copyable the moment it's ranked. No vetting/recruiting/trust tiers:
the only thing that keeps a wallet out of the roster is the owner blacklisting it.

Eligibility (§5.1):
  - trade_count >= wallet_min_trades  (avoid one-hit wallets)
  - realized_pnl > 0                  (exclude illiquid-bag / unrealized-only)
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Optional

from .config import HypeConfig
from .data.birdeye import BirdeyeClient
from .data.helius import HeliusClient
from .logging_setup import get_logger, utcnow_iso
from .models import TraderStat


def filter_and_rank(pool: List[TraderStat], cfg: HypeConfig) -> List[TraderStat]:
    """Apply §5.1 eligibility and return candidates by realized PnL (descending).

    Returns the whole eligible list sorted (not truncated) so a downstream
    recency prune can walk it in PnL order; callers that don't prune take the
    top-N themselves."""
    min_trades = cfg.ranker.wallet_min_trades
    eligible = [t for t in pool if t.trade_count >= min_trades and t.realized_pnl > 0]
    eligible.sort(key=lambda t: t.realized_pnl, reverse=True)
    return eligible


def prune_by_activity(
    candidates: List[TraderStat], cfg: HypeConfig, helius: Optional[HeliusClient]
) -> List[TraderStat]:
    """Walk candidates in PnL order, keep only those with a recent on-chain tx,
    stop once we have follow_top_n. A wallet that ranked well on 7-day PnL but
    has gone silent emits no live buys, so following it just wastes a slot and
    makes the feed look dead. If recency filtering is off or Helius is missing,
    fall back to the plain top-N by PnL."""
    top_n = cfg.ranker.follow_top_n
    max_idle_h = cfg.ranker.wallet_max_idle_hours
    if helius is None or max_idle_h <= 0:
        return candidates[:top_n]

    log = get_logger()
    now = time.time()
    cutoff = max_idle_h * 3600.0
    kept: List[TraderStat] = []
    checked = dropped = 0
    for t in candidates:
        if len(kept) >= top_n:
            break
        checked += 1
        try:
            ts = helius.last_activity_ts(t.address)
        except Exception:  # noqa: BLE001 - never let a recency check kill ranking
            ts = None
        t.last_active_unix = ts   # remember the real on-chain time (for the Traders tab)
        # Unknown activity → keep (don't over-prune on a flaky RPC call).
        if ts is None or (now - ts) <= cutoff:
            kept.append(t)
        else:
            dropped += 1
    log.info("Ranker: activity prune — checked %d, dropped %d dormant (>%.0fh), kept %d.",
             checked, dropped, max_idle_h, len(kept))
    return kept


def store_ranking(conn: sqlite3.Connection, ranked: List[TraderStat],
                  cfg: Optional[HypeConfig] = None) -> None:
    """Persist the followed set: every top-N wallet becomes active + copyable
    immediately, and wallets that dropped out of the top-N are deactivated. The
    ONLY exception is a blacklisted wallet — the owner benched it by hand, so it
    is never auto-followed or auto-reactivated. Open positions live in a separate
    table and are unaffected.

    Only touches wallets whose source is 'ranked' — manually added wallets
    (source='manual') are never auto-deactivated here."""
    now = utcnow_iso()
    ranked_addrs = {t.address for t in ranked}

    for t in ranked:
        # Real last on-chain activity time (from the prune) → the Traders tab's
        # "Last active" column. Fall back to now only if unknown.
        last_active = (
            datetime.fromtimestamp(t.last_active_unix, tz=timezone.utc).isoformat()
            if t.last_active_unix else None
        )
        conn.execute(
            """
            INSERT INTO traders (
                wallet_address, source, active, investigation_state, realized_pnl_usd,
                win_rate, trade_count_external, last_active_ts, created_ts, updated_ts
            ) VALUES (?, 'ranked', 1, 'active', ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                -- Follow the raw top-N directly: a ranked wallet is active+copyable
                -- unless the owner BLACKLISTED it. No vetting/recruiting gate.
                active = CASE WHEN traders.investigation_state='blacklisted' THEN 0 ELSE 1 END,
                investigation_state = CASE WHEN traders.investigation_state='blacklisted'
                                           THEN 'blacklisted' ELSE 'active' END,
                realized_pnl_usd = excluded.realized_pnl_usd,
                trade_count_external = excluded.trade_count_external,
                last_active_ts = COALESCE(excluded.last_active_ts, traders.last_active_ts),
                updated_ts = excluded.updated_ts,
                source = CASE WHEN traders.source='manual' THEN 'manual' ELSE 'ranked' END
            """,
            (t.address, t.realized_pnl, t.trade_count, last_active or now, now, now),
        )

    # Deactivate ranked wallets that fell out of the current top-N (blacklisted
    # ones are left exactly as they are).
    rows = conn.execute(
        "SELECT wallet_address FROM traders WHERE source='ranked' AND active=1"
    ).fetchall()
    for r in rows:
        if r["wallet_address"] not in ranked_addrs:
            conn.execute(
                "UPDATE traders SET active=0, updated_ts=? "
                "WHERE wallet_address=? AND investigation_state != 'blacklisted'",
                (now, r["wallet_address"]),
            )
    conn.commit()


def fetch_candidates(client: BirdeyeClient, cfg: HypeConfig) -> List[TraderStat]:
    """The EXPENSIVE half: page the Birdeye leaderboard and apply §5.1 eligibility.
    Returns the eligible pool sorted by PnL (untruncated). Cache this and re-run
    only the cheap `select_and_store` between Birdeye refreshes."""
    log = get_logger()
    pool = client.fetch_leaderboard(
        window_days=cfg.ranker.birdeye_rank_window_days,
        pool_size=cfg.ranker.rank_pool_size,
    )
    candidates = filter_and_rank(pool, cfg)
    log.info("Ranker: fetched %d leaderboard entries, %d eligible after filters.",
             len(pool), len(candidates))
    return candidates


def select_and_store(
    candidates: List[TraderStat], cfg: HypeConfig, conn: sqlite3.Connection,
    helius: Optional[HeliusClient] = None,
) -> List[TraderStat]:
    """The CHEAP half: from a cached candidate pool, keep the top-N currently
    ACTIVE-on-chain wallets (by rank), and persist the roster — every one active
    and copyable. Uses only Helius recency checks (no Birdeye). Run this often to
    hold N followed at all times; run `fetch_candidates` hourly to refresh who's
    eligible."""
    # Never re-consider blacklisted wallets — they must never be auto-followed.
    blacklisted = {r["wallet_address"] for r in conn.execute(
        "SELECT wallet_address FROM traders WHERE investigation_state='blacklisted'").fetchall()}
    if blacklisted:
        candidates = [c for c in candidates if c.address not in blacklisted]
    ranked = prune_by_activity(candidates, cfg, helius)
    store_ranking(conn, ranked, cfg)
    return ranked


def rank_and_store(
    client: BirdeyeClient, cfg: HypeConfig, conn: sqlite3.Connection,
    helius: Optional[HeliusClient] = None,
) -> List[TraderStat]:
    """Full cycle: fetch (Birdeye) → select active top-N → persist. Convenience
    for the initial rank / one-off refresh; the running engine caches the pool
    and calls fetch/select separately."""
    return select_and_store(fetch_candidates(client, cfg), cfg, conn, helius)
