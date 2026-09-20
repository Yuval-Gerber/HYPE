"""Portable path resolution.

No Mac-specific paths are baked into core logic. Locations are derived from the
environment, the platform, and whether we're running as a packaged app:

  - Dev run:      <project>/config.toml and <project>/data/  (handy in the repo)
  - Packaged app: a stable per-user data dir, because the app bundle is
    read-only / ephemeral (PyInstaller _MEIPASS). On macOS that's
    ~/Library/Application Support/Hype ; elsewhere ~/.hype.

Env overrides always win:
  HYPE_HOME    -> data directory (DB, logs).
  HYPE_CONFIG  -> path to config.toml.
  HYPE_DB      -> path to the SQLite database file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def project_root() -> Path:
    """Repository root (the directory containing this package)."""
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Stable, writable per-user data directory. Created if missing."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Hype"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", home)) / "Hype"
    else:
        base = home / ".hype"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _base_dir() -> Path:
    """Where config/data live by default: project dir in dev, user dir packaged."""
    return user_data_dir() if _is_frozen() else project_root()


def data_dir() -> Path:
    """Directory for runtime data (DB, logs). Created if missing."""
    override = os.environ.get("HYPE_HOME")
    if override:
        base = Path(override).expanduser()
    elif _is_frozen():
        base = user_data_dir()
    else:
        base = project_root() / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    """Location of config.toml (the live-editable source of truth, §9)."""
    override = os.environ.get("HYPE_CONFIG")
    return Path(override).expanduser() if override else _base_dir() / "config.toml"


def db_path() -> Path:
    """Location of the SQLite database."""
    override = os.environ.get("HYPE_DB")
    return Path(override).expanduser() if override else data_dir() / "hype.db"


def log_path() -> Path:
    """Location of the rotating log file."""
    return data_dir() / "hype.log"
