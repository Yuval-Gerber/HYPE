"""Small reusable UI widgets (cards, badges, status dots, toggle)."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from .theme import C


class Card(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")


class StatusDot(QLabel):
    """A small colored connection indicator with a label."""

    GREY, GREEN, RED, AMBER = C.FAINT, C.GREEN, C.RED, C.AMBER

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.set_state("grey")

    def set_state(self, state: str) -> None:
        color = {"grey": self.GREY, "ok": self.GREEN, "down": self.RED, "warn": self.AMBER}.get(state, self.GREY)
        self.setText(f"● {self._name}")
        self.setStyleSheet(f"color: {color}; font-weight: 600; font-size: 12px;")


class Badge(QLabel):
    """A small rounded badge (e.g. investigation level ×2)."""

    def __init__(self, text: str, kind: str = "info") -> None:
        super().__init__(text)
        bg, fg = {
            "info": (C.OCEAN_TINT, C.OCEAN_DARK),
            "good": (C.GREEN_BG, C.GREEN),
            "bad": (C.RED_BG, C.RED),
            "warn": (C.AMBER_BG, C.AMBER),
        }.get(kind, (C.OCEAN_TINT, C.OCEAN_DARK))
        self.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 9px; padding: 2px 9px; "
            f"font-weight: 700; font-size: 11px;")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)


class StatTile(QFrame):
    """A labeled metric tile for the Performance tab."""

    def __init__(self, label: str, value: str = "—") -> None:
        super().__init__()
        self.setObjectName("statTile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        self._label = QLabel(label)
        self._label.setStyleSheet(f"color: {C.MUTED}; font-size: 12px; font-weight: 600;")
        self._value = QLabel(value)
        self._value.setStyleSheet(f"color: {C.TEXT}; font-size: 22px; font-weight: 800;")
        lay.addWidget(self._label)
        lay.addWidget(self._value)

    def set_value(self, text: str, color: str | None = None) -> None:
        self._value.setText(text)
        self._value.setStyleSheet(
            f"color: {color or C.TEXT}; font-size: 22px; font-weight: 800;")


class SlideToggle(QWidget):
    """Generic two-state sliding switch: one pill slides to the chosen side.

    options: [(key, label), (key, label)] (left, right). Pill uses on_color.
    Emits changed(key).
    """

    changed = pyqtSignal(str)

    def __init__(self, options: list[tuple[str, str]], initial: str,
                 *, on_color: str = C.OCEAN, width: int = 176, height: int = 36) -> None:
        super().__init__()
        from PyQt6.QtCore import QEasingCurve, QVariantAnimation
        self.options = options
        self.on_color = on_color
        self._index = 0 if initial == options[0][0] else 1
        self._t = float(self._index)
        self.setFixedSize(width, height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(190)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def key(self) -> str:
        return self.options[self._index][0]

    def set_key(self, key: str, *, emit: bool = False) -> None:
        idx = 0 if key == self.options[0][0] else 1
        if idx == self._index:
            return
        self._index = idx
        self._animate(float(idx))
        if emit:
            self.changed.emit(self.key())

    def _on_anim(self, v) -> None:
        self._t = float(v)
        self.update()

    def _animate(self, target: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(target)
        self._anim.start()

    def mousePressEvent(self, e) -> None:
        self._index = 1 - self._index
        self._animate(float(self._index))
        self.changed.emit(self.key())

    def paintEvent(self, e) -> None:
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QFont, QPainter
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C.BORDER))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)

        pad = 3
        pw = (w - 2 * pad) / 2
        x = pad + self._t * pw
        p.setBrush(QColor(self.on_color))
        p.drawRoundedRect(QRectF(x, pad, pw, h - 2 * pad), (h - 2 * pad) / 2, (h - 2 * pad) / 2)

        font = QFont(self.font())
        font.setBold(True)
        font.setPointSize(11)
        p.setFont(font)
        left = QRectF(pad, 0, pw, h)
        right = QRectF(pad + pw, 0, pw, h)
        p.setPen(QColor("white" if self._t < 0.5 else C.MUTED))
        p.drawText(left, Qt.AlignmentFlag.AlignCenter, self.options[0][1])
        p.setPen(QColor("white" if self._t >= 0.5 else C.MUTED))
        p.drawText(right, Qt.AlignmentFlag.AlignCenter, self.options[1][1])


class BalanceChip(QFrame):
    """Compact balance pill: small colored label + bold value. Clickable."""

    clicked = pyqtSignal()

    def __init__(self, label: str, accent: str) -> None:
        super().__init__()
        self.setObjectName("statTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"QFrame#statTile {{ background: {C.CARD_ALT}; border: 1px solid {C.BORDER}; "
            f"border-radius: 11px; }}"
            f"QFrame#statTile:hover {{ border-color: {C.OCEAN_LIGHT}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 6, 14, 6)
        lay.setSpacing(0)
        self._label = QLabel(label.upper())
        self._label.setStyleSheet(
            f"color: {accent}; font-size: 10px; font-weight: 800; letter-spacing: 1px; "
            f"background: transparent;")
        self._value = QLabel("—")
        self._value.setStyleSheet(
            f"color: {C.TEXT}; font-size: 15px; font-weight: 800; background: transparent;")
        lay.addWidget(self._label)
        lay.addWidget(self._value)

    def set_value(self, text: str, muted: bool = False) -> None:
        self._value.setText(text)
        color = C.FAINT if muted else C.TEXT
        self._value.setStyleSheet(
            f"color: {color}; font-size: 15px; font-weight: 800; background: transparent;")

    def mousePressEvent(self, e):
        self.clicked.emit()
        super().mousePressEvent(e)


class BalancesView(QWidget):
    """The two balance chips (paper & live) for the top bar center."""

    paperClicked = pyqtSignal()
    liveClicked = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.paper = BalanceChip("Paper", C.OCEAN)
        self.live = BalanceChip("Live", C.MUTED)
        self.paper.clicked.connect(self.paperClicked.emit)
        self.live.clicked.connect(self.liveClicked.emit)
        lay.addWidget(self.paper)
        lay.addWidget(self.live)
        self.set_balances(paper=1000.0, live=None)

    def set_balances(self, *, paper: float | None, live: float | None) -> None:
        self.paper.set_value(f"${paper:,.2f}" if paper is not None else "—", muted=paper is None)
        self.live.set_value(f"${live:,.2f}" if live is not None else "—", muted=live is None)


class ToggleSwitch(QWidget):
    """Compact iOS-style on/off switch (ocean when on)."""

    toggled = pyqtSignal(bool)

    def __init__(self, on: bool = False) -> None:
        super().__init__()
        from PyQt6.QtCore import QEasingCurve, QVariantAnimation
        self._on = on
        self._t = 1.0 if on else 0.0
        self.setFixedSize(46, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(lambda v: (setattr(self, "_t", float(v)), self.update()))

    def isChecked(self) -> bool:
        return self._on

    def setChecked(self, on: bool) -> None:
        if on == self._on:
            return
        self._on = on
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def mousePressEvent(self, e) -> None:
        self.setChecked(not self._on)
        self.toggled.emit(self._on)

    def paintEvent(self, e) -> None:
        from PyQt6.QtCore import QRectF
        from PyQt6.QtGui import QColor, QPainter
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # blend track color border->ocean
        def lerp(a, b):
            return int(a + (b - a) * self._t)
        c0 = QColor(C.BORDER_STRONG)
        c1 = QColor(C.OCEAN)
        track = QColor(lerp(c0.red(), c1.red()), lerp(c0.green(), c1.green()), lerp(c0.blue(), c1.blue()))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        # knob
        d = h - 6
        x = 3 + self._t * (w - d - 6)
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, 3, d, d))


class InfoButton(QPushButton):
    """A button that shows a styled explanation popup on hover (a reliable
    custom tooltip — the native one wasn't appearing)."""

    def __init__(self, text: str, tip: str, parent=None) -> None:
        super().__init__(text, parent)
        self._tip_text = tip
        self._tip: QLabel | None = None

    def _ensure(self) -> None:
        if self._tip is None:
            self._tip = QLabel(self._tip_text)
            self._tip.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
            self._tip.setWordWrap(True)
            self._tip.setMaximumWidth(320)
            self._tip.setStyleSheet(
                f"background: {C.OCEAN_DEEP}; color: white; padding: 9px 12px; "
                f"border-radius: 8px; font-weight: 600;")

    def enterEvent(self, e):
        self._ensure()
        self._tip.adjustSize()
        gp = self.mapToGlobal(self.rect().topRight())
        self._tip.move(gp.x() - self._tip.width(), gp.y() - self._tip.height() - 8)
        self._tip.show()
        super().enterEvent(e)

    def leaveEvent(self, e):
        if self._tip:
            self._tip.hide()
        super().leaveEvent(e)

    def hideEvent(self, e):
        if self._tip:
            self._tip.hide()
        super().hideEvent(e)


class StatusRow(QWidget):
    """A connection indicator for the sidebar: colored dot + label (label hides
    when the sidebar is collapsed)."""

    COLORS = {"grey": C.FAINT, "ok": C.GREEN, "down": C.RED, "warn": C.AMBER}

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.setFixedHeight(26)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 0, 12, 0)
        lay.setSpacing(14)
        self.dot = QLabel("●")
        self.dot.setFixedWidth(24)
        self.dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text = QLabel(name)
        self.text.setStyleSheet(f"color: {C.MUTED}; font-size: 12px; background: transparent;")
        self.text.setVisible(False)
        lay.addWidget(self.dot)
        lay.addWidget(self.text)
        lay.addStretch()
        self.set_state("grey")

    def set_state(self, state: str) -> None:
        color = self.COLORS.get(state, C.FAINT)
        self.dot.setStyleSheet(f"color: {color}; font-size: 13px; background: transparent;")

    def set_label_visible(self, v: bool) -> None:
        self.text.setVisible(v)
