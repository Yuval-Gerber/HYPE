"""Phase 2 runner — read-only data layer demo.

Ranks the top wallets via Birdeye, prints them with stats, then watches them
live via Helius and prints each buy in real time. No trading happens here.

Usage:
  python tools/p2_run.py                 # rank, then watch live buys (Ctrl-C to stop)
  python tools/p2_run.py --rank-only     # just print the ranking and exit
  python tools/p2_run.py --verbose       # also print every parsed tx (liveness proof)
  python tools/p2_run.py --watch ADDR..  # watch specific wallets instead of ranked set
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
from hype.data.helius import HeliusClient  # noqa: E402
from hype.logging_setup import log_activity, utcnow_iso  # noqa: E402
from hype.models import BuySignal, TraderStat  # noqa: E402
from hype.monitor import LiveMonitor  # noqa: E402
from hype.ranker import rank_and_store  # noqa: E402


def print_ranking(ranked: list[TraderStat]) -> None:
    print("\n================== HYPE — Top wallets (by realized PnL) ==================")
    print(f"{'#':>2}  {'wallet':<46}  {'realized PnL':>16}  {'unreal PnL':>14}  {'trades':>6}")
    print("-" * 92)
    for i, t in enumerate(ranked, 1):
        print(f"{i:>2}  {t.address:<46}  {t.realized_pnl:>16,.0f}  {t.unrealized_pnl:>14,.0f}  {t.trade_count:>6}")
    if not ranked:
        print("  (no eligible wallets — try widening rank_pool_size or lowering filters)")
    print("=" * 92)
    print(f"Following {len(ranked)} wallets.\n")


async def watch(helius: HeliusClient, wallets: list[str], conn, verbose: bool) -> None:
    def on_buy(sig: BuySignal) -> None:
        line = (f"🟢 BUY  {sig.timestamp}  {sig.trader_short}  "
                f"bought {sig.amount:,.2f} of {sig.mint_short}  "
                f"(spent {sig.spent_sol:.3f} SOL)  {sig.solscan_tx}")
        print(line)
        log_activity(conn, "scan", "buy detected (paper read-only)",
                     trader=sig.trader_wallet, mint=sig.token_mint,
                     amount=sig.amount, sig=sig.signature)

    def on_event(wallet: str, tx: dict) -> None:
        if verbose:
            short = f"{wallet[:4]}…{wallet[-4:]}"
            print(f"   · {utcnow_iso()}  {short}  {tx.get('type','?'):<14} {tx.get('signature','')[:18]}…")

    monitor = LiveMonitor(helius, wallets, on_buy=on_buy,
                          on_event=on_event if verbose else None)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(30)
            print(f"   …watching {len(wallets)} wallets — "
                  f"{monitor.events_seen} events seen, {monitor.buys_emitted} buys so far")

    hb = asyncio.create_task(heartbeat())
    try:
        await monitor.run()
    finally:
        hb.cancel()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank-only", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="print every parsed tx, not just buys")
    ap.add_argument("--watch", nargs="*", help="watch these wallet addresses instead of the ranked set")
    args = ap.parse_args()

    cfg = load_config()
    conn = db.init_db()

    bkey = secrets.birdeye_api_key()
    hkey = secrets.helius_api_key()
    if not bkey or not hkey:
        print("ERROR: Birdeye/Helius keys not in Keychain. Store them first.", file=sys.stderr)
        return 2

    if args.watch:
        wallets = args.watch
        print(f"Watching {len(wallets)} explicitly-provided wallet(s).")
    else:
        birdeye = BirdeyeClient(bkey)
        print("Ranking wallets via Birdeye (rate-limited; this can take a moment)…")
        ranked = rank_and_store(birdeye, cfg, conn)
        print_ranking(ranked)
        log_activity(conn, "system", f"ranked top {len(ranked)} wallets")
        wallets = [t.address for t in ranked]

    if args.rank_only or not wallets:
        conn.close()
        return 0

    helius = HeliusClient(hkey)
    print("Starting live monitor (Ctrl-C to stop)…\n")
    try:
        asyncio.run(watch(helius, wallets, conn, args.verbose))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
