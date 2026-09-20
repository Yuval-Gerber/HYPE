"""Login view (§6.6) — password OR Touch ID, with first-run credential setup.

If no owner password exists yet, this shows a one-time setup form (create
password + secret nickname) so the owner can configure auth from the app itself.
Otherwise it shows the login form. Emits `authenticated` on success.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..security.auth import AuthManager
from .auth_worker import TouchIDWorker
from .resources import asset_path
from .theme import C


class LoginView(QWidget):
    authenticated = pyqtSignal()

    def __init__(self, auth: AuthManager) -> None:
        super().__init__()
        self.auth = auth
        self._worker: TouchIDWorker | None = None
        self.setObjectName("loginRoot")
        self._build()

    # --- UI ------------------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch()

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(380)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(36, 40, 36, 36)
        cl.setSpacing(10)

        logo = QLabel()
        pix = QPixmap(asset_path("logo.png"))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(120, 120, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(logo)

        # No "Hype" title — the logo already says Hype.
        self.subtitle = QLabel("")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setWordWrap(True)
        self.subtitle.setStyleSheet(f"color: {C.MUTED};")
        cl.addWidget(self.subtitle)

        self.setup_mode = not self.auth.is_password_set()

        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText("Password")
        self.pw.returnPressed.connect(self._submit)
        cl.addWidget(self.pw)

        # Setup-only fields.
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw2.setPlaceholderText("Confirm password")
        cl.addWidget(self.pw2)
        self.nick = QLineEdit()
        self.nick.setEchoMode(QLineEdit.EchoMode.Password)
        self.nick.setPlaceholderText("Secret nickname (3rd factor)")
        cl.addWidget(self.nick)

        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {C.RED};")
        self.error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.error)

        self.primary = QPushButton()
        self.primary.setObjectName("primary")
        self.primary.clicked.connect(self._submit)
        cl.addWidget(self.primary)

        self.touch_btn = QPushButton("Use Touch ID")
        self.touch_btn.clicked.connect(self._touch_id)
        cl.addWidget(self.touch_btn)

        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()

        self._apply_mode()

    def _apply_mode(self) -> None:
        if self.setup_mode:
            self.subtitle.setText("Create your owner password")
            self.primary.setText("Create password")
            self.pw.setPlaceholderText("New password (min 6 chars)")
            self.pw2.setVisible(True)
            self.nick.setVisible(True)
            self.touch_btn.setVisible(False)
        else:
            self.subtitle.setText("Enter password or use Touch ID to log in")
            self.primary.setText("Log in")
            self.pw.setPlaceholderText("Password")
            self.pw2.setVisible(False)
            self.nick.setVisible(False)
            self.touch_btn.setVisible(True)

    # --- actions -------------------------------------------------------------

    def _submit(self) -> None:
        self.error.setText("")
        if self.setup_mode:
            pw, pw2, nick = self.pw.text(), self.pw2.text(), self.nick.text().strip()
            if len(pw) < 6:
                return self._fail("Password must be at least 6 characters")
            if pw != pw2:
                return self._fail("Passwords don't match")
            if not nick:
                return self._fail("Nickname can't be empty")
            self.auth.set_password(pw)
            self.auth.set_nickname(nick)
            self.setup_mode = False
            self._clear()
            self.authenticated.emit()
        else:
            if self.auth.verify_password(self.pw.text()):
                self._clear()
                self.authenticated.emit()
            else:
                self._fail("Incorrect password")

    def _touch_id(self) -> None:
        self.error.setText("")
        self.touch_btn.setEnabled(False)
        self.touch_btn.setText("Waiting for Touch ID…")
        self._worker = TouchIDWorker(self.auth, "Unlock Hype")
        self._worker.done.connect(self._touch_done)
        self._worker.start()

    def _touch_done(self, ok: bool) -> None:
        self.touch_btn.setEnabled(True)
        self.touch_btn.setText("Use Touch ID")
        if ok:
            self._clear()
            self.authenticated.emit()
        else:
            self._fail("Touch ID failed or cancelled")

    def _fail(self, msg: str) -> None:
        self.error.setText(msg)

    def _clear(self) -> None:
        for f in (self.pw, self.pw2):
            f.clear()
        self.error.setText("")
