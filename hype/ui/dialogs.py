"""Reusable auth-confirm dialog — requires password AND Touch ID.

Used to gate sensitive actions (e.g. Panic Drain). Returns True only if the
password verifies and Touch ID succeeds.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from ..security.auth import AuthManager
from .auth_worker import TouchIDWorker
from .theme import C


class AuthConfirmDialog(QDialog):
    def __init__(self, auth: AuthManager, parent, title: str, message: str) -> None:
        super().__init__(parent)
        self.auth = auth
        self._worker: TouchIDWorker | None = None
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedWidth(380)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(12)

        head = QLabel(title)
        head.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {C.TEXT};")
        lay.addWidget(head)
        msg = QLabel(message)
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {C.MUTED};")
        lay.addWidget(msg)

        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText("Password")
        self.pw.returnPressed.connect(self._confirm)
        lay.addWidget(self.pw)

        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {C.RED};")
        lay.addWidget(self.error)

        row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.confirm = QPushButton("Confirm with Touch ID")
        self.confirm.setObjectName("danger")
        self.confirm.clicked.connect(self._confirm)
        row.addWidget(cancel)
        row.addStretch()
        row.addWidget(self.confirm)
        lay.addLayout(row)

    def _confirm(self) -> None:
        if not self.auth.verify_password(self.pw.text()):
            self.error.setText("Incorrect password")
            return
        self.error.setText("")
        self.confirm.setEnabled(False)
        self.confirm.setText("Waiting for Touch ID…")
        self._worker = TouchIDWorker(self.auth, "Authorize this action")
        self._worker.done.connect(self._touch_done)
        self._worker.start()

    def _touch_done(self, ok: bool) -> None:
        if ok:
            self.accept()
        else:
            self.confirm.setEnabled(True)
            self.confirm.setText("Confirm with Touch ID")
            self.error.setText("Touch ID failed or cancelled")


def require_password_and_touchid(parent, auth: AuthManager, title: str, message: str) -> bool:
    dlg = AuthConfirmDialog(auth, parent, title, message)
    return dlg.exec() == QDialog.DialogCode.Accepted


class HomeWalletChangeDialog(QDialog):
    """Change the payout (home) wallet — requires the full 3 factors (§6.3):
    password + secret nickname + Touch ID."""

    def __init__(self, auth: AuthManager, cfg, parent) -> None:
        super().__init__(parent)
        self.auth = auth
        self.cfg = cfg
        self._worker: TouchIDWorker | None = None
        self.new_address: str | None = None
        self.setWindowTitle("Change payout wallet")
        self.setModal(True)
        self.setFixedWidth(420)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(10)

        head = QLabel("Change payout (home) wallet")
        head.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {C.TEXT};")
        lay.addWidget(head)
        msg = QLabel("The only destination Hype can ever send funds to. Changing it "
                     "requires all three factors: password, secret nickname, and Touch ID.")
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {C.MUTED};")
        lay.addWidget(msg)

        self.addr = QLineEdit()
        self.addr.setPlaceholderText("New home wallet — Solana address (Base58)")
        self.pw = QLineEdit(); self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText("Password")
        self.nick = QLineEdit(); self.nick.setEchoMode(QLineEdit.EchoMode.Password)
        self.nick.setPlaceholderText("Secret nickname")
        for wdg in (self.addr, self.pw, self.nick):
            lay.addWidget(wdg)

        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {C.RED};")
        self.error.setWordWrap(True)
        lay.addWidget(self.error)

        row = QHBoxLayout()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        self.confirm = QPushButton("Confirm with Touch ID")
        self.confirm.setObjectName("primary")
        self.confirm.clicked.connect(self._confirm)
        row.addWidget(cancel); row.addStretch(); row.addWidget(self.confirm)
        lay.addLayout(row)

    def _confirm(self) -> None:
        addr = self.addr.text().strip()
        if not _looks_like_solana_address(addr):
            self.error.setText("That doesn't look like a Solana Base58 address.")
            return
        if not self.auth.verify_password(self.pw.text()):
            self.error.setText("Incorrect password.")
            return
        if not self.auth.verify_nickname(self.nick.text()):
            self.error.setText("Incorrect nickname.")
            return
        self.error.setText("")
        self.confirm.setEnabled(False)
        self.confirm.setText("Waiting for Touch ID…")
        self._worker = TouchIDWorker(self.auth, "Authorize changing the payout wallet")
        self._worker.done.connect(lambda ok: self._touch_done(ok, addr))
        self._worker.start()

    def _touch_done(self, ok: bool, addr: str) -> None:
        if not ok:
            self.confirm.setEnabled(True)
            self.confirm.setText("Confirm with Touch ID")
            self.error.setText("Touch ID failed or cancelled.")
            return
        from .. import config as cfgmod
        cfgmod.set_home_wallet_address(self.cfg, addr, authorized=True)
        self.new_address = addr
        self.accept()


def _looks_like_solana_address(s: str) -> bool:
    # Base58, 32–44 chars, no 0/O/I/l.
    if not (32 <= len(s) <= 44):
        return False
    allowed = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return all(c in allowed for c in s)
