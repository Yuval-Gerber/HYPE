"""Positions tab (§7.1) — open paper trades, live.

Reads open positions from the DB (the engine updates current_price each manage
cycle), shows token, trigger trader, entry, current price, live %P&L (color
coded), time held, TP/SL, and a per-row "Sell now" that asks the engine to close
the position.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..theme import C

COLS = ["Token", "Trader", "Nickname", "Invested", "Entry", "Current", "P&L", "Held", "TP / SL", ""]


def _short(s: str | None) -> str:
    return f"{s[:4]}…{s[-4:]}" if s and len(s) > 10 else (s or "—")


def _price(p) -> str:
    p = p or 0.0
    return f"${p:.6g}" if p else "—"


def _held(opened_ts: str) -> str:
    try:
        start = datetime.fromisoformat(opened_ts)
        secs = (datetime.now(timezone.utc) - start).total_seconds()
    except Exception:
        return "—"
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"


class PositionsTab(QWidget):
    def __init__(self, conn: sqlite3.Connection, controller) -> None:
        super().__init__()
        self.conn = conn
        self.controller = controller
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)   # refresh every 1s so the "held" timer ticks smoothly
        self.refresh()

    def _build(self) -> None:
        self._row_addrs: list = []   # trigger_trader per row (for the detail popup)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("Positions")
        title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {C.OCEAN_DARK};")
        head.addWidget(title)
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {C.MUTED};")
        head.addWidget(self.summary)
        head.addStretch()
        lay.addLayout(head)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        # Token gets a compact FIXED width (it only holds a short symbol); the
        # Nickname column stretches to absorb leftover space so the row fills nicely
        # and the Sell button always fits — no giant Token column.
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed); hh.resizeSection(0, 132)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)   # Nickname absorbs slack
        # 1 Trader · 3 Invested · 4 Entry · 5 Current · 6 P&L · 7 Held · 8 TP/SL · 9 Sell
        for i, w in {1: 88, 3: 76, 4: 90, 5: 90, 6: 68, 7: 56, 8: 92, 9: 112}.items():
            hh.resizeSection(i, w)
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(48)
        # Double-click a row → the same full trader-detail popup as the Traders tabs.
        self.table.cellDoubleClicked.connect(self._open_trader_detail)
        lay.addWidget(self.table)

    def _open_trader_detail(self, row: int, _col: int = 0) -> None:
        if not (0 <= row < len(self._row_addrs)):
            return
        addr = self._row_addrs[row]
        if not addr or not self.conn.execute(
                "SELECT 1 FROM traders WHERE wallet_address=?", (addr,)).fetchone():
            return   # adopted / test / unknown trigger — no trader record to show
        from .traders import TraderDetailDialog
        TraderDetailDialog(self.conn, addr, self).exec()

    def _nickname(self, addr):
        try:
            r = self.conn.execute("SELECT label FROM traders WHERE wallet_address=?", (addr,)).fetchone()
            return r["label"] if r and r["label"] else None
        except Exception:
            return None

    def _active_session(self):
        # Follow the ACTIVE mode (live or paper) so live positions show too.
        from ...config import load_config
        mode = load_config().mode
        return self.conn.execute(
            "SELECT id FROM sessions WHERE mode=? AND ended_ts IS NULL "
            "ORDER BY id DESC LIMIT 1", (mode,)).fetchone()

    def refresh(self) -> None:
        sess = self._active_session()
        if sess is None:
            self.table.setRowCount(0)
            self.summary.setText("— no active session yet (press Start)")
            return
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE session_id=? AND status='open' ORDER BY id DESC",
            (sess["id"],)).fetchall()
        self.table.setRowCount(len(rows))
        self._row_addrs = [p["trigger_trader"] for p in rows]
        total_unreal = 0.0
        for i, p in enumerate(rows):
            entry = p["entry_price"] or 0.0
            cur = p["current_price"] or entry
            pnl_pct = ((cur - entry) / entry * 100.0) if entry else 0.0
            unreal = (p["entry_qty"] or 0) * cur - (p["entry_amount_usd"] or 0)
            total_unreal += unreal
            green = pnl_pct >= 0

            def cell(text, color=None, bold=False):
                it = QTableWidgetItem(text)
                if color:
                    it.setForeground(QColor(color))
                if bold:
                    f = it.font(); f.setBold(True); it.setFont(f)
                return it

            self.table.setItem(i, 0, cell(p["token_symbol"] or _short(p["token_mint"]), C.TEXT, True))
            tr_cell = cell(_short(p["trigger_trader"]), C.MUTED)
            nick_cell = cell(self._nickname(p["trigger_trader"]) or "—", C.TEXT)
            for c in (tr_cell, nick_cell):
                c.setToolTip("Double-click for full trader details")
            self.table.setItem(i, 1, tr_cell)
            self.table.setItem(i, 2, nick_cell)
            self.table.setItem(i, 3, cell(f"${p['entry_amount_usd']:,.2f}" if p["entry_amount_usd"] else "—", C.TEXT))
            self.table.setItem(i, 4, cell(_price(entry)))
            self.table.setItem(i, 5, cell(_price(cur)))
            self.table.setItem(i, 6, cell(f"{pnl_pct:+.1f}%", C.GREEN if green else C.RED, True))
            self.table.setItem(i, 7, cell(_held(p["opened_ts"]), C.MUTED))
            self.table.setItem(i, 8, cell(f"+{p['tp_pct']:.0f}% / {p['sl_pct']:.0f}%", C.MUTED))

            btn = QPushButton("Sell now")
            btn.setObjectName("danger")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedSize(90, 30)
            btn.clicked.connect(lambda _=False, pid=p["id"]: self._sell(pid))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(6, 0, 6, 0)   # centered in a cell wider than the button
            hl.setSpacing(0)
            hl.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(i, 9, holder)

        color = C.GREEN if total_unreal >= 0 else C.RED
        self.summary.setText("")
        self.summary.setText(f"  {len(rows)} open · unrealized "
                             f"<span style='color:{color}'>${total_unreal:+,.2f}</span>")
        self.summary.setTextFormat(Qt.TextFormat.RichText)

    def _sell(self, position_id: int) -> None:
        if self.controller is not None:
            self.controller.request_sell(position_id)
