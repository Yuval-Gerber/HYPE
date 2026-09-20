"""Run Touch ID off the UI thread so the system prompt never freezes the app."""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from ..security.auth import AuthManager


class TouchIDWorker(QThread):
    done = pyqtSignal(bool)

    def __init__(self, auth: AuthManager, reason: str) -> None:
        super().__init__()
        self.auth = auth
        self.reason = reason

    def run(self) -> None:
        try:
            ok = self.auth.touch_id(self.reason)
        except Exception:
            ok = False
        self.done.emit(ok)
