"""Top bar (§7).

Left:   logo · Paper/Live slide toggle
Center: balance chips (paper & live)
Right:  Start/Stop slide toggle · Lock (icon only)

Both toggles use the same ocean-blue pill. Connection lights live in the
sidebar; Panic Drain lives in the System tab.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from .icons import icon_pixmap
from .resources import asset_path
from .theme import C
from .widgets import BalancesView, SlideToggle


class TopBar(QFrame):
    mode_changed = pyqtSignal(str)   # 'paper' | 'live'
    run_changed = pyqtSignal(str)    # 'start' | 'stop'
    lock_clicked = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")
        self.setFixedHeight(64)
        self._build()

    def _build(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(12)

        # Left: logo + mode toggle
        logo = QLabel()
        pix = QPixmap(asset_path("logo.png"))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(logo)
        lay.addSpacing(4)
        self.mode = SlideToggle([("paper", "PAPER"), ("live", "LIVE")], "paper",
                                on_color=C.OCEAN, width=170)
        self.mode.changed.connect(self.mode_changed.emit)
        lay.addWidget(self.mode)

        lay.addStretch()

        # Center: balances
        self.balances = BalancesView()
        lay.addWidget(self.balances)

        lay.addStretch()

        # Right: run toggle + lock
        self.run = SlideToggle([("start", "Start"), ("stop", "Stop")], "stop",
                               on_color=C.OCEAN, width=150)
        self.run.changed.connect(self.run_changed.emit)
        lay.addWidget(self.run)

        self.btn_lock = QPushButton()
        self.btn_lock.setIcon(QIcon(icon_pixmap("lock", C.TEXT, 20)))
        self.btn_lock.setIconSize(QSize(20, 20))
        self.btn_lock.setFixedSize(40, 36)
        self.btn_lock.setToolTip("Lock (blur + re-auth)")
        self.btn_lock.clicked.connect(self.lock_clicked.emit)
        lay.addWidget(self.btn_lock)

    # --- public API ----------------------------------------------------------

    def set_balances(self, *, paper: float | None, live: float | None) -> None:
        self.balances.set_balances(paper=paper, live=live)
