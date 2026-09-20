"""Hype desktop app entry point.

Launches the QApplication, applies the ocean-blue light theme, sets the H icon,
and shows the login → dashboard window. This is the PyInstaller entry too.

Dev run:  python -m hype.ui.app
"""

from __future__ import annotations

import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from ..security.auth import AuthManager
from .mainwindow import HypeWindow
from .resources import asset_path
from .theme import apply_theme


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Hype")
    app.setApplicationDisplayName("Hype")
    app.setWindowIcon(QIcon(asset_path("icon_1024.png")))
    apply_theme(app)

    auth = AuthManager()
    window = HypeWindow(auth)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
