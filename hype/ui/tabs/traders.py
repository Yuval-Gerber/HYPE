"""Traders tab (§7.3) — the followed copy-list + blacklist.

Two sub-tabs: Followed (the live roster Hype copies — the raw Birdeye top-N,
every one active the moment it's ranked) and Blacklist (wallets the owner benched
by hand; never copied or auto-added again). Supports nicknames, enable/disable
copying, manual add/remove, per-tab search, click-for-details, and a manual
"Pull traders" (the engine also re-ranks automatically on a timer).

All owner actions mutate the traders table directly and then ask the engine to
refresh its live watch set, so changes take effect without a restart.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QScrollArea, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ... import nicknames
from ...config import load_config
from ...engine.investigation import InvestigationEngine
from ...logging_setup import utcnow_iso
from ..theme import C

STATE_LABEL = {
    "active": "Active",
    "paused": "Paused",
    "dropped": "Dropped",
}
STATE_COLOR = {
    "active": C.GREEN,
    "paused": C.MUTED,
    "dropped": C.FAINT,
}

_B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _valid_address(addr: str) -> bool:
    return 32 <= len(addr) <= 44 and all(c in _B58 for c in addr)


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-6:]}" if addr and len(addr) > 14 else (addr or "—")


def _badge(level: int) -> str:
    return f"×{level}" if level and level >= 2 else ""


def _pnl_text(v) -> tuple[str, str]:
    if v is None:
        return "—", C.MUTED
    return (f"+${v:,.0f}" if v >= 0 else f"-${abs(v):,.0f}",
            C.GREEN if v >= 0 else C.RED)


def _last_active(iso) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
        return f"{int(secs // 86400)}d ago"
    except Exception:
        return "—"


class AddTraderDialog(QDialog):
    """Manually add a wallet to follow (address + optional nickname)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add trader")
        self.setModal(True)
        self.setMinimumWidth(440)
        self.address = ""
        self.nickname = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        t = QLabel("Add a trader to follow")
        t.setStyleSheet(f"font-size: 15px; font-weight: 800; color: {C.OCEAN_DARK};")
        lay.addWidget(t)
        lay.addWidget(self._muted("Paste a Solana wallet address (Base58). "
                                  "It's copied like any ranked wallet; you can nickname it."))
        self.addr_in = QLineEdit(); self.addr_in.setPlaceholderText("Wallet address")
        lay.addWidget(self.addr_in)
        self.nick_in = QLineEdit(); self.nick_in.setPlaceholderText("Nickname (optional)")
        lay.addWidget(self.nick_in)
        self.err = QLabel(""); self.err.setStyleSheet(f"color: {C.RED};")
        lay.addWidget(self.err)
        row = QHBoxLayout(); row.addStretch()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        add = QPushButton("Add"); add.setObjectName("primary"); add.clicked.connect(self._accept)
        row.addWidget(cancel); row.addWidget(add)
        lay.addLayout(row)

    def _muted(self, text: str) -> QLabel:
        w = QLabel(text); w.setWordWrap(True)
        w.setStyleSheet(f"color: {C.MUTED};")
        return w

    def _accept(self) -> None:
        addr = self.addr_in.text().strip()
        if not _valid_address(addr):
            self.err.setText("That doesn't look like a Solana address (Base58, 32–44 chars).")
            return
        self.address = addr
        self.nickname = self.nick_in.text().strip()
        self.accept()


