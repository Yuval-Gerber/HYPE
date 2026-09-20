"""Phase 4 on-demand demo — push ONE real buy through the whole live pipeline.

Instead of waiting for a followed wallet to buy a filter-passing token, this
manufactures a buy signal for a real token and runs it through the EXACT same
path the live engine uses: safety filters (live) → sizing → simulated fill at
the live price → open position → TP/SL management against the live price → close.

It uses tight TP/SL so a small real price move closes the trade quickly, and an
isolated scratch DB so your real paper session stays clean. Still 100% paper.

  python tools/p4_demo_buy.py                       # BONK, TP +1% / SL -1%, watch 90s
  python tools/p4_demo_buy.py --mint <MINT> --tp 2 --sl -2 --seconds 120
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolated scratch DB so the demo never touches the real paper session.
_scratch = tempfile.mkdtemp(prefix="hype_p4demo_")
os.environ["HYPE_HOME"] = _scratch
os.environ["HYPE_DB"] = os.path.join(_scratch, "hype.db")
os.environ["HYPE_CONFIG"] = os.path.join(_scratch, "config.toml")

from hype import db, secrets  # noqa: E402
from hype.config import load_config  # noqa: E402
from hype.data.dexscreener import DexScreenerClient  # noqa: E402
from hype.data.helius import HeliusClient  # noqa: E402
from hype.data.jupiter import JupiterClient  # noqa: E402
from hype.data.rugcheck import RugCheckClient  # noqa: E402
from hype.engine.engine import PaperEngine  # noqa: E402
from hype.engine.executor import PaperExecutor  # noqa: E402
from hype.engine.pricing import DexScreenerPriceFeed  # noqa: E402
from hype.logging_setup import utcnow_iso  # noqa: E402
from hype.models import BuySignal  # noqa: E402
from hype.safety import SafetyFilters  # noqa: E402

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint", default=BONK)
    ap.add_argument("--tp", type=float, default=1.0, help="take-profit %% for the demo")
    ap.add_argument("--sl", type=float, default=-1.0, help="stop-loss %% for the demo")
    ap.add_argument("--seconds", type=int, default=90, help="how long to watch before force-closing")
    args = ap.parse_args()

    cfg = load_config()
    cfg.trading.take_profit_pct = args.tp      # tight, so a small real move closes it
    cfg.trading.stop_loss_pct = args.sl
    conn = db.init_db()

    feed = DexScreenerPriceFeed(DexScreenerClient(), ttl_seconds=2.0)
    executor = PaperExecutor(feed, slippage_bps=cfg.trading.paper_slippage_bps,
                             fee_bps=cfg.trading.paper_fee_bps)
    filters = SafetyFilters(HeliusClient(secrets.helius_api_key()),
                            JupiterClient(secrets.jupiter_api_key()),
                            DexScreenerClient(), RugCheckClient())
    engine = PaperEngine(conn, cfg, executor, feed, filters=filters)

    print(f"\n=== P4 live demo: pushing a BUY for {args.mint} through the real pipeline ===")
    print(f"Demo TP +{args.tp}% / SL {args.sl}% | start ${cfg.trading.paper_starting_balance_usd:,.0f}\n")

    print("1) Running REAL safety filters on the token…")
    report = filters.check(args.mint, cfg)
    print(report.summary())
    if not report.passed:
        print("\nToken failed filters → engine would skip it (this is correct). "
              "Try a liquid token like BONK.")
        return 0

    print("\n2) Injecting the buy signal → sizing → simulated fill at live price…")
    signal = BuySignal("DEMO_TRADER_0000000000000000000000000000000", args.mint,
                       "demo_sig", utcnow_iso())
    out = engine.handle_signal(signal)
    if not out.accepted:
        print(f"   not opened: {out.reason}")
        return 1
    pos = conn.execute("SELECT * FROM positions WHERE id=?", (out.position_id,)).fetchone()
    print(f"   ✅ position #{pos['id']} opened: {pos['entry_qty']:,.2f} tokens "
          f"@ ${pos['entry_price']:.8g}  (cost ${pos['entry_amount_usd']:,.2f})")

    print(f"\n3) Managing against the LIVE price for up to {args.seconds}s "
          f"(re-pricing every {cfg.engine.position_poll_seconds:.0f}s)…\n")
    deadline = time.monotonic() + args.seconds
    closed = None
    while time.monotonic() < deadline:
        closes = engine.manage_once()
        if closes:
            closed = closes[0]
            break
        p = conn.execute("SELECT * FROM positions WHERE id=?", (out.position_id,)).fetchone()
        price = feed.get_price_usd(args.mint)
        if price:
            pct = engine.positions.pnl_pct(p, price)
            print(f"   live price ${price:.8g}  →  P&L {pct:+.2f}%")
        time.sleep(cfg.engine.position_poll_seconds)

    if closed is None:
        print("\n4) No TP/SL hit in the window → force-closing to show the close path…")
        closed = engine.positions.close_by_id(out.position_id, reason="manual")

    print(f"\n   ✅ position closed [{closed.reason.upper()}]  "
          f"P&L {closed.realized_pnl_pct:+.2f}%  (${closed.realized_pnl_usd:+,.2f})  "
          f"{'WIN' if closed.win else 'LOSS'}")
    eq = engine.account.equity(engine.session_id, feed)
    print(f"   final paper equity ${eq.equity_usd:,.2f} (realized ${eq.realized_pnl_usd:+,.2f})")
    print("\nThat exercised filters → sizing → fill → position → TP/SL → close on LIVE data.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
