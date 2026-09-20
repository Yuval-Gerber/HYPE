"""Collapsible left navigation sidebar.

Folded to a narrow icon rail by default; expands to show labels on hover
(animated). Hosts the page nav at the top and the connection-status indicators
at the bottom — both react to the expand/collapse. Emits currentChanged.
"""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QVariantAnimation, Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .icons import icon_pixmap
from .theme import C
from .widgets import StatusRow

COLLAPSED = 64
EXPANDED = 212
ICON_SIZE = 22


class NavItem(QWidget):
    clicked = pyqtSignal(int)

    def __init__(self, index: int, icon_name: str, label: str) -> None:
        super().__init__()
        self.index = index
        self.icon_name = icon_name
        self.selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(46)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 0, 12, 0)
        lay.setSpacing(14)

        self.icon = QLabel()
        self.icon.setFixedWidth(24)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setStyleSheet("background: transparent;")

        self.text = QLabel(label)
        self.text.setStyleSheet("background: transparent;")
        self.text.setVisible(False)

        lay.addWidget(self.icon)
        lay.addWidget(self.text)
        lay.addStretch()
        self._restyle()

    def set_label_visible(self, v: bool) -> None:
        self.text.setVisible(v)

    def set_selected(self, v: bool) -> None:
        self.selected = v
        self._restyle()

    def _restyle(self) -> None:
        color = C.OCEAN if self.selected else C.TEXT
        self.icon.setPixmap(icon_pixmap(self.icon_name, color, ICON_SIZE))
        if self.selected:
            self.setStyleSheet("NavItem { background: %s; border-radius: 10px; }" % C.OCEAN_TINT)
            self.text.setStyleSheet(f"color: {C.OCEAN_DARK}; font-weight: 700; background: transparent;")
        else:
            self.setStyleSheet("NavItem { background: transparent; border-radius: 10px; }")
            self.text.setStyleSheet(f"color: {C.MUTED}; font-weight: 600; background: transparent;")

    def enterEvent(self, e):
        if not self.selected:
            self.setStyleSheet("NavItem { background: rgba(2,119,189,0.06); border-radius: 10px; }")
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._restyle()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self.clicked.emit(self.index)
        super().mousePressEvent(e)


class CollapsibleSidebar(QFrame):
    currentChanged = pyqtSignal(int)

    def __init__(self, items: list[tuple[str, str]], status_names: list[str]) -> None:
        """items: list of (icon_name, label). status_names: connection indicators."""
        super().__init__()
        self.setObjectName("card")
        self.setFixedWidth(COLLAPSED)
        self._current = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 12, 8, 12)
        lay.setSpacing(6)

        self.items: list[NavItem] = []
        for i, (icon_name, label) in enumerate(items):
            it = NavItem(i, icon_name, label)
            it.clicked.connect(self.set_current)
            self.items.append(it)
            lay.addWidget(it)

        lay.addStretch()

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; background: {C.BORDER}; max-height: 1px;")
        lay.addWidget(sep)

        self.status: dict[str, StatusRow] = {}
        for name in status_names:
            row = StatusRow(name)
            self.status[name] = row
            lay.addWidget(row)

        self.items[0].set_selected(True)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(lambda v: self.setFixedWidth(int(v)))

    # --- selection -----------------------------------------------------------

    def set_current(self, index: int) -> None:
        if index == self._current:
            return
        self.items[self._current].set_selected(False)
        self._current = index
        self.items[index].set_selected(True)
        self.currentChanged.emit(index)

    def set_light(self, name: str, state: str) -> None:
        if name in self.status:
            self.status[name].set_state(state)

    # --- hover expand/collapse ----------------------------------------------

    def _animate_to(self, target: int) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.width())
        self._anim.setEndValue(target)
        self._anim.start()

    def _set_labels(self, visible: bool) -> None:
        for it in self.items:
            it.set_label_visible(visible)
        for row in self.status.values():
            row.set_label_visible(visible)

    def enterEvent(self, e):
        self._set_labels(True)
        self._animate_to(EXPANDED)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._set_labels(False)
        self._animate_to(COLLAPSED)
        super().leaveEvent(e)
