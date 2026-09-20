"""Small popover for the top-bar balance chips.

Paper: reset or set the paper balance. Live: send funds to the home wallet
(placeholder — wired in P6). Not a modal dialog — a lightweight popup that
closes when you click away.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QDoubleValidator
from PyQt6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .theme import C


class BalancePopover(QWidget):
    def __init__(self, parent, *, mode: str, current: float, default: float,
                 on_submit: Optional[Callable[[float], None]] = None,
                 live_ctx: Optional[dict] = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.mode = mode
        self.on_submit = on_submit
        self.default = default
        self.live_ctx = live_ctx or {}

        # Transparent OUTER window; the rounded white card is nested inside it,
        # so the card's corners are transparent (no white square behind it).
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 16)   # room for the drop shadow

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(330)
        card.setStyleSheet(
            f"QFrame#card {{ background: {C.CARD}; border: 1px solid {C.BORDER_STRONG}; "
            f"border-radius: 12px; }}")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(26)
        shadow.setXOffset(0); shadow.setYOffset(6)
        shadow.setColor(QColor(15, 30, 45, 60))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        if mode == "paper":
            self._build_paper(lay, current)
        else:
            self._build_live(lay)

    def _build_paper(self, lay, current: float) -> None:
        t = QLabel("Paper balance")
        t.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        lay.addWidget(t)
        lay.addWidget(self._muted("Set or reset your simulated balance."))

        self.input = QLineEdit(f"{current:.2f}")
        self.input.setValidator(QDoubleValidator(0.0, 1_000_000_000.0, 2))
        self.input.setPlaceholderText("Amount (USD)")
        self.input.returnPressed.connect(self._submit)
        lay.addWidget(self.input)

        row = QHBoxLayout()
        reset = QPushButton(f"Reset to ${self.default:,.0f}")
        reset.clicked.connect(lambda: self._submit(self.default))
        setb = QPushButton("Set balance"); setb.setObjectName("primary")
        setb.clicked.connect(lambda: self._submit())   # ignore the clicked(bool)
        row.addWidget(reset); row.addStretch(); row.addWidget(setb)
        lay.addLayout(row)

    def _build_live(self, lay) -> None:
        self._price = self.live_ctx.get("sol_price")          # USD per SOL (or None)
        self._max_sol = float(self.live_ctx.get("max_sol") or 0.0)
        reserve = self.live_ctx.get("gas_reserve_sol")

        head = QHBoxLayout()
        t = QLabel("Send to MetaMask")
        t.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        head.addWidget(t); head.addStretch()
        # ⓘ — the gas-reserve note lives behind this icon (no cluttering text line).
        info = QLabel("ⓘ")
        rtxt = f"{reserve:.3f} SOL" if reserve is not None else "a small amount"
        info.setToolTip(f"Keeps {rtxt} in the wallet as a gas reserve so Hype can always pay "
                        f"transaction fees. Max sendable now: {self._max_usd_text()}.")
        info.setStyleSheet(f"color: {C.MUTED}; font-size: 15px;")
        info.setCursor(Qt.CursorShape.WhatsThisCursor)
        head.addWidget(info)
        lay.addLayout(head)

        # USD amount input — no spin arrows (plain field), like the live balance reads.
        self.input = QLineEdit()
        self.input.setValidator(QDoubleValidator(0.0, 1_000_000_000.0, 2))
        self.input.setPlaceholderText("Amount (USD)")
        self.input.textChanged.connect(self._live_equiv)
        self.input.returnPressed.connect(self._submit_live)
        lay.addWidget(self.input)

        self.equiv = self._muted("≈ 0.0000 SOL")
        lay.addWidget(self.equiv)

        row = QHBoxLayout()
        maxb = QPushButton(f"Max ({self._max_usd_text()})")
        maxb.clicked.connect(self._set_max)
        row.addWidget(maxb); row.addStretch()
        send = QPushButton("Send"); send.setObjectName("primary")
        send.clicked.connect(self._submit_live)
        row.addWidget(send)
        lay.addLayout(row)

    def _max_usd_text(self) -> str:
        if self._price:
            return f"${self._max_sol * self._price:,.2f}"
        return f"{self._max_sol:.4f} SOL"

    def _usd_to_sol(self, usd: float) -> float:
        return usd / self._price if self._price else usd  # no price → treat input as SOL

    def _set_max(self) -> None:
        if self._price:
            self.input.setText(f"{self._max_sol * self._price:.2f}")
        else:
            self.input.setText(f"{self._max_sol:.4f}")

    def _live_equiv(self) -> None:
        try:
            val = float(self.input.text().replace(",", ""))
        except (ValueError, AttributeError):
            self.equiv.setText("≈ 0.0000 SOL"); return
        self.equiv.setText(f"≈ {self._usd_to_sol(val):.4f} SOL")

    def _submit_live(self) -> None:
        try:
            val = float(self.input.text().replace(",", ""))
        except (ValueError, AttributeError):
            return
        sol = self._usd_to_sol(val)
        if sol <= 0:
            return
        if self.on_submit:
            self.on_submit(sol)   # controller enforces the cap + gas reserve again
        self.close()

    def _muted(self, text: str) -> QLabel:
        w = QLabel(text); w.setWordWrap(True)
        w.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        return w

    def _submit(self, amount: Optional[float] = None) -> None:
        if amount is None:
            try:
                amount = float(self.input.text().replace(",", ""))
            except (ValueError, AttributeError):
                return
        if self.on_submit:
            self.on_submit(amount)
        self.close()
