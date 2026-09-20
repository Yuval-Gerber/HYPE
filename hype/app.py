"""Hype headless bootstrap entrypoint.

Wires together the foundation pieces: config, database, logging, and the
KeyVault. Running `python -m hype` initializes everything and prints a status
report.

The engine, dashboard, live wallet, and Telegram layers build on top of this
bootstrap (the desktop app's entrypoint is `hype.ui.app`).
"""

from __future__ import annotations

import sys

from . import db, paths, secrets
from .config import HypeConfig, load_config
from .logging_setup import get_logger, log_activity
from .security import touchid
from .security.keyvault import get_vault


class App:
    """Holds the initialized foundation services."""

    def __init__(self) -> None:
        self.logger = get_logger()
        self.config: HypeConfig = load_config()
        self.conn = db.init_db()
        log_activity(self.conn, "system", "Hype foundation initialized", level="INFO")

    def close(self) -> None:
        self.conn.close()


def _bool(b: bool) -> str:
    return "yes" if b else "no"


def status_report(app: App) -> str:
    cfg = app.config
    tables = db.table_names(app.conn)
    sec = secrets.secrets_status()
    tid_ok, tid_reason = touchid.is_available()

    configured = [k for k, v in sec.items() if v]
    missing = [k for k, v in sec.items() if not v]

    lines = [
        "",
        "======================== HYPE — status ========================",
        f"  version          : {__import__('hype').__version__}",
        f"  config file      : {paths.config_path()}",
        f"  database         : {paths.db_path()}",
        f"  log file         : {paths.log_path()}",
        f"  data dir         : {paths.data_dir()}",
        "  --------------------------------------------------------------",
        f"  mode             : {cfg.mode}",
        f"  per_trade_pct    : {cfg.trading.per_trade_pct}%  (cap ${cfg.trading.per_trade_cap_usd:,.0f})",
        f"  TP / SL          : +{cfg.trading.take_profit_pct}% / {cfg.trading.stop_loss_pct}%",
        f"  follow_top_n     : {cfg.ranker.follow_top_n}   refresh {cfg.ranker.rank_refresh_hours}h",
        f"  payout enabled   : {_bool(cfg.payout.enabled)}",
        f"  home wallet set  : {_bool(bool(cfg.payout.home_wallet_address))}",
        "  --------------------------------------------------------------",
        f"  DB tables ({len(tables)}) : {', '.join(tables)}",
        "  --------------------------------------------------------------",
        f"  Touch ID/password ready : {_bool(tid_ok)}" + (f"  ({tid_reason})" if not tid_ok else ""),
        f"  secrets configured      : {', '.join(configured) if configured else '(none yet)'}",
        f"  secrets missing         : {', '.join(missing) if missing else '(none)'}",
        "===============================================================",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    app = App()
    try:
        print(status_report(app))
        app.logger.info("Bootstrap complete.")
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
