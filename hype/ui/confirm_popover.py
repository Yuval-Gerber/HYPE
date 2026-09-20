"""Small anchored confirmation card (not an OS dialog).

Shown just under the widget that triggered it — e.g. the Paper/Live toggle — as a
lightweight popover with Yes/No. Clicking away counts as No. Modeled on
BalancePopover so it matches the dashboard's look.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from .theme import C


class ConfirmPopover(QWidget):
    def __init__(self, parent, *, title: str, message: str,
                 on_yes: Callable[[], None], on_no: Optional[Callable[[], None]] = None,
                 yes_label: str = "Yes", no_label: str = "No", width: int = 300) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.on_yes = on_yes
        self.on_no = on_no
        self._decided = False

        # Transparent outer window; rounded card nested inside (transparent corners).
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 16)   # room for the drop shadow

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(width)
        card.setStyleSheet(
            f"QFrame#card {{ background: {C.CARD}; border: 1px solid {C.RED}; "
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

        t = QLabel(title)
        t.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.RED};")
        lay.addWidget(t)
        m = QLabel(message)
        m.setWordWrap(True)
        m.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        lay.addWidget(m)

        row = QHBoxLayout()
        no = QPushButton(no_label)
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.clicked.connect(self._no)
        yes = QPushButton(yes_label)
        yes.setObjectName("danger")
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.clicked.connect(self._yes)
        row.addWidget(no); row.addStretch(); row.addWidget(yes)
        lay.addLayout(row)

    def _yes(self) -> None:
        self._decided = True
        if self.on_yes:
            self.on_yes()
        self.close()

    def _no(self) -> None:
        self._decided = True
        if self.on_no:
            self.on_no()
        self.close()

    def closeEvent(self, e):
        # Clicking away (dismiss) counts as "No" so the toggle reverts.
        if not self._decided and self.on_no:
            self._decided = True
            self.on_no()
        super().closeEvent(e)
