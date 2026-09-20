"""Lock overlay (§6.6) — covers the dashboard, blurs it, and requires
password / Touch ID to return. Emits `unlocked` on success.

The blur is applied by the parent (MainWindow) to the dashboard widget; this
overlay sits on top with a frosted card.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QFrame, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..security.auth import AuthManager
from .auth_worker import TouchIDWorker
from .resources import asset_path
from .theme import C


class LockOverlay(QWidget):
    unlocked = pyqtSignal()

    def __init__(self, auth: AuthManager, parent: QWidget) -> None:
        super().__init__(parent)
        self.auth = auth
        self._worker: TouchIDWorker | None = None
        self.setObjectName("lockOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Scope the translucent background to THIS widget only (objectName
        # selector) so it doesn't bleed into the card/buttons inside it.
        self.setStyleSheet("QWidget#lockOverlay { background: rgba(244, 247, 250, 0.78); }")
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.addStretch()
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(320)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(28, 28, 28, 28)
        cl.setSpacing(12)

        logo = QLabel()
        pix = QPixmap(asset_path("logo.png"))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(logo)

        t = QLabel("Locked")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet(f"font-size: 18px; font-weight: 800; color: {C.OCEAN_DARK};")
        cl.addWidget(t)

        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText("Password")
        self.pw.returnPressed.connect(self._submit)
        cl.addWidget(self.pw)

        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {C.RED};")
        self.error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.error)

        unlock = QPushButton("Unlock")
        unlock.setObjectName("primary")
        # Explicit style as a safety net so it always reads as a filled button.
        unlock.setStyleSheet(
            f"QPushButton {{ background: {C.OCEAN}; color: white; border: none; "
            f"border-radius: 9px; padding: 10px 18px; font-weight: 700; }}"
            f"QPushButton:hover {{ background: {C.OCEAN_DARK}; }}")
        unlock.clicked.connect(self._submit)
        cl.addWidget(unlock)

        self.touch_btn = QPushButton("Use Touch ID")
        self.touch_btn.clicked.connect(self._touch_id)
        cl.addWidget(self.touch_btn)

        lay.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch()

    def focus_password(self) -> None:
        self.pw.setFocus()

    def _submit(self) -> None:
        if self.auth.verify_password(self.pw.text()):
            self._done()
        else:
            self.error.setText("Incorrect password")

    def _touch_id(self) -> None:
        self.touch_btn.setEnabled(False)
        self.touch_btn.setText("Waiting for Touch ID…")
        self._worker = TouchIDWorker(self.auth, "Unlock Hype")
        self._worker.done.connect(self._touch_done)
        self._worker.start()

    def _touch_done(self, ok: bool) -> None:
        self.touch_btn.setEnabled(True)
        self.touch_btn.setText("Use Touch ID")
        if ok:
            self._done()
        else:
            self.error.setText("Touch ID failed or cancelled")

    def _done(self) -> None:
        self.pw.clear()
        self.error.setText("")
        self.unlocked.emit()
