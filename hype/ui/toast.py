"""Toast notification with expand/collapse for long messages.

A single toast in the top-right. If the message fits, it shows on one line and
auto-dismisses after 4s. If it's too long, the visible text is elided and an
expand arrow appears; expanding shows the full message and keeps the toast open
until you collapse it or click ✕. A new toast replaces the current one.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QIcon
from PyQt6.QtWidgets import QGridLayout, QLabel, QPushButton, QWidget

from .icons import icon_pixmap
from .theme import C

_TOP = 78
_MARGIN = 16
_LIFETIME_MS = 4000
_WIDTH = 320
_MSG_W = 210


class Toast(QWidget):
    closed = pyqtSignal(object)

    def __init__(self, parent: QWidget, message: str, dot: str) -> None:
        super().__init__(parent)
        self._closing = False
        self._expanded = False
        self._full = message

        self.setObjectName("toast")
        # Never steal focus/activation when shown — activating a transient widget
        # is what kicks a native-fullscreen Space back to the desktop on macOS.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#toast {{ background: {C.CARD}; border: 1px solid {C.BORDER_STRONG}; "
            f"border-radius: 12px; }}")
        grid = QGridLayout(self)
        grid.setContentsMargins(14, 10, 8, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(0)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(14)
        self._dot.setStyleSheet(f"color: {dot}; font-size: 13px; background: transparent;")
        self._dot.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._msg = QLabel()
        self._msg.setFixedWidth(_MSG_W)
        self._msg.setStyleSheet(f"color: {C.TEXT}; font-weight: 700; background: transparent;")
        self._msg.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        fm = QFontMetrics(self._msg.font())
        self._elided = fm.elidedText(message, Qt.TextElideMode.ElideRight, _MSG_W)
        self._needs_expand = self._elided != message

        self._expand_btn = QPushButton()
        self._expand_btn.setIcon(QIcon(icon_pixmap("chevron_down", C.MUTED, 12)))
        self._expand_btn.setIconSize(QSize(12, 12))
        self._expand_btn.setFixedSize(20, 20)
        self._expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._expand_btn.setStyleSheet("QPushButton{border:none;background:transparent;}")
        self._expand_btn.clicked.connect(self._toggle)
        self._expand_btn.setVisible(self._needs_expand)

        close = QPushButton()
        close.setIcon(QIcon(icon_pixmap("close", C.TEXT, 11)))
        close.setIconSize(QSize(11, 11))
        close.setFixedSize(20, 20)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet("QPushButton{border:none;background:transparent;}")
        close.clicked.connect(self._fire)

        grid.addWidget(self._dot, 0, 0, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self._msg, 0, 1)
        grid.addWidget(self._expand_btn, 0, 2, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(close, 0, 3, Qt.AlignmentFlag.AlignTop)

        self._apply_collapsed()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._timer.start(_LIFETIME_MS)

    # --- collapse / expand ---------------------------------------------------

    def _apply_collapsed(self) -> None:
        self._msg.setWordWrap(False)
        self._msg.setText(self._elided)
        self._resize(46)

    def _apply_expanded(self) -> None:
        self._msg.setWordWrap(True)
        self._msg.setText(self._full)
        h = self._msg.sizeHint().height()
        self._resize(max(46, h + 20))

    def _resize(self, card_h: int) -> None:
        self.setFixedSize(_WIDTH, card_h)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        if self._expanded:
            self._timer.stop()          # stay open while expanded
            self._expand_btn.setIcon(QIcon(icon_pixmap("chevron_up", C.MUTED, 12)))
            self._apply_expanded()
        else:
            self._expand_btn.setIcon(QIcon(icon_pixmap("chevron_down", C.MUTED, 12)))
            self._apply_collapsed()
            self._timer.start(_LIFETIME_MS)

    def _fire(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self.closed.emit(self)


class ToastManager:
    def __init__(self, window: QWidget) -> None:
        self.window = window
        self._current: Toast | None = None

    def show(self, message: str, *, kind: str = "info") -> None:
        if self._current is not None:
            old = self._current
            self._current = None
            old.hide()
            old.deleteLater()
        dot = {"ok": C.GREEN, "stop": C.RED, "info": C.OCEAN}.get(kind, C.OCEAN)
        t = Toast(self.window, message, dot)
        t.closed.connect(self._remove)
        self._current = t
        self._place(t)
        t.show()
        t.raise_()

    def _remove(self, toast: Toast) -> None:
        if toast is self._current:
            self._current = None
        toast.hide()
        toast.deleteLater()

    def reflow(self) -> None:
        if self._current is not None:
            self._place(self._current)

    def _place(self, t: Toast) -> None:
        t.move(self.window.width() - t.width() - _MARGIN, _TOP)
        t.raise_()
