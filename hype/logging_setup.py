"""Structured logging — console + rotating file + DB activity feed.

Two layers:
  1. Standard Python logging to console and a rotating file (hype.log).
  2. `log_activity(category, message, **data)` — writes a structured row to the
     `activity_log` table that powers the dashboard's real-time feed (§7.5) and
     also emits to the standard logger.

SECURITY (§6.1): never log secrets. The KeyVault never returns secret values to
this module, and callers must not pass key material into log messages.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

from . import paths

_CONFIGURED = False


def utcnow_iso() -> str:
    """Consistent UTC ISO-8601 timestamp used across all stored rows."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logging once (idempotent). Returns the 'hype' logger."""
    global _CONFIGURED
    logger = logging.getLogger("hype")
    if _CONFIGURED:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        paths.log_path(), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger() -> logging.Logger:
    return setup_logging()


def log_activity(
    conn: sqlite3.Connection,
    category: str,
    message: str,
    *,
    level: str = "INFO",
    **data,
) -> None:
    """Record a structured action in the activity feed AND the standard log.

    `category` is a short tag (scan, buy, sell, tp, sl, investigation, mode,
    error, system). Extra kwargs are stored as JSON in `data_json`.
    """
    ts = utcnow_iso()
    data_json = json.dumps(data, default=str) if data else None
    conn.execute(
        "INSERT INTO activity_log(ts, level, category, message, data_json) "
        "VALUES(?,?,?,?,?)",
        (ts, level, category, message, data_json),
    )
    conn.commit()

    logger = get_logger()
    extra = f"  {data_json}" if data_json else ""
    logger.log(getattr(logging, level, logging.INFO), "[%s] %s%s", category, message, extra)


def recent_activity(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
