"""Performance tab (§7.2) — annotated equity curve + leaderboards.

Sub-tabs:
  Equity  — stat tiles, time-range tabs (1D/1W/1M/1Y/All), a reset, and the
            equity curve with clickable buy/sell markers and a hover crosshair
            that shows the date/time (without blocking the markers).
  Leaders — Top traders and Top coins by realized P&L, with gold/silver/bronze
            borders on the podium (top 3).

All data is scoped to the ACTIVE paper session, so resetting the balance (or the
'Reset' here) gives a fresh curve — test/false data under old sessions is ignored.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from ...config import load_config
from ...engine.account import Account
from ..theme import C
from ..widgets import StatTile

PODIUM = {0: "#D4AF37", 1: "#AEB4BC", 2: "#CD7F32"}   # gold / silver / bronze


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def _short(s) -> str:
    return f"{s[:4]}…{s[-4:]}" if s and len(s) > 10 else (s or "—")


def _nickname(conn, addr):
    try:
        r = conn.execute("SELECT label FROM traders WHERE wallet_address=?", (addr,)).fetchone()
        return r["label"] if r and r["label"] else None
    except Exception:
        return None


# --- equity chart ------------------------------------------------------------

class EquityChart(QWidget):
    markerClicked = pyqtSignal(dict)

    def __init__(self) -> None:
        super().__init__()
        pg.setConfigOptions(antialias=True)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.setBackground(C.CARD)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self.plot.setMouseEnabled(x=True, y=False)
        for ax in ("left", "bottom"):
            a = self.plot.getAxis(ax)
            a.setPen(C.BORDER_STRONG); a.setTextPen(C.MUTED)
        self.curve = self.plot.plot([], [], pen=pg.mkPen(C.OCEAN, width=2))

        self.markers = pg.ScatterPlotItem(size=12, pen=pg.mkPen("white", width=1.5))
        self.markers.sigClicked.connect(self._on_click)
        self.plot.addItem(self.markers)

        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen(C.FAINT, style=Qt.PenStyle.DashLine))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.vline.hide()
        self.tlabel = pg.TextItem(color=C.TEXT, fill=pg.mkBrush(255, 255, 255, 220), anchor=(0, 1))
        self.plot.addItem(self.tlabel, ignoreBounds=True)
        self.tlabel.hide()
        self.plot.scene().sigMouseMoved.connect(self._on_move)

        lay.addWidget(self.plot)

    def set_data(self, curve_pts: list, markers: list) -> None:
        xs = [p[0] for p in curve_pts]; ys = [p[1] for p in curve_pts]
        self.curve.setData(xs, ys)
        spots = []
        for m in markers:
            if m["marker"] == "buy":
                color, sym = C.OCEAN, "t1"          # up triangle
            elif m["marker"] == "withdraw":
                color, sym = "#8E44AD", "d"          # purple diamond = payout to home wallet
            else:
                reason = (m.get("note") or "").split(" ")[0]
                color = {"tp": C.GREEN, "sl": C.RED}.get(reason, C.AMBER)
                sym = "t"                            # down triangle
            spots.append({"pos": (m["x"], m["y"]), "data": m, "symbol": sym,
                          "brush": pg.mkBrush(color)})
        self.markers.setData(spots)

    def _on_click(self, _scatter, points) -> None:
        if len(points):
            self.markerClicked.emit(points[0].data())

    def _on_move(self, pos) -> None:
        vb = self.plot.getViewBox()
        if not self.plot.sceneBoundingRect().contains(pos):
            self.vline.hide(); self.tlabel.hide(); return
        mp = vb.mapSceneToView(pos)
        self.vline.setPos(mp.x()); self.vline.show()
        dt = datetime.fromtimestamp(mp.x(), tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        (x0, x1), (y0, y1) = vb.viewRange()
        self.tlabel.setText(dt)
        self.tlabel.setPos(mp.x(), y1)   # top edge → doesn't cover the markers/curve
        self.tlabel.show()


# --- leaders -----------------------------------------------------------------

class PodiumCard(QFrame):
    def __init__(self, rank: int, name: str, pnl: float, trades: int, winrate) -> None:
        super().__init__()
        border = PODIUM.get(rank, C.BORDER)
        width = 2 if rank in PODIUM else 1
        self.setObjectName("card")
        self.setStyleSheet(f"QFrame#card {{ background: {C.CARD}; border: {width}px solid {border}; "
                           f"border-radius: 12px; }}")
        lay = QHBoxLayout(self); lay.setContentsMargins(14, 10, 14, 10); lay.setSpacing(12)
        medal = "🥇🥈🥉"[rank] if rank < 3 else f"#{rank + 1}"
        r = QLabel(medal)
        r.setStyleSheet(f"font-size: 18px; font-weight: 800; color: {border if rank in PODIUM else C.MUTED};")
        r.setFixedWidth(34); r.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(r)
        nm = QLabel(name); nm.setStyleSheet(f"font-weight: 700; color: {C.TEXT};")
        lay.addWidget(nm)
        lay.addStretch()
        sub = QLabel(f"{trades} trades · {winrate}")
        sub.setStyleSheet(f"color: {C.MUTED}; font-size: 12px;")
        lay.addWidget(sub)
        p = QLabel(f"${pnl:+,.2f}")
        p.setStyleSheet(f"font-weight: 800; color: {C.GREEN if pnl >= 0 else C.RED};")
        lay.addWidget(p)


class LeaderColumn(QWidget):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.lay = QVBoxLayout(self); self.lay.setContentsMargins(0, 0, 0, 0); self.lay.setSpacing(8)
        t = QLabel(title); t.setStyleSheet(f"font-size: 15px; font-weight: 800; color: {C.OCEAN_DARK};")
        self.lay.addWidget(t)
        self._body = QVBoxLayout(); self._body.setSpacing(8)
        self.lay.addLayout(self._body)
        self.lay.addStretch()

    def set_rows(self, rows: list) -> None:
        while self._body.count():
            w = self._body.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not rows:
            empty = QLabel("No closed trades yet."); empty.setStyleSheet(f"color: {C.FAINT};")
            self._body.addWidget(empty)
            return
        for i, (name, pnl, n, wr) in enumerate(rows):
            self._body.addWidget(PodiumCard(i, name, pnl, n, wr))


class MarkerDetailDialog(QDialog):
    def __init__(self, parent, rec: dict, conn) -> None:
        super().__init__(parent)
        self.setWindowTitle("Trade detail")
        self.setModal(True)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self); lay.setContentsMargins(22, 20, 22, 18); lay.setSpacing(10)

        is_buy = rec["marker"] == "buy"
        note = rec.get("note") or ""
        parts = note.split(" ")
        action = parts[0] if parts else ""
        token = parts[-1] if len(parts) > 1 else ""
        head = QLabel("BUY" if is_buy else f"SELL · {action.upper()}")
        head.setStyleSheet(f"font-size: 15px; font-weight: 800; "
                           f"color: {C.OCEAN_DARK if is_buy else C.GREEN};")
        lay.addWidget(head)

        grid = QGridLayout(); grid.setHorizontalSpacing(16); grid.setVerticalSpacing(8)
        row = 0

        def add(label, text_html):
            nonlocal row
            lab = QLabel(label); lab.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
            val = QLabel(text_html); val.setTextFormat(Qt.TextFormat.RichText)
            val.setOpenExternalLinks(True); val.setWordWrap(True)
            val.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            grid.addWidget(lab, row, 0); grid.addWidget(val, row, 1); row += 1

        when = datetime.fromisoformat(rec["ts"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        add("Time", when)
        if token:
            add("Token", f"{_short(token)}<br><a href='https://solscan.io/token/{token}'>Solscan</a>")
        trader = rec.get("trader")
        if trader:
            nick = _nickname(conn, trader)
            name = f"<b>{nick}</b> · " if nick else ""
            add("Trader", f"{name}{_short(trader)}"
                          f"<br><a href='https://solscan.io/account/{trader}'>Solscan</a>")
        add("Balance after", f"${rec['y']:,.2f}")
        lay.addLayout(grid)
        lay.addStretch()
        close = QPushButton("Close"); close.setObjectName("primary"); close.clicked.connect(self.accept)
        r = QHBoxLayout(); r.addStretch(); r.addWidget(close); lay.addLayout(r)


class PerformanceTab(QWidget):
    def __init__(self, conn: sqlite3.Connection, controller=None) -> None:
        super().__init__()
        self.conn = conn
        self.controller = controller
        self._mode = load_config().mode   # "paper" | "live" — which session to show
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(2000)
        self.refresh()

    # --- build ---------------------------------------------------------------

    def _build(self) -> None:
        lay = QVBoxLayout(self); lay.setContentsMargins(4, 4, 4, 4); lay.setSpacing(12)
        titlerow = QHBoxLayout()
        title = QLabel("Performance")
        title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {C.OCEAN_DARK};")
        titlerow.addWidget(title)
        titlerow.addStretch()
        # Paper | Live scope — both always available so neither disappears when you
        # switch modes; the active mode is auto-selected (set_mode()).
        self.mode_pills = {}
        for m, label in (("paper", "○  Paper"), ("live", "●  Live")):
            b = QPushButton(label); b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, mm=m: self.set_mode(mm))
            self.mode_pills[m] = b; titlerow.addWidget(b)
        lay.addLayout(titlerow)

        # sub-tabs Equity | Leaders
        self.stack = QStackedWidget()
        self.stack.addWidget(self._equity_page())
        self.stack.addWidget(self._leaders_page())
        pillrow = QHBoxLayout(); pillrow.setSpacing(6)
        self.pills = []
        for i, label in enumerate(["Equity", "Leaders"]):
            b = QPushButton(label); b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self._select(idx))
            self.pills.append(b); pillrow.addWidget(b)
        pillrow.addStretch()
        lay.addLayout(pillrow)
        lay.addWidget(self.stack, 1)
        self._select(0)
        self._restyle_modes()

    def _equity_page(self) -> QWidget:
        page = QWidget(); pl = QVBoxLayout(page); pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(12)

        grid = QGridLayout(); grid.setSpacing(12)
        self.tiles = {
            "total": StatTile("Total P&L", "—"), "today": StatTile("Today P&L", "—"),
            "winrate": StatTile("Win rate", "—"), "trades": StatTile("Trades", "—"),
            "best": StatTile("Best", "—"), "worst": StatTile("Worst", "—"),
            "fees": StatTile("Fees", "—"),
        }
        for i, t in enumerate(self.tiles.values()):
            grid.addWidget(t, 0, i)
        pl.addLayout(grid)

        bar = QHBoxLayout()
        self.chart_hint = QLabel("Drag to pan · scroll to zoom the time range")
        self.chart_hint.setStyleSheet(f"color: {C.FAINT};")
        bar.addWidget(self.chart_hint)
        bar.addStretch()
        self.reset_btn = QPushButton("Reset"); self.reset_btn.setObjectName("danger")
        self.reset_btn.setToolTip("Clear the paper performance data (fresh session). Stop the bot first.")
        self.reset_btn.clicked.connect(self._reset)
        bar.addWidget(self.reset_btn)
        pl.addLayout(bar)

        card = QFrame(); card.setObjectName("card")
        cl = QVBoxLayout(card); cl.setContentsMargins(10, 10, 10, 10)
        self.chart = EquityChart()
        self.chart.markerClicked.connect(self._marker)
        cl.addWidget(self.chart)
        pl.addWidget(card, 1)
        return page

    def _leaders_page(self) -> QWidget:
        page = QWidget(); pl = QHBoxLayout(page); pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(16)
        self.top_traders = LeaderColumn("Top traders")
        self.top_coins = LeaderColumn("Top coins")
        pl.addWidget(self.top_traders); pl.addWidget(self.top_coins)
        return page

    # --- mode (paper/live) scope ---------------------------------------------

    def set_mode(self, mode: str) -> None:
        """Switch the whole tab between paper and live data. Called by the pills
        and by the main window when the top-bar Paper/Live toggle changes."""
        if mode not in ("paper", "live"):
            return
        self._mode = mode
        self._restyle_modes()
        self.refresh()

    def _restyle_modes(self) -> None:
        for m, b in self.mode_pills.items():
            on = m == self._mode
            b.setChecked(on)
            accent = C.OCEAN if m == "paper" else C.RED
            if on:
                b.setStyleSheet(f"QPushButton {{ background: {accent}; color: white; border: none; "
                                f"border-radius: 8px; padding: 6px 14px; font-weight: 700; }}")
            else:
                b.setStyleSheet(f"QPushButton {{ background: {C.CARD}; color: {C.MUTED}; "
                                f"border: 1px solid {C.BORDER_STRONG}; border-radius: 8px; padding: 6px 14px; }}")
        # Reset is available in both modes. Paper wipes to the paper starting
        # balance; Live re-baselines tracking to the CURRENT real wallet balance
        # (never touches funds) — used to start fresh real-data collection.
        if hasattr(self, "reset_btn"):
            if self._mode == "live":
                self.reset_btn.setText("Reset Live Data")
                self.reset_btn.setToolTip(
                    "Start fresh live performance tracking from your current wallet "
                    "balance. Does NOT touch your funds. Stop the bot and close all "
                    "live positions first.")
            else:
                self.reset_btn.setText("Reset")
                self.reset_btn.setToolTip(
                    "Clear the paper performance data (fresh session). Stop the bot first.")

    def _select(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        for i, b in enumerate(self.pills):
            b.setChecked(i == idx)
            if i == idx:
                b.setStyleSheet(f"QPushButton {{ background: {C.OCEAN_TINT}; color: {C.OCEAN_DARK}; "
                                f"border: none; border-radius: 9px; padding: 8px 16px; font-weight: 700; }}")
            else:
                b.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.MUTED}; border: none; "
                                f"border-radius: 9px; padding: 8px 16px; font-weight: 600; }}"
                                f"QPushButton:hover {{ color: {C.TEXT}; }}")
        self.refresh()

    # --- data ----------------------------------------------------------------

    def _session(self):
        return self.conn.execute(
            "SELECT id, starting_balance_usd, started_ts FROM sessions "
            "WHERE mode=? AND ended_ts IS NULL ORDER BY id DESC LIMIT 1", (self._mode,)).fetchone()

    def refresh(self) -> None:
        sess = self._session()
        if sess is None:
            self.chart.set_data([], [])
            for t in self.tiles.values():
                t.set_value("—")
            if self.stack.currentIndex() == 1:
                self.top_traders.set_rows([]); self.top_coins.set_rows([])
            return
        sid = sess["id"]
        self._refresh_chart(sid, sess)
        self._refresh_stats(sid, sess)
        if self.stack.currentIndex() == 1:
            self._refresh_leaders(sid)

    def _refresh_chart(self, sid, sess) -> None:
        # REALIZED equity curve: the line only moves on a CLOSED trade (by its
        # realized P&L) or a deposit/withdraw — NOT on buys or unrealized wiggle.
        # This kills the zigzag where each buy dipped the line by its gas/rent.
        import re as _re
        start_ts = _epoch(sess["started_ts"]) if sess["started_ts"] else None
        start_bal = sess["starting_balance_usd"] or 0.0
        # step events: closes (+realized pnl) and withdraws (−amount)
        events = [(_epoch(c["closed_ts"]), c["realized_pnl_usd"] or 0.0)
                  for c in self.conn.execute(
                      "SELECT closed_ts, realized_pnl_usd FROM positions "
                      "WHERE session_id=? AND status='closed' AND closed_ts IS NOT NULL",
                      (sid,)).fetchall()]
        for w in self.conn.execute(
                "SELECT ts, note FROM equity_snapshots WHERE session_id=? AND marker='withdraw'",
                (sid,)).fetchall():
            m = _re.search(r"\$([\d.]+)", w["note"] or "")
            if m:
                events.append((_epoch(w["ts"]), -float(m.group(1))))
        events.sort(key=lambda e: e[0])
        # build the step line
        curve = []
        running = start_bal
        if start_ts is not None:
            curve.append((start_ts, running))
        for ts, amt in events:
            curve.append((ts, running))      # flat up to the event
            running += amt
            curve.append((ts, running))      # step
        curve.append((datetime.now(timezone.utc).timestamp(), running))

        def _level_at(t: float) -> float:
            lvl = start_bal
            for ts, amt in events:
                if ts <= t:
                    lvl += amt
                else:
                    break
            return lvl

        # markers (buy/sell/withdraw) sit ON the realized line at their time
        snaps = self.conn.execute(
            "SELECT ts, marker, trigger_trader, note FROM equity_snapshots "
            "WHERE session_id=? AND marker IS NOT NULL ORDER BY ts", (sid,)).fetchall()
        markers = [{"x": _epoch(s["ts"]), "y": _level_at(_epoch(s["ts"])), "marker": s["marker"],
                    "trader": s["trigger_trader"], "note": s["note"], "ts": s["ts"]}
                   for s in snaps]
        self.chart.set_data(curve, markers)

    def _refresh_stats(self, sid, sess) -> None:
        closed = self.conn.execute(
            "SELECT realized_pnl_usd, closed_ts FROM positions WHERE session_id=? AND status='closed'",
            (sid,)).fetchall()
        pnls = [c["realized_pnl_usd"] or 0.0 for c in closed]
        n = len(pnls); wins = sum(1 for p in pnls if p > 0)
        # Total P&L = REALIZED sum (matches the realized curve). The old derived
        # equity overstated for live (ignored gas + locked rent); the honest live
        # equity is the real wallet, shown on the top bar and Telegram /pnl.
        total = sum(pnls)
        # today (local midnight)
        mid = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = sum((c["realized_pnl_usd"] or 0) for c in closed if _epoch(c["closed_ts"] or "") >= mid)
        fees = self.conn.execute(
            "SELECT COALESCE(SUM(fees_usd),0) f FROM trades t JOIN positions p ON p.id=t.position_id "
            "WHERE p.session_id=?", (sid,)).fetchone()["f"]

        def money(t, v):
            t.set_value(f"${v:+,.2f}", C.GREEN if v >= 0 else C.RED)
        money(self.tiles["total"], total)
        money(self.tiles["today"], today)
        self.tiles["winrate"].set_value(f"{wins / n * 100:.0f}%" if n else "—")
        self.tiles["trades"].set_value(str(n))
        self.tiles["best"].set_value(f"${max(pnls):+,.2f}" if pnls else "—", C.GREEN)
        self.tiles["worst"].set_value(f"${min(pnls):+,.2f}" if pnls else "—", C.RED)
        self.tiles["fees"].set_value(f"${fees:,.2f}")

    def _leader_rows(self, sid, group_col, is_trader):
        rows = self.conn.execute(
            f"SELECT {group_col} g, SUM(realized_pnl_usd) pnl, COUNT(*) n, "
            f"SUM(CASE WHEN realized_pnl_usd>0 THEN 1 ELSE 0 END) wins "
            f"FROM positions WHERE session_id=? AND status='closed' AND {group_col} IS NOT NULL "
            f"GROUP BY {group_col} ORDER BY pnl DESC LIMIT 8", (sid,)).fetchall()
        out = []
        for r in rows:
            name = (_nickname(self.conn, r["g"]) or _short(r["g"])) if is_trader else _short(r["g"])
            wr = f"{r['wins'] / r['n'] * 100:.0f}% win" if r["n"] else "—"
            out.append((name, r["pnl"] or 0.0, r["n"], wr))
        return out

    def _refresh_leaders(self, sid) -> None:
        self.top_traders.set_rows(self._leader_rows(sid, "trigger_trader", True))
        self.top_coins.set_rows(self._leader_rows(sid, "token_mint", False))

    # --- actions -------------------------------------------------------------

    def _marker(self, rec: dict) -> None:
        MarkerDetailDialog(self, rec, self.conn).exec()

    def _reset(self) -> None:
        if self.controller and self.controller.running:
            QMessageBox.information(self, "Reset", "Stop the bot first, then reset.")
            return
        if self._mode == "live":
            self._reset_live()
            return
        if QMessageBox.warning(
                self, "Reset performance",
                "Start a fresh paper session? The current curve, trades and leaderboards clear "
                "(old data is kept as history but no longer shown).",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        start = load_config().trading.paper_starting_balance_usd
        Account(self.conn, "paper").reset_session(start)
        self.refresh()

    def _reset_live(self) -> None:
        """Re-baseline LIVE performance tracking to the current real wallet
        balance. Does NOT move any funds — it only starts a fresh session so P&L,
        the equity curve and stats begin from now (used when going from testing to
        real profit-tracking after depositing)."""
        # Refuse if there are OPEN live positions — resetting would orphan real
        # on-chain positions under the old session (the engine only manages the
        # active one). Close them first.
        sess = self._session()
        if sess is not None:
            n_open = self.conn.execute(
                "SELECT COUNT(*) c FROM positions WHERE session_id=? AND status='open'",
                (sess["id"],)).fetchone()["c"]
            if n_open:
                QMessageBox.warning(
                    self, "Reset Live Data",
                    f"You have {n_open} open live position(s). Sell/close them first "
                    "(Positions tab) so real trades aren't left untracked, then reset.")
                return
        # Current real wallet balance = the new starting baseline.
        start = 0.0
        if self.controller is not None and hasattr(self.controller, "_live_cash_usd"):
            try:
                start = float(self.controller._live_cash_usd() or 0.0)
            except Exception:
                start = 0.0
        if QMessageBox.warning(
                self, "Reset Live Data",
                f"Start fresh LIVE performance tracking?\n\n"
                f"P&L, equity curve and stats reset to zero, baselined at your current "
                f"wallet balance (${start:,.2f}).\n\n"
                f"This does NOT touch your funds — only the performance tracking. "
                f"Old data is kept as history but no longer shown.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        Account(self.conn, "live").reset_session(start)
        self.refresh()
