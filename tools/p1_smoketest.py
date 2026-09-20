"""Phase 1 smoke test (non-interactive by default).

Verifies the foundation without requiring a fingerprint:
  - config.toml is created, validates, and round-trips edits
  - the home-wallet guard rejects silent changes (§6.3)
  - all SQLite tables are created
  - logging writes to the activity feed
  - a secret can be stored/read/deleted in the Keychain (round-trip)
  - Touch ID / password availability is detectable (without prompting)

Run an INTERACTIVE Touch ID prompt with:  python tools/p1_smoketest.py --touchid

This uses a throwaway scratch DB/config so it never touches real runtime data,
and a throwaway Keychain entry that it deletes afterward.
"""

from __future__ import annotations

import os
import sys
import tempfile

# Make the project root importable regardless of where this is run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Route all paths to a scratch dir BEFORE importing the package.
_scratch = tempfile.mkdtemp(prefix="hype_p1_")
os.environ["HYPE_HOME"] = _scratch
os.environ["HYPE_CONFIG"] = os.path.join(_scratch, "config.toml")
os.environ["HYPE_DB"] = os.path.join(_scratch, "hype.db")

from hype import config as cfgmod  # noqa: E402
from hype import db, paths, secrets  # noqa: E402
from hype.logging_setup import log_activity, recent_activity  # noqa: E402
from hype.security import touchid  # noqa: E402
from hype.security.keyvault import get_vault  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))


def main() -> int:
    # 1. Config created + defaults correct
    cfg = cfgmod.load_config()
    check("config.toml created", paths.config_path().exists(), str(paths.config_path()))
    check("default mode == paper", cfg.mode == "paper", cfg.mode)
    check("default per_trade_pct == 10", cfg.trading.per_trade_pct == 10.0)
    check("default follow_top_n == 15", cfg.ranker.follow_top_n == 15)
    check("default payout disabled", cfg.payout.enabled is False)

    # 2. Config round-trip (edit -> save -> reload)
    cfg.trading.take_profit_pct = 12.5
    cfgmod.save_config(cfg)
    reloaded = cfgmod.load_config()
    check("config edit persists", reloaded.trading.take_profit_pct == 12.5)

    # 3. Home-wallet guard rejects silent change (§6.3)
    reloaded.payout.home_wallet_address = "SoMeFakeSolanaAddr1111111111111111111111111"
    try:
        cfgmod.save_config(reloaded)
        check("home-wallet guard blocks silent change", False, "save did NOT raise")
    except cfgmod.HomeWalletChangeError:
        check("home-wallet guard blocks silent change", True)

    # 3b. Authorized path works
    cfgmod.set_home_wallet_address(reloaded, "AuthorizedAddr2222222222222222222222222222", authorized=True)
    check("authorized home-wallet change works",
          cfgmod.load_config().payout.home_wallet_address.startswith("Authorized"))
    # 3c. Unauthorized explicit call rejected
    try:
        cfgmod.set_home_wallet_address(reloaded, "X", authorized=False)
        check("unauthorized home-wallet change rejected", False)
    except cfgmod.HomeWalletChangeError:
        check("unauthorized home-wallet change rejected", True)

    # 4. DB tables created
    conn = db.init_db()
    tables = set(db.table_names(conn))
    expected = {"traders", "sessions", "positions", "trades",
                "equity_snapshots", "activity_log", "schema_meta"}
    check("all DB tables present", expected.issubset(tables),
          f"missing: {expected - tables}" if not expected.issubset(tables) else "")

    # 5. Activity log writes + reads
    log_activity(conn, "system", "smoketest entry", level="INFO", foo="bar")
    rows = recent_activity(conn, limit=1)
    check("activity log round-trip", len(rows) == 1 and rows[0]["category"] == "system")

    # 6. Secret store/read/delete round-trip (real Keychain, throwaway entry)
    vault = get_vault()
    test_name = "p1_smoketest_throwaway"
    secret_ok = True
    try:
        vault.set_secret(test_name, "s3cr3t-value")
        got = vault.get_secret(test_name)
        secret_ok = (got == "s3cr3t-value") and vault.has_secret(test_name)
    finally:
        vault.delete_secret(test_name)
    check("Keychain secret round-trip", secret_ok)
    check("secret deleted (cleanup)", not vault.has_secret(test_name))

    # 6b. secrets_status returns booleans only (no values)
    st = secrets.secrets_status()
    check("secrets_status returns booleans", all(isinstance(v, bool) for v in st.values()))

    # 7. Touch ID availability (no prompt)
    avail, reason = touchid.is_available()
    check("Touch ID/password policy detectable", True,
          f"available={avail}" + (f" ({reason})" if reason else ""))

    conn.close()

    # --- report ---
    print()
    width = max(len(n) for _, n, _ in results)
    for status, name, detail in results:
        mark = "\033[92m[ok]\033[0m" if status == PASS else "\033[91m[XX]\033[0m"
        line = f"  {mark} {status}  {name.ljust(width)}"
        if detail:
            line += f"   {detail}"
        print(line)
    failed = [r for r in results if r[0] == FAIL]
    print()
    print(f"  {len(results) - len(failed)}/{len(results)} checks passed.")
    print(f"  scratch dir: {_scratch}")

    # Optional interactive Touch ID prompt
    if "--touchid" in sys.argv:
        print("\n  --touchid: prompting for Touch ID / password now...")
        ok, err = touchid.authenticate("Hype Phase 1 smoke test — confirm Touch ID works")
        print(f"  Touch ID result: {'SUCCESS' if ok else 'FAILED'}" + (f"  ({err})" if err else ""))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
