"""Phase 4 live paper runner — drive the paper engine off REAL live signals.

Pipeline: rank wallets (or reuse the DB set) → watch them live (Helius) →
safety-filter each buy (P3) → size + simulate the fill (PaperExecutor) → manage
TP/SL in the background → investigation engine on each close. No real money.

Usage:
  python tools/p4_run.py            # reuse ranked wallets from the DB (ranks once if empty)
  python tools/p4_run.py --rerank   # force a fresh Birdeye ranking first
  python tools/p4_run.py --watch ADDR..  # watch specific wallets

Ctrl-C to stop. Prints a status line (equity + open positions) periodically.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hype import db, secrets  # noqa: E402
from hype.config import load_config  # noqa: E402
from hype.data.birdeye import BirdeyeClient  # noqa: E402
from hype.data.dexscreener import DexScreenerClient  # noqa: E402
from hype.data.helius import HeliusClient  # noqa: E402
from hype.data.jupiter import JupiterClient  # noqa: E402
from hype.data.rugcheck import RugCheckClient  # noqa: E402
from hype.engine.engine import PaperEngine  # noqa: E402
from hype.engine.executor import PaperExecutor  # noqa: E402
from hype.engine.pricing import DexScreenerPriceFeed  # noqa: E402
from hype.ranker import rank_and_store  # noqa: E402
from hype.safety import SafetyFilters  # noqa: E402


def get_wallets(cfg, conn, args) -> list[str]:
    if args.watch:
        return args.watch
    rows = conn.execute(
        "SELECT wallet_address FROM traders WHERE active=1 AND source='ranked' "
        "ORDER BY realized_pnl_usd DESC").fetchall()
    if rows and not args.rerank:
        return [r["wallet_address"] for r in rows]
    print("Ranking wallets via Birdeye…")
    ranked = rank_and_store(BirdeyeClient(secrets.birdeye_api_key()), cfg, conn)
    return [t.address for t in ranked]


async def run(engine: PaperEngine, helius, wallets, poll_seconds: float):
    from hype.monitor import LiveMonitor
    monitor = LiveMonitor(helius, wallets, on_buy=engine.handle_signal)

    async def manage_loop():
        while True:
            await asyncio.sleep(poll_seconds)
            await asyncio.to_thread(engine.manage_once)

    async def status_loop():
        while True:
            await asyncio.sleep(20)
            eq = await asyncio.to_thread(engine.account.equity, engine.session_id, engine.feed)
            print(f"   📊 equity ${eq.equity_usd:,.2f} | cash ${eq.cash_usd:,.2f} | "
                  f"open {eq.open_positions} | realized ${eq.realized_pnl_usd:+,.2f} | "
                  f"events {monitor.events_seen} buys {monitor.buys_emitted}")

    tasks = [asyncio.create_task(manage_loop()), asyncio.create_task(status_loop())]
    try:
        await monitor.run()
    finally:
        for t in tasks:
            t.cancel()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--watch", nargs="*")
    args = ap.parse_args()

    cfg = load_config()
    conn = db.init_db()
    for k in ("birdeye_api_key", "helius_api_key"):
        if not getattr(secrets, k)():
            print(f"ERROR: {k} not configured.", file=sys.stderr)
            return 2

    wallets = get_wallets(cfg, conn, args)
    print(f"\nPaper engine starting (mode={cfg.mode}). Watching {len(wallets)} wallets.")
    print(f"Start balance ${cfg.trading.paper_starting_balance_usd:,.0f} | "
          f"TP +{cfg.trading.take_profit_pct}% / SL {cfg.trading.stop_loss_pct}% | "
          f"size {cfg.trading.per_trade_pct}% (cap ${cfg.trading.per_trade_cap_usd:,.0f})\n")

    feed = DexScreenerPriceFeed(DexScreenerClient(), ttl_seconds=cfg.engine.price_cache_ttl_seconds)
    executor = PaperExecutor(feed, slippage_bps=cfg.trading.paper_slippage_bps,
                             fee_bps=cfg.trading.paper_fee_bps)
    filters = SafetyFilters(
        helius=HeliusClient(secrets.helius_api_key()),
        jupiter=JupiterClient(secrets.jupiter_api_key()),
        dexscreener=DexScreenerClient(), rugcheck=RugCheckClient())
    engine = PaperEngine(conn, cfg, executor, feed, filters=filters)

    helius = HeliusClient(secrets.helius_api_key())
    try:
        asyncio.run(run(engine, helius, wallets, cfg.engine.position_poll_seconds))
    except KeyboardInterrupt:
        eq = engine.account.equity(engine.session_id, feed)
        print(f"\nStopped. Final paper equity ${eq.equity_usd:,.2f} "
              f"(realized ${eq.realized_pnl_usd:+,.2f}, {eq.open_positions} open).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