def _fmt_ts(iso) -> str:
    """Absolute UTC timestamp, e.g. '2026-07-07 14:32 UTC', or '—'."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:  # noqa: BLE001
        return "—"


class TraderDetailDialog(QDialog):
    """Everything Hype knows about one trader — opened by double-clicking any row
    in the Followed or Blacklist tab (or a trader in the Positions tab). Shows
    identity, our own copied record, the live positions ledger, and the raw
    Birdeye stats."""

    def __init__(self, conn: sqlite3.Connection, address: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Trader details")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setMinimumHeight(520)

        tr = conn.execute("SELECT * FROM traders WHERE wallet_address=?", (address,)).fetchone()
        led = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl_usd),0) pnl, "
            " SUM(CASE WHEN realized_pnl_usd>0 THEN 1 ELSE 0 END) w, "
            " SUM(CASE WHEN realized_pnl_usd<=0 THEN 1 ELSE 0 END) l, COUNT(*) n "
            "FROM positions WHERE mode='live' AND status='closed' AND trigger_trader=?",
            (address,)).fetchone()
        open_n = conn.execute(
            "SELECT COUNT(*) n FROM positions WHERE mode='live' AND status='open' "
            "AND trigger_trader=?", (address,)).fetchone()["n"]
        led_pnl = led["pnl"] or 0.0
        led_w, led_l, led_n = led["w"] or 0, led["l"] or 0, led["n"] or 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(); v = QVBoxLayout(body)
        v.setContentsMargins(22, 20, 22, 20); v.setSpacing(6)
        scroll.setWidget(body); outer.addWidget(scroll)

        if tr is None:
            v.addWidget(QLabel("Trader not found."))
            return

        # Header: nickname + wallet link.
        name = QLabel(tr["label"] or _short(address))
        name.setStyleSheet(f"font-size: 17px; font-weight: 800; color: {C.OCEAN_DARK};")
        v.addWidget(name)
        sub = QLabel(f"<a href='https://solscan.io/account/{address}' "
                     f"style='color:{C.MUTED}; text-decoration:none;'>{_short(address)} ↗</a>")
        sub.setOpenExternalLinks(True); sub.setStyleSheet(f"color: {C.MUTED};")
        v.addWidget(sub)

        # --- Identity / status ---
        self._section(v, "Status")
        st = tr["investigation_state"]
        lvl = _badge(tr["investigation_level"])
        state_txt = "⛔ blacklisted" if st == "blacklisted" else STATE_LABEL.get(st, st)
        state_col = C.RED if st == "blacklisted" else STATE_COLOR.get(st, C.TEXT)
        self._kv(v, "State", f"{(lvl + ' ') if lvl else ''}{state_txt}", state_col)
        self._kv(v, "Copying", "● active" if tr["active"] else "○ benched",
                 C.GREEN if tr["active"] else C.MUTED)
        self._kv(v, "Source", "manual (owner-added)" if tr["source"] == "manual" else "ranked (Birdeye)")
        self._kv(v, "First seen", _fmt_ts(tr["created_ts"]))

        # --- Our copied record ---
        self._section(v, "Our copied record")
        self._kv(v, "Wins / Losses", f"{tr['copied_wins']}W / {tr['copied_losses']}L")
        self._kv(v, "Copied P&L", f"${(tr['copied_pnl_usd'] or 0):+,.2f}",
                 C.GREEN if (tr['copied_pnl_usd'] or 0) >= 0 else C.RED)
        self._kv(v, "Losing streak", str(tr["consecutive_losses"]))

        # --- Live positions ledger (all-time on Hype) ---
        self._section(v, "Live ledger (all-time)")
        self._kv(v, "Realized P&L", f"${led_pnl:+,.2f}", C.GREEN if led_pnl >= 0 else C.RED)
        self._kv(v, "Closed trades", f"{led_n}  ({led_w}W / {led_l}L)")
        self._kv(v, "Open now", str(open_n))

        # --- Birdeye stats (leaderboard) ---
        self._section(v, "Birdeye stats")
        bp, bc = _pnl_text(tr["realized_pnl_usd"])
        self._kv(v, "Realized PnL", bp, bc)
        wr = tr["win_rate"]
        self._kv(v, "Win rate", f"{wr * 100:.0f}%" if wr is not None else "—")
        self._kv(v, "Trade count", str(tr["trade_count_external"]) if tr["trade_count_external"] is not None else "—")
        self._kv(v, "Last active", _last_active(tr["last_active_ts"]))

        v.addStretch()
        row = QHBoxLayout(); row.addStretch()
        close = QPushButton("Close"); close.setObjectName("primary"); close.clicked.connect(self.accept)
        row.addWidget(close)
        outer.addLayout(row)

    def _section(self, lay: QVBoxLayout, title: str) -> None:
        w = QLabel(title.upper())
        w.setStyleSheet(f"color: {C.OCEAN_DARK}; font-weight: 800; font-size: 11px; "
                        f"margin-top: 10px; letter-spacing: 0.5px;")
        lay.addWidget(w)

    def _kv(self, lay: QVBoxLayout, key: str, value: str, color: str = C.TEXT) -> None:
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
        k = QLabel(key); k.setFixedWidth(150)
        k.setStyleSheet(f"color: {C.MUTED}; font-size: 12px;")
        val = QLabel(str(value)); val.setWordWrap(True)
        val.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: 600;")
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(k); row.addWidget(val, 1)
        lay.addLayout(row)


class TradersTab(QWidget):
    def __init__(self, conn: sqlite3.Connection, controller=None) -> None:
        super().__init__()
        self.conn = conn
        self.controller = controller
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(3000)
        self._refresh()

    # --- build ---------------------------------------------------------------

    def _build(self) -> None:
        self._selected_addr = None
        self._row_addrs: list[str] = []
        self._bl_addrs: list[str] = []
        self._filters = {i: "" for i in range(2)}   # per-tab search text
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("Traders")
        title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {C.OCEAN_DARK};")
        head.addWidget(title)
        self.count = QLabel(""); self.count.setStyleSheet(f"color: {C.MUTED};")
        head.addWidget(self.count)
        head.addStretch()
        add = QPushButton("+ Add trader"); add.clicked.connect(self._add)
        self.edit_btn = QPushButton("Edit nickname")
        self.edit_btn.setToolTip("Select a trader in the list, then rename it.")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(lambda: self._rename_addr(self._selected_addr))
        refresh = QPushButton("Pull traders"); refresh.setObjectName("primary")
        refresh.setToolTip("Fetch the Birdeye leaderboard now and refill the active roster "
                           "toward the target (Settings → Follow top N). Works running or stopped.")
        refresh.clicked.connect(self._refresh_ranking)
        head.addWidget(add); head.addWidget(self.edit_btn); head.addWidget(refresh)
        lay.addLayout(head)

        # Sub-tab pills: Followed | Blacklist
        pillrow = QHBoxLayout(); pillrow.setSpacing(6)
        self.pills: list[QPushButton] = []
        for i, label in enumerate(["Followed", "Blacklist"]):
            b = QPushButton(label); b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self._select_sub(idx))
            self.pills.append(b); pillrow.addWidget(b)
        pillrow.addStretch()
        lay.addLayout(pillrow)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._followed_page())       # 0
        self.stack.addWidget(self._blacklist_page())      # 1
        lay.addWidget(self.stack, 1)
        self._select_sub(0)

    def _blacklist_page(self) -> QWidget:
        page = QWidget(); pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(8)
        lbl = QLabel("Blacklisted wallets are never copied and never auto-added by the ranker "
                     "again — they survive re-ranks and live restarts. Remove one here to make "
                     "it eligible again.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {C.MUTED}; font-size: 11px;")
        pl.addWidget(lbl)
        pl.addWidget(self._search_box(1, "Search blacklist by nickname or wallet…"))
        self.bl_table = QTableWidget(0, 4)
        self.bl_table.setHorizontalHeaderLabels(["Nickname", "Wallet", "Realized PnL", ""])
        self.bl_table.verticalHeader().setVisible(False)
        self.bl_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.bl_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.bl_table.setShowGrid(False)
        bh = self.bl_table.horizontalHeader()
        bh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, w in ((1, 150), (2, 130), (3, 150)):
            bh.resizeSection(col, w)
        self.bl_table.cellDoubleClicked.connect(
            lambda r, _c: self._open_detail(self._bl_addrs, r))
        pl.addWidget(self.bl_table, 1)
        return page

    def _followed_page(self) -> QWidget:
        page = QWidget(); pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(8)
        lbl = QLabel("Ranked by realized PnL · double-click a trader for full details")
        lbl.setStyleSheet(f"color: {C.MUTED}; font-weight: 800; font-size: 11px;")
        pl.addWidget(lbl)
        pl.addWidget(self._search_box(0, "Search followed traders by nickname or wallet…"))
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Nickname", "Wallet", "Realized PnL", "Win", "Trades", "Last active", "Status", ""])
        # Numbered row column (1, 2, 3 …) so the roster reads as a list and the last
        # number = how many traders are shown (active sort to the top).
        vh = self.table.verticalHeader()
        vh.setVisible(True)
        vh.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setFixedWidth(40)
        vh.setStyleSheet(
            f"QHeaderView::section {{ background: {C.CARD}; color: {C.MUTED}; border: none; "
            f"font-weight: 700; }}")
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.cellDoubleClicked.connect(
            lambda r, _c: self._open_detail(self._row_addrs, r))
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, w in ((1, 140), (2, 110), (3, 60), (4, 70), (5, 100), (6, 140), (7, 44)):
            hh.resizeSection(col, w)
        self.table.setColumnWidth(7, 44)
        pl.addWidget(self.table, 1)
        return page

    def _select_sub(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        self._style_pills(idx)

    def _style_pills(self, sel: int) -> None:
        for i, b in enumerate(self.pills):
            b.setChecked(i == sel)
            if i == sel:
                b.setStyleSheet(
                    f"QPushButton {{ background: {C.OCEAN_TINT}; color: {C.OCEAN_DARK}; "
                    f"border: none; border-radius: 9px; padding: 8px 16px; font-weight: 700; }}")
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background: transparent; color: {C.MUTED}; border: none; "
                    f"border-radius: 9px; padding: 8px 16px; font-weight: 600; }}"
                    f"QPushButton:hover {{ color: {C.TEXT}; }}")

    # --- search --------------------------------------------------------------

    def _search_box(self, idx: int, placeholder: str) -> QLineEdit:
        """A per-tab search field — filters the tab's list by nickname or wallet
        address as you type (so you can find a trader fast in a long list)."""
        box = QLineEdit()
        box.setPlaceholderText(placeholder)
        box.setClearButtonEnabled(True)
        box.setStyleSheet("QLineEdit { padding: 6px 10px; border-radius: 8px; }")
        box.textChanged.connect(lambda t, i=idx: self._set_filter(i, t))
        return box

    def _set_filter(self, idx: int, text: str) -> None:
        self._filters[idx] = text or ""
        self._refresh()

    def _match(self, tr, idx: int) -> bool:
        q = self._filters.get(idx, "").strip().lower()
        if not q:
            return True
        return q in (tr["label"] or "").lower() or q in tr["wallet_address"].lower()

    def _open_detail(self, addrs, row: int) -> None:
        if 0 <= row < len(addrs) and addrs[row]:
            TraderDetailDialog(self.conn, addrs[row], self).exec()

    # --- data ----------------------------------------------------------------

    def _query(self):
        try:
            return self.conn.execute(
                "SELECT wallet_address, label, label_auto, source, active, realized_pnl_usd, win_rate, "
                "trade_count_external, last_active_ts, investigation_state, investigation_level "
                "FROM traders "
                "ORDER BY active DESC, (realized_pnl_usd IS NULL), realized_pnl_usd DESC").fetchall()
        except Exception:
            return []

    def _refresh(self) -> None:
        nicknames.ensure(self.conn)   # give any new trader a friendly name
        rows = self._query()
        bl = [r for r in rows if r["investigation_state"] == "blacklisted"]
        # Followed list = the live roster. Show every ACTIVE trader, plus any
        # INACTIVE one worth keeping on screen (owner-added or owner-nicknamed, for
        # score-keeping). Auto-named ranked wallets that dropped off the roster are
        # hidden so the active traders aren't buried under a graveyard of "off" rows
        # — they stay in the DB and reappear if they re-rank.
        def _keep(r):
            if r["investigation_state"] == "blacklisted":
                return False
            if r["active"]:
                return True
            return r["source"] == "manual" or (r["label_auto"] == 0)
        followed = [r for r in rows if _keep(r)]
        dropped = sum(1 for r in rows
                      if not _keep(r) and r["investigation_state"] != "blacklisted")
        n_active = sum(1 for r in followed if r["active"])
        tracked = len(followed)
        txt = f"{n_active} active"
        if tracked != n_active:
            txt += f" · {tracked} shown"
        if dropped:
            txt += f" · {dropped} inactive hidden"
        self.count.setText(txt)
        self._build_table(followed)
        self._build_blacklist(bl)
        self.pills[1].setText(f"Blacklist ({len(bl)})" if bl else "Blacklist")

    def _build_blacklist(self, rows) -> None:
        rows = [r for r in rows if self._match(r, 1)]
        self._bl_addrs = [r["wallet_address"] for r in rows]
        self.bl_table.setRowCount(len(rows) or 1)
        if not rows:
            self.bl_table.setItem(0, 0, _item("No blacklisted wallets. Blacklist a bad trader from "
                                              "its ⋮ menu.", C.FAINT))
            self.bl_table.setSpan(0, 0, 1, 4)
            return
        self.bl_table.clearSpans()
        for i, tr in enumerate(rows):
            addr = tr["wallet_address"]
            self.bl_table.setItem(i, 0, _item(tr["label"] or "—", C.TEXT))
            self.bl_table.setItem(i, 1, _item(_short(addr), C.MUTED))
            pnl_txt, pnl_col = _pnl_text(tr["realized_pnl_usd"])
            self.bl_table.setItem(i, 2, _item(pnl_txt, pnl_col, bold=True))
            btn = QPushButton("Remove")
            btn.setToolTip("Remove from blacklist — makes this wallet eligible to be followed again.")
            btn.setFixedHeight(30); btn.setMinimumWidth(96)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, a=addr: self.investigate(a, "unblacklist"))
            holder = QWidget(); hl = QHBoxLayout(holder); hl.setContentsMargins(6, 6, 6, 6)
            hl.addWidget(btn); hl.addStretch()
            self.bl_table.setCellWidget(i, 3, holder)
            self.bl_table.setRowHeight(i, 50)

    def _build_table(self, rows) -> None:
        rows = [tr for tr in rows if self._match(tr, 0)]
        sb = self.table.verticalScrollBar().value()
        self._loading = True
        self._row_addrs = [tr["wallet_address"] for tr in rows]
        self.table.setRowCount(len(rows))
        for i, tr in enumerate(rows):
            addr = tr["wallet_address"]
            disabled = not tr["active"]

            nick = QTableWidgetItem(tr["label"] or "—")
            if not tr["label"]:
                nick.setForeground(_qcolor(C.FAINT))
            self.table.setItem(i, 0, nick)

            self.table.setItem(i, 1, _item(_short(addr), C.MUTED))

            pnl_txt, pnl_col = _pnl_text(tr["realized_pnl_usd"])
            self.table.setItem(i, 2, _item(pnl_txt, pnl_col, bold=True))

            wr = tr["win_rate"]
            self.table.setItem(i, 3, _item(f"{wr * 100:.0f}%" if wr is not None else "—"))
            tc = tr["trade_count_external"]
            self.table.setItem(i, 4, _item(str(tc) if tc is not None else "—"))
            self.table.setItem(i, 5, _item(_last_active(tr["last_active_ts"]), C.MUTED))

            st = tr["investigation_state"]
            badge = _badge(tr["investigation_level"])
            label = STATE_LABEL.get(st, st)
            text = f"{badge + ' ' if badge else ''}{label}"
            if disabled:
                text += " · off"
            self.table.setItem(i, 6, _item(text, STATE_COLOR.get(st, C.MUTED) if not disabled
                                           else C.FAINT, bold=True))

            btn = QPushButton("⋮")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedSize(30, 26)
            btn.setStyleSheet(
                f"QPushButton {{ border: none; color: {C.MUTED}; font-size: 16px; "
                f"font-weight: 800; border-radius: 6px; }}"
                f"QPushButton:hover {{ background: {C.OCEAN_TINT}; color: {C.OCEAN_DARK}; }}")
            btn.clicked.connect(lambda _=False, t=tr, b=btn: self._open_menu(t, b))
            self.table.setCellWidget(i, 7, btn)
            self.table.setRowHeight(i, 40)

        if not rows:
            self.table.setRowCount(1)
            self.table.setItem(0, 0, _item("No traders yet — press Start to rank wallets, "
                                           "or add one manually.", C.FAINT))
        # Restore the previous selection (rows are rebuilt every refresh).
        if self._selected_addr in self._row_addrs:
            self.table.selectRow(self._row_addrs.index(self._selected_addr))
        else:
            self._selected_addr = None
        self._loading = False
        self.edit_btn.setEnabled(self._selected_addr is not None)
        self.table.verticalScrollBar().setValue(sb)

    def _on_select(self) -> None:
        if self._loading:
            return
        r = self.table.currentRow()
        self._selected_addr = self._row_addrs[r] if 0 <= r < len(self._row_addrs) else None
        self.edit_btn.setEnabled(self._selected_addr is not None)

    # --- actions -------------------------------------------------------------

    def _open_menu(self, tr, btn) -> None:
        addr = tr["wallet_address"]
        m = QMenu(self)
        m.addAction("Rename…", lambda: self._rename_addr(addr))
        if tr["active"]:
            m.addAction("Disable copying", lambda: self._set_active(addr, 0))
        else:
            m.addAction("Enable copying", lambda: self._set_active(addr, 1))
        m.addSeparator()
        if tr["investigation_state"] == "blacklisted":
            m.addAction("Remove from blacklist", lambda: self.investigate(addr, "unblacklist"))
        else:
            m.addAction("⛔ Blacklist (never follow again)", lambda: self._confirm_blacklist(addr))
        if tr["source"] == "manual":
            m.addSeparator()
            m.addAction("Remove trader", lambda: self._remove(tr))
        m.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _confirm_blacklist(self, addr: str) -> None:
        if QMessageBox.question(
                self, "Blacklist trader",
                "Blacklist this wallet? It will never be copied and never auto-added by the "
                "ranker again (survives re-ranks and live restarts) until you remove it from "
                "the Blacklist tab.") == QMessageBox.StandardButton.Yes:
            self.investigate(addr, "blacklist")

    def investigate(self, addr: str, action: str) -> None:
        try:
            eng = InvestigationEngine(self.conn, load_config())
            getattr(eng, action)(addr)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Action failed", str(e))
            return
        self._after_change()

    def _set_active(self, addr: str, active: int) -> None:
        self.conn.execute("UPDATE traders SET active=?, updated_ts=? WHERE wallet_address=?",
                          (active, utcnow_iso(), addr))
        self.conn.commit()
        self._after_change()

    def _rename_addr(self, addr) -> None:
        if not addr:
            return
        row = self.conn.execute("SELECT label FROM traders WHERE wallet_address=?",
                                (addr,)).fetchone()
        cur = (row["label"] if row else "") or ""
        text, ok = QInputDialog.getText(self, "Edit nickname",
                                        f"Nickname for {_short(addr)}:", text=cur)
        if not ok:
            return
        raw = text.strip()
        if raw:
            new, auto = raw, 0          # owner-set → never auto-regenerated
        else:
            new, auto = nicknames.generate(addr), 1   # blank → back to an auto-name
        self.conn.execute("UPDATE traders SET label=?, label_auto=?, updated_ts=? "
                          "WHERE wallet_address=?", (new, auto, utcnow_iso(), addr))
        self.conn.commit()
        self._after_change()

    def _remove(self, tr) -> None:
        addr = tr["wallet_address"]
        if QMessageBox.question(self, "Remove trader",
                                f"Remove {tr['label'] or _short(addr)} from the follow list?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        self.conn.execute("DELETE FROM traders WHERE wallet_address=? AND source='manual'", (addr,))
        self.conn.commit()
        self._after_change()

    def _add(self) -> None:
        dlg = AddTraderDialog(self)
        if not dlg.exec():
            return
        now = utcnow_iso()
        # Auto-nickname if the owner didn't type one; typed = owner-set (auto=0).
        auto = 0 if dlg.nickname else 1
        nick = dlg.nickname or nicknames.generate(dlg.address)
        try:
            self.conn.execute(
                "INSERT INTO traders(wallet_address, label, label_auto, source, active, "
                "investigation_state, investigation_level, created_ts, updated_ts) "
                "VALUES(?, ?, ?, 'manual', 1, 'active', 0, ?, ?) "
                "ON CONFLICT(wallet_address) DO UPDATE SET active=1, label=?, label_auto=?, "
                "updated_ts=?",
                (dlg.address, nick, auto, now, now, nick, auto, now))
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Add failed", str(e))
            return
        self._after_change()

    def _refresh_ranking(self) -> None:
        if self.controller is not None:
            self.controller.request_rerank()

    def _after_change(self) -> None:
        if self.controller is not None:
            self.controller.request_watch_refresh()
        self._refresh()


def _qcolor(hexstr: str):
    from PyQt6.QtGui import QColor
    return QColor(hexstr)


def _item(text: str, color: str = C.TEXT, *, bold: bool = False) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setForeground(_qcolor(color))
    if bold:
        f = it.font(); f.setBold(True); it.setFont(f)
    return it
