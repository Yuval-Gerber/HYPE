"""Phase 3 checker — run the safety filters against token mints (§5.3).

Usage:
  python tools/p3_check.py                 # run the built-in good/bad test set
  python tools/p3_check.py <MINT> [<MINT>] # check specific token mints

Read-only: queries Helius / DexScreener / Jupiter / RugCheck and prints a
pass/fail report per token. No trading.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hype import secrets  # noqa: E402
from hype.config import load_config  # noqa: E402
from hype.data.dexscreener import DexScreenerClient  # noqa: E402
from hype.data.helius import HeliusClient  # noqa: E402
from hype.data.jupiter import JupiterClient  # noqa: E402
from hype.data.rugcheck import RugCheckClient  # noqa: E402
from hype.safety import SafetyFilters  # noqa: E402

# Built-in test set with the verdict we EXPECT, to sanity-check the filters.
TEST_TOKENS = [
    ("BONK  (established, authorities revoked)", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "PASS"),
    ("WIF   (established, authorities revoked)", "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "PASS"),
    ("USDC  (freeze authority ACTIVE)",          "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "FAIL"),
    ("garbage mint (no token/liquidity)",        "1nc1nerator11111111111111111111111111111111", "FAIL"),
]


def main() -> int:
    cfg = load_config()
    hkey, jkey = secrets.helius_api_key(), secrets.jupiter_api_key()
    if not hkey:
        print("ERROR: Helius key not configured.", file=sys.stderr)
        return 2

    filters = SafetyFilters(
        helius=HeliusClient(hkey),
        jupiter=JupiterClient(jkey),
        dexscreener=DexScreenerClient(),
        rugcheck=RugCheckClient(),
    )

    args = sys.argv[1:]
    if args:
        items = [(m, m, "?") for m in args]
    else:
        items = TEST_TOKENS

    print(f"\nRunning safety filters (liquidity>=${cfg.filters.min_liquidity_usd:,.0f}, "
          f"top10<{cfg.filters.max_top10_holders_pct:.0f}%, sellability={cfg.filters.sellability_sim})\n")

    ok_count = 0
    for label, mint, expected in items:
        print(f"── {label}")
        report = filters.check(mint, cfg)
        print(report.summary())
        if expected != "?":
            got = "PASS" if report.passed else "FAIL"
            match = "✔ as expected" if got == expected else f"✘ EXPECTED {expected}, GOT {got}"
            print(f"   >>> {match}")
            ok_count += (got == expected)
        print()

    if all(e != "?" for _, _, e in items):
        print(f"Expectation match: {ok_count}/{len(items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
