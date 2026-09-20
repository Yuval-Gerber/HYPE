"""Locate bundled assets in both dev and PyInstaller-packaged runs."""

from __future__ import annotations

import os
import sys


def asset_path(name: str) -> str:
    """Absolute path to an asset file (icon/logo), dev or frozen app."""
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        base = os.path.join(sys._MEIPASS, "assets")  # type: ignore[attr-defined]
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    return os.path.join(base, name)
