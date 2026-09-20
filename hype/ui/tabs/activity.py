"""Activity Log tab (§7.5) — real-time, filterable, exportable feed.

Features: live table of the activity_log; filter by category, by date, and by
free text; mark individual rows (checkboxes) to export or delete just those;
export to CSV; delete selected or clear everything. Marks survive the live
refresh (kept by row id).
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..blockreasons import REASONS, classify
from ..datepicker import DateFilter
from ..theme import C
from ..widgets import ToggleSwitch

CATEGORIES = ["All", "system", "scan", "filter", "skip", "buy", "sell",
              "tp", "sl", "investigation", "mode", "error"]

CAT_COLOR = {
    "buy": C.GREEN, "tp": C.GREEN, "sell": C.OCEAN, "sl": C.RED,
    "error": C.RED, "investigation": C.AMBER, "skip": C.FAINT,
    "filter": C.MUTED, "scan": C.OCEAN_DARK, "system": C.MUTED,
    "mode": C.OCEAN_DARK,
}


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso


class ActivityLogTab(QWidget):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__()
        self.conn = conn
        self._marked: set[int] = set()
        self._loading = False
        self._rows: list = []
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1500)
        self._refresh()

    # --- build ---------------------------------------------------------------

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(10)

        # Row 1: title + filters
        row1 = QHBoxLayout()
        title = QLabel("Activity Log")
        title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {C.OCEAN_DARK};")
        row1.addWidget(title)
        self.count = QLabel("")
        self.count.setStyleSheet(f"color: {C.MUTED};")
        row1.addWidget(self.count)
        row1.addStretch()

        self.cat = QComboBox(); self.cat.addItems(CATEGORIES)
        self.cat.currentTextChanged.connect(self._refresh)

        # Custom calendar date filter ('All dates' when unset).
        self.date = DateFilter()
        self.date.changed.connect(self._refresh)

        self.search = QLineEdit(); self.search.setPlaceholderText("Search messages…")
        self.search.setFixedWidth(200)
        self.search.textChanged.connect(self._refresh)
        for w in (QLabel("Category"), self.cat, QLabel("Date"), self.date, self.search):
            row1.addWidget(w)
        lay.addLayout(row1)

        # Row 2: selection + actions
        row2 = QHBoxLayout()
        self.sel_label = QLabel("0 selected")
        self.sel_label.setStyleSheet(f"color: {C.MUTED};")
        row2.addWidget(self.sel_label)
        live = QLabel("Live"); live.setStyleSheet(f"color: {C.MUTED};")
        self.live = ToggleSwitch(True)
        row2.addSpacing(10); row2.addWidget(live); row2.addWidget(self.live)
        row2.addStretch()

        self.btn_export = QPushButton("Export")
        self.btn_export.clicked.connect(self._export)
        self.btn_del = QPushButton("Delete selected")
        self.btn_del.clicked.connect(self._delete_selected)
        self.btn_clear = QPushButton("Clear all")
        self.btn_clear.setObjectName("danger")
        self.btn_clear.clicked.connect(self._clear_all)
        for b in (self.btn_export, self.btn_del, self.btn_clear):
            row2.addWidget(b)
        lay.addLayout(row2)

        # Table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "When", "Category", "Message"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.resizeSection(0, 34)
        hh.resizeSection(1, 180)
        hh.resizeSection(2, 130)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.table)

    # --- data ----------------------------------------------------------------

    def _where(self):
        conds, params = [], []
        if self.cat.currentText() != "All":
            conds.append("category = ?"); params.append(self.cat.currentText())
        d = self.date.date()
        if d is not None:
            conds.append("date(ts, 'localtime') = ?")
            params.append(d.toString("yyyy-MM-dd"))
        s = self.search.text().strip()
        if s:
            conds.append("message LIKE ?"); params.append(f"%{s}%")
        return (" WHERE " + " AND ".join(conds)) if conds else "", params

    def _query(self, limit: int = 400):
        where, params = self._where()
        sql = (f"SELECT id, ts, level, category, message, data_json FROM activity_log"
               f"{where} ORDER BY id DESC LIMIT {limit}")
        try:
            return self.conn.execute(sql, params).fetchall()
        except Exception:
            return []

    def _refresh(self) -> None:
        if not self.live.isChecked():
            return
        rows = self._query()
        self._rows = rows
        self.count.setText(f"{len(rows)} shown")
        self._loading = True
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setData(Qt.ItemDataRole.UserRole, r["id"])
            chk.setCheckState(Qt.CheckState.Checked if r["id"] in self._marked
                              else Qt.CheckState.Unchecked)
            when = QTableWidgetItem(_fmt_dt(r["ts"])); when.setForeground(QColor(C.MUTED))
            cat = QTableWidgetItem(r["category"])
            cat.setForeground(QColor(CAT_COLOR.get(r["category"], C.MUTED)))
            fn = cat.font(); fn.setBold(True); cat.setFont(fn)
            msg = QTableWidgetItem(r["message"])
            if r["level"] == "ERROR":
                msg.setForeground(QColor(C.RED))
            elif r["level"] == "WARNING":
                msg.setForeground(QColor(C.AMBER))
            for col, item in enumerate((chk, when, cat, msg)):
                self.table.setItem(i, col, item)
        self._loading = False
        if not rows:
            self.table.setRowCount(1)
            empty = QTableWidgetItem("No activity yet — events appear here as the bot runs.")
            empty.setForeground(QColor(C.FAINT))
            self.table.setItem(0, 3, empty)
        self._update_sel_label()

    # --- marking -------------------------------------------------------------

    def _on_item_changed(self, item) -> None:
        if self._loading or item.column() != 0:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if rid is None:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._marked.add(rid)
        else:
            self._marked.discard(rid)
        self._update_sel_label()

    def _update_sel_label(self) -> None:
        n = len(self._marked)
        self.sel_label.setText(f"{n} selected")
        self.btn_del.setEnabled(n > 0)
        self.btn_export.setText("Export selected" if n else "Export all")

    # --- row detail ----------------------------------------------------------

    def _on_cell_clicked(self, row: int, col: int) -> None:
        if col == 0:            # checkbox column toggles selection, no detail
            return
        if 0 <= row < len(self._rows):
            ActivityDetailDialog(self, self._rows[row], self.conn).exec()

    # --- export --------------------------------------------------------------

    def _export(self) -> None:
        if self._marked:
            ph = ",".join("?" * len(self._marked))
            sql = (f"SELECT ts, level, category, message, data_json FROM activity_log "
                   f"WHERE id IN ({ph}) ORDER BY id DESC")
            rows = self.conn.execute(sql, tuple(self._marked)).fetchall()
        else:
            rows = self._query(limit=1000000)
        if not rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export activity log", "hype_activity.csv",
                                              "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fp:
                w = csv.writer(fp)
                w.writerow(["time", "level", "category", "message", "details"])
                for r in rows:
                    w.writerow([r["ts"], r["level"], r["category"], r["message"], r["data_json"] or ""])
        except Exception:
            pass

    # --- delete --------------------------------------------------------------

    def _delete_selected(self) -> None:
        if not self._marked:
            return
        n = len(self._marked)
        if QMessageBox.question(self, "Delete entries", f"Delete {n} selected log entr"
                                f"{'y' if n == 1 else 'ies'}?") != QMessageBox.StandardButton.Yes:
            return
        ph = ",".join("?" * len(self._marked))
        self.conn.execute(f"DELETE FROM activity_log WHERE id IN ({ph})", tuple(self._marked))
        self.conn.commit()
        self._marked.clear()
        self._refresh()

    def _clear_all(self) -> None:
        if QMessageBox.warning(
                self, "Clear all", "Delete the ENTIRE activity log? This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        self.conn.execute("DELETE FROM activity_log")
        self.conn.commit()
        self._marked.clear()
        self._refresh()


# Friendly labels for common data keys.
_FIELD_LABELS = {
    "trader": "Trader", "mint": "Token", "token": "Token", "amount": "Amount",
    "sig": "Signature", "position_id": "Position #", "pnl_usd": "P&L (USD)",
    "win": "Win", "fee": "Fee (USD)", "level_badge": "Investigation level", "kind": "Kind",
}


def _nickname(conn: sqlite3.Connection, addr: str):
    try:
        r = conn.execute("SELECT label FROM traders WHERE wallet_address=?", (addr,)).fetchone()
        return r["label"] if r and r["label"] else None
    except Exception:
        return None


class ActivityDetailDialog(QDialog):
    """Detail view for one activity-log row: full timestamp, message, and every
    structured field (trader nickname, token, signature with Solscan links)."""

    def __init__(self, parent, record, conn: sqlite3.Connection) -> None:
        super().__init__(parent)
        self.setWindowTitle("Activity detail")
        self.setModal(True)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)

        cat = record["category"]
        head = QLabel(cat.upper())
        head.setStyleSheet(f"font-size: 15px; font-weight: 800; color: {CAT_COLOR.get(cat, C.OCEAN_DARK)};")
        lay.addWidget(head)
        msg = QLabel(record["message"]); msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {C.TEXT}; font-weight: 600;")
        lay.addWidget(msg)

        # For a blocked buy, spell out WHY in plain language (one line per reason),
        # with the actual reading (e.g. "117% (max 100%)") when it was logged.
        if cat in ("filter", "skip"):
            keys = classify(cat, record["message"])
            if keys:
                try:
                    _d = json.loads(record["data_json"]) if record["data_json"] else {}
                except Exception:
                    _d = {}
                readings = _d.get("readings") or {}
                why = QLabel("Why this was blocked")
                why.setStyleSheet(f"color: {C.MUTED}; font-weight: 700; margin-top: 4px;")
                lay.addWidget(why)
                for k in keys:
                    title, expl = REASONS.get(k, (k, ""))
                    rd = readings.get(k)
                    extra = f" <span style='color:{C.OCEAN_DARK}'>({rd})</span>" if rd else ""
                    item = QLabel(f"• <b>{title}</b>{extra} — {expl}")
                    item.setWordWrap(True); item.setTextFormat(Qt.TextFormat.RichText)
                    item.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
                    lay.addWidget(item)

        grid = QGridLayout(); grid.setHorizontalSpacing(16); grid.setVerticalSpacing(8)
        row = 0

        def add(label: str, widget) -> None:
            nonlocal row
            lab = QLabel(label)
            lab.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
            lab.setAlignment(Qt.AlignmentFlag.AlignTop)
            grid.addWidget(lab, row, 0)
            grid.addWidget(widget, row, 1)
            row += 1

        def value_label(text: str) -> QLabel:
            w = QLabel(text); w.setWordWrap(True); w.setTextFormat(Qt.TextFormat.RichText)
            w.setOpenExternalLinks(True)
            w.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            return w

        add("Time", value_label(_fmt_dt(record["ts"])))
        add("Level", value_label(record["level"]))

        data = {}
        try:
            data = json.loads(record["data_json"]) if record["data_json"] else {}
        except Exception:
            data = {}

        for key, val in data.items():
            if key == "readings":
                continue
            label = _FIELD_LABELS.get(key, key.replace("_", " ").title())
            if key == "trader" and isinstance(val, str) and len(val) > 20:
                nick = _nickname(conn, val)
                short = f"{val[:6]}…{val[-6:]}"
                name = f"<b>{nick}</b> · " if nick else ""
                add(label, value_label(
                    f"{name}{short}<br><a href='https://solscan.io/account/{val}'>view on Solscan</a>"))
            elif key in ("mint", "token") and isinstance(val, str) and len(val) > 20:
                short = f"{val[:6]}…{val[-6:]}"
                add(label, value_label(
                    f"{short}<br><a href='https://solscan.io/token/{val}'>view on Solscan</a>"))
            elif key == "sig" and isinstance(val, str) and len(val) > 20:
                add(label, value_label(
                    f"<a href='https://solscan.io/tx/{val}'>view transaction on Solscan</a>"))
            else:
                add(label, value_label(str(val)))

        lay.addLayout(grid)
        lay.addStretch()

        close = QPushButton("Close"); close.setObjectName("primary")
        close.clicked.connect(self.accept)
        rowb = QHBoxLayout(); rowb.addStretch(); rowb.addWidget(close)
        lay.addLayout(rowb)
