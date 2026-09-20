"""System tab — sub-tabs for live engine Status and the effective Config, plus
Panic Drain.

Status: engine running state + counters + a 'run a test paper trade' button.
Config: a READ-ONLY view of every setting currently in config.toml, so you can
verify a change in Settings actually took effect. (Engine settings apply on the
next Start.)
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from ...config import load_config
from ...security.auth import AuthManager
from ..blockreasons import ORDER, REASONS, classify
from ..dialogs import require_password_and_touchid
from ..theme import C
from ..widgets import InfoButton, StatTile
from .settings import GROUPS


def _fmt_uptime(s) -> str:
    if not s:
        return "—"
    s = int(s); h = s // 3600; m = (s % 3600) // 60; sec = s % 60
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _fmt_age(s) -> str:
    if s is None:
        return "—"
    s = int(s)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def _fmt_ms(v) -> str:
    return "—" if v is None else f"{v:.0f} ms"


def _fmt(kind: str, val) -> str:
    if val is None:
        return "Not set" if kind == "opt_usd" else "Off"
    if kind == "bool":
        return "On" if val else "Off"
    if kind == "pct":
        return f"{val:g} %"
    if kind in ("usd", "opt_usd"):
        return f"${val:,.2f}"
    if kind == "sol":
        return f"{val:g} SOL"
    if kind == "bps":
        return f"{val} bps"
    if kind == "sec":
        return f"{val:g} s"
    return str(val)


class ConfigView(QWidget):
    """Read-only display of the effective config.toml, grouped."""

    def __init__(self) -> None:
        super().__init__()
        self._values: dict[tuple[str, str], QLabel] = {}
        self._general: dict[str, QLabel] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(); col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 8, 0); col.setSpacing(12)

        col.addWidget(self._general_card())
        for title, group_attr, fields in GROUPS:
            col.addWidget(self._group_card(title, group_attr, fields))
        col.addStretch()
        scroll.setWidget(body)
        lay.addWidget(scroll)
        self.refresh()

    def _general_card(self) -> QFrame:
        card = QFrame(); card.setObjectName("card")
        v = QVBoxLayout(card); v.setContentsMargins(20, 16, 20, 16); v.setSpacing(8)
        t = QLabel("General"); t.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        v.addWidget(t)
        form = QFormLayout(); form.setHorizontalSpacing(24); form.setVerticalSpacing(8)
        for key, label in (("mode", "Mode"), ("home_wallet", "Home wallet")):
            val = QLabel("—"); val.setStyleSheet(f"font-weight: 700; background: transparent;")
            self._general[key] = val
            lab = QLabel(label); lab.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
            form.addRow(lab, val)
        v.addLayout(form)
        return card

    def _group_card(self, title: str, group_attr: str, fields) -> QFrame:
        card = QFrame(); card.setObjectName("card")
        v = QVBoxLayout(card); v.setContentsMargins(20, 16, 20, 16); v.setSpacing(8)
        t = QLabel(title); t.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        v.addWidget(t)
        form = QFormLayout(); form.setHorizontalSpacing(24); form.setVerticalSpacing(8)
        for attr, label, kind in fields:
            val = QLabel("—"); val.setStyleSheet("font-weight: 700; background: transparent;")
            self._values[(group_attr, attr)] = val
            lab = QLabel(label); lab.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
            form.addRow(lab, val)
        v.addLayout(form)
        return card

    def refresh(self) -> None:
        try:
            cfg = load_config()
        except Exception:
            return
        self._general["mode"].setText(cfg.mode)
        hw = cfg.payout.home_wallet_address
        self._general["home_wallet"].setText(f"{hw[:6]}…{hw[-6:]}" if hw else "Not set")
        for title, group_attr, fields in GROUPS:
            group = getattr(cfg, group_attr)
            for attr, label, kind in fields:
                self._values[(group_attr, attr)].setText(_fmt(kind, getattr(group, attr)))


class BlocksView(QWidget):
    """Explains every reason Hype can block a buy and how often each has fired,
    read live from the activity log (filter + skip rows)."""

    def __init__(self, conn=None) -> None:
        super().__init__()
        self.conn = conn
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        intro = QLabel("Every reason Hype can block a buy, with how often it has fired. "
                       "A single block can trip several reasons at once, so the "
                       "percentages can add up to more than 100%.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {C.MUTED};")
        lay.addWidget(intro)
        self.total_lbl = QLabel("")
        self.total_lbl.setStyleSheet(f"color: {C.FAINT}; font-weight: 700;")
        lay.addWidget(self.total_lbl)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(); col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 8, 0); col.setSpacing(8)
        self.rows: dict[str, tuple[QLabel, QProgressBar]] = {}
        for key in ORDER:
            title, expl = REASONS[key]
            col.addWidget(self._reason_card(key, title, expl))
        col.addStretch()
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        self.refresh()

    def _reason_card(self, key: str, title: str, expl: str) -> QFrame:
        card = QFrame(); card.setObjectName("card")
        v = QVBoxLayout(card); v.setContentsMargins(16, 12, 16, 12); v.setSpacing(6)
        head = QHBoxLayout()
        t = QLabel(title); t.setStyleSheet(f"font-weight: 800; color: {C.TEXT};")
        head.addWidget(t); head.addStretch()
        stat = QLabel("—"); stat.setStyleSheet(f"font-weight: 800; color: {C.OCEAN_DARK};")
        head.addWidget(stat)
        v.addLayout(head)
        e = QLabel(expl); e.setWordWrap(True); e.setStyleSheet(f"color: {C.MUTED};")
        v.addWidget(e)
        bar = QProgressBar(); bar.setRange(0, 100); bar.setValue(0)
        bar.setTextVisible(False); bar.setFixedHeight(6)
        bar.setStyleSheet(
            f"QProgressBar {{ background: {C.OCEAN_TINT}; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {C.OCEAN}; border-radius: 3px; }}")
        v.addWidget(bar)
        self.rows[key] = (stat, bar)
        return card

    def _tally(self) -> tuple[dict[str, int], int]:
        counts = {k: 0 for k in ORDER}
        total = 0
        if self.conn is None:
            return counts, total
        try:
            rows = self.conn.execute(
                "SELECT category, message FROM activity_log "
                "WHERE category IN ('filter', 'skip')").fetchall()
        except Exception:
            return counts, total
        for r in rows:
            keys = classify(r["category"], r["message"])
            if not keys:
                continue
            total += 1
            for k in keys:
                if k in counts:
                    counts[k] += 1
        return counts, total

    def refresh(self) -> None:
        counts, total = self._tally()
        self.total_lbl.setText(
            f"{total:,} blocked buys logged" if total else "No blocked buys logged yet.")
        for key, (stat, bar) in self.rows.items():
            c = counts.get(key, 0)
            pct = (c / total * 100.0) if total else 0.0
            stat.setText(f"{c:,} · {pct:.0f}%")
            bar.setValue(int(round(pct)))


class SystemTab(QWidget):
    _funds_ready = pyqtSignal(dict)

    def __init__(self, auth: AuthManager, controller=None, conn=None, telegram=None,
                 wallet=None) -> None:
        super().__init__()
        self.auth = auth
        self.controller = controller
        self.conn = conn
        self.telegram = telegram
        self.wallet = wallet
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(12)

        title = QLabel("System")
        title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {C.OCEAN_DARK};")
        lay.addWidget(title)

        # sub-tabs: Status | Funds | Telegram | MetaMask | Blocks | Config
        self.stack = QStackedWidget()
        self.stack.addWidget(self._status_page())        # 0
        self.stack.addWidget(self._funds_page())         # 1
        self.stack.addWidget(self._telegram_page())      # 2
        self.stack.addWidget(self._metamask_page())      # 3
        self.blocks_view = BlocksView(conn)
        self.stack.addWidget(self.blocks_view)           # 4
        self.config_view = ConfigView()
        self.stack.addWidget(self.config_view)           # 5

        pillrow = QHBoxLayout(); pillrow.setSpacing(6)
        self.pills = []
        for i, label in enumerate(["Status", "Funds", "Telegram", "MetaMask", "Blocks", "Config"]):
            b = QPushButton(label); b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self._select(idx))
            self.pills.append(b); pillrow.addWidget(b)
        pillrow.addStretch()
        lay.addLayout(pillrow)
        lay.addWidget(self.stack, 1)
        self._select(0)

        # Panic Drain (always visible)
        row = QHBoxLayout(); row.addStretch()
        self.btn_panic = InfoButton(
            "Panic Drain",
            "Immediately sweeps all funds (above the gas reserve) from the trading "
            "wallet to your home wallet — the only allowed destination.")
        self.btn_panic.setObjectName("danger")
        self.btn_panic.clicked.connect(self._panic)
        row.addWidget(self.btn_panic)
        lay.addLayout(row)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)
        self._refresh()

    def _funds_page(self) -> QWidget:
        page = QWidget(); pl = QVBoxLayout(page); pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(12)
        card = QFrame(); card.setObjectName("card")
        cl = QVBoxLayout(card); cl.setContentsMargins(18, 16, 18, 16); cl.setSpacing(12)
        head = QHBoxLayout()
        h = QLabel("Where your money is"); h.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        head.addWidget(h); head.addStretch()
        self.btn_reclaim = QPushButton("Reclaim locked")
        self.btn_reclaim.setObjectName("primary")
        self.btn_reclaim.setToolTip("Close empty accounts + burn worthless dust and close, "
                                    "refunding all locked rent to your wallet. Touch ID confirms.")
        self.btn_reclaim.clicked.connect(self._reclaim)
        head.addWidget(self.btn_reclaim)
        cl.addLayout(head)
        grid = QGridLayout(); grid.setSpacing(12)
        self.ftiles = {
            "total": StatTile("Total wallet value", "—"),
            "liquid": StatTile("Liquid SOL", "—"),
            "rent": StatTile("Locked rent", "—"),
            "dust": StatTile("Dust value", "—"),
            "fees": StatTile("Fees paid (live)", "—"),
            "accts": StatTile("Token accounts", "—"),
        }
        for i, t in enumerate(self.ftiles.values()):
            grid.addWidget(t, i // 3, i % 3)
        cl.addLayout(grid)
        note = QLabel("Liquid = spendable SOL. Locked rent = ~0.002 SOL held by each token "
                      "account (refundable when the account is closed). Dust = tiny leftover "
                      "tokens (rugged coins can't be sold, so their rent is stuck until burned). "
                      "Auto-reclaim runs every 30 min while live; 'Reclaim locked' does it now.")
        note.setWordWrap(True); note.setStyleSheet(f"color: {C.FAINT};")
        cl.addWidget(note)
        pl.addWidget(card); pl.addStretch()
        self._funds_ready.connect(self._on_funds)
        self._last_funds = 0.0
        return page

    def _reclaim(self) -> None:
        wc = getattr(self.controller, "wallet_ctrl", None) if self.controller else None
        if wc is None:
            QMessageBox.information(self, "Reclaim", "Live wallet not available.")
            return
        if QMessageBox.question(
                self, "Reclaim locked",
                "Close empty token accounts and burn worthless dust to refund all "
                "locked rent to your wallet?\n\nRefund goes only to your own wallet. "
                "Touch ID will confirm.") != QMessageBox.StandardButton.Yes:
            return
        wc.reclaim_rent()

    def _refresh_funds(self) -> None:
        import threading, time as _t
        wc = getattr(self.controller, "wallet_ctrl", None) if self.controller else None
        if wc is None:
            return
        self._last_funds = _t.monotonic()

        def _worker():
            fb = wc.funds_breakdown()
            fees = 0.0
            try:
                if self.conn is not None:
                    fees = self.conn.execute(
                        "SELECT COALESCE(SUM(fees_usd),0) f FROM trades WHERE mode='live'").fetchone()[0]
            except Exception:  # noqa: BLE001
                pass
            if fb is not None:
                fb = dict(fb); fb["fees_usd"] = fees
                self._funds_ready.emit(fb)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_funds(self, fb: dict) -> None:
        total = fb["liquid_usd"] + fb["rent_usd"] + fb["dust_usd"]
        self.ftiles["total"].set_value(f"${total:,.2f}")
        self.ftiles["liquid"].set_value(f"${fb['liquid_usd']:,.2f}")
        self.ftiles["rent"].set_value(f"${fb['rent_usd']:,.2f}", C.AMBER if fb["rent_usd"] > 0.5 else C.MUTED)
        self.ftiles["dust"].set_value(f"${fb['dust_usd']:,.2f}")
        self.ftiles["fees"].set_value(f"${fb.get('fees_usd', 0):,.2f}")
        self.ftiles["accts"].set_value(f"{fb['n_accounts']} ({fb['n_dust']} dust)")

    def _status_page(self) -> QWidget:
        page = QWidget(); pl = QVBoxLayout(page); pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(12)
        card = QFrame(); card.setObjectName("card")
        cl = QVBoxLayout(card); cl.setContentsMargins(18, 16, 18, 16); cl.setSpacing(12)
        chead = QHBoxLayout()
        h = QLabel("Engine"); h.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        chead.addWidget(h); chead.addStretch()
        self.btn_test = QPushButton("Run a test paper trade")
        self.btn_test.setObjectName("primary")
        self.btn_test.setToolTip("Forces a paper buy on a liquid token (BONK) so you can see a "
                                 "position appear. Paper only.")
        self.btn_test.clicked.connect(self._test_trade)
        chead.addWidget(self.btn_test)
        self.btn_test_sl = QPushButton("Test stop-loss")
        self.btn_test_sl.setToolTip("Opens a paper position then crashes it −17% so the −15% "
                                    "stop-loss fires — watch the balance and win rate drop. Paper only.")
        self.btn_test_sl.clicked.connect(self._test_sl)
        chead.addWidget(self.btn_test_sl)
        cl.addLayout(chead)
        grid = QGridLayout(); grid.setSpacing(12)
        self.tiles = {
            "status": StatTile("Engine status", "Stopped"),
            "wallets": StatTile("Wallets watched", "—"),
            "events": StatTile("Events received", "—"),
            "buys": StatTile("Buys detected", "—"),
            "uptime": StatTile("Uptime", "—"),
            "heartbeat": StatTile("Last heartbeat", "—"),
            "helius": StatTile("Helius latency", "—"),
            "jupiter": StatTile("Jupiter latency", "—"),
            "feed": StatTile("Feed last event", "—"),
        }
        for i, t in enumerate(self.tiles.values()):
            grid.addWidget(t, i // 4, i % 4)
        cl.addLayout(grid)
        note = QLabel("Real copy-trades are infrequent (a followed wallet must buy a token that "
                      "passes every filter). 'Events received' climbing = the live feed works; "
                      "use the test trade to see a position on demand. RPC latency + heartbeat "
                      "refresh ~every 30s while running. SOL gas balance and the trading-wallet "
                      "address arrive with the live wallet (Phase 6). Engine settings apply on "
                      "the next Start.")
        note.setWordWrap(True); note.setStyleSheet(f"color: {C.FAINT};")
        cl.addWidget(note)
        pl.addWidget(card)
        pl.addStretch()
        return page

    def _telegram_page(self) -> QWidget:
        page = QWidget(); pl = QVBoxLayout(page); pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(12)
        tgcard = QFrame(); tgcard.setObjectName("card")
        tl = QVBoxLayout(tgcard); tl.setContentsMargins(18, 16, 18, 16); tl.setSpacing(12)
        th = QLabel("Telegram bot"); th.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        tl.addWidget(th)
        tgrid = QGridLayout(); tgrid.setSpacing(12)
        self.tg_tiles = {
            "status": StatTile("Bot status", "Off"),
            "bot": StatTile("Bot", "—"),
            "chat": StatTile("Linked chat", "—"),
            "sent": StatTile("Messages sent", "—"),
            "recv": StatTile("Commands received", "—"),
            "last": StatTile("Last activity", "—"),
        }
        for i, t in enumerate(self.tg_tiles.values()):
            tgrid.addWidget(t, i // 3, i % 3)
        tl.addLayout(tgrid)
        tnote = QLabel("Set the token and link your account in Settings → Telegram. "
                       "The bot only obeys your linked chat.")
        tnote.setWordWrap(True); tnote.setStyleSheet(f"color: {C.FAINT};")
        tl.addWidget(tnote)
        pl.addWidget(tgcard)
        pl.addStretch()
        return page

    def _metamask_page(self) -> QWidget:
        page = QWidget(); pl = QVBoxLayout(page); pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(12)
        wcard = QFrame(); wcard.setObjectName("card")
        wl = QVBoxLayout(wcard); wl.setContentsMargins(18, 16, 18, 16); wl.setSpacing(12)
        wh = QLabel("Wallets"); wh.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        wl.addWidget(wh)
        wgrid = QGridLayout(); wgrid.setSpacing(12)
        self.w_tiles = {
            "trading": StatTile("Trading wallet (SOL)", "—"),
            "trading_usd": StatTile("Trading wallet (USD)", "—"),
            "home": StatTile("MetaMask home (SOL)", "—"),
            "home_usd": StatTile("MetaMask home (USD)", "—"),
        }
        for i, t in enumerate(self.w_tiles.values()):
            wgrid.addWidget(t, i // 2, i % 2)
        wl.addLayout(wgrid)
        wnote = QLabel("The trading wallet is Hype's hot wallet; the MetaMask home wallet is the "
                       "only place Hype can pay out to. Balances refresh ~every 15s.")
        wnote.setWordWrap(True); wnote.setStyleSheet(f"color: {C.FAINT};")
        wl.addWidget(wnote)
        pl.addWidget(wcard)
        pl.addStretch()
        return page

    def _select(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        if idx == 1:
            self._refresh_funds()
        elif idx == 4:
            self.blocks_view.refresh()
        elif idx == 5:
            self.config_view.refresh()
        for i, b in enumerate(self.pills):
            b.setChecked(i == idx)
            if i == idx:
                b.setStyleSheet(f"QPushButton {{ background: {C.OCEAN_TINT}; color: {C.OCEAN_DARK}; "
                                f"border: none; border-radius: 9px; padding: 8px 16px; font-weight: 700; }}")
            else:
                b.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.MUTED}; border: none; "
                                f"border-radius: 9px; padding: 8px 16px; font-weight: 600; }}"
                                f"QPushButton:hover {{ color: {C.TEXT}; }}")

    def _refresh(self) -> None:
        # Re-pull the funds breakdown (~every 20s) while the Funds tab is open.
        if self.stack.currentIndex() == 1:
            import time as _t
            if _t.monotonic() - getattr(self, "_last_funds", 0.0) > 20:
                self._refresh_funds()
        s = self.controller.stats() if self.controller else {"running": False}
        running = s.get("running", False)
        self.tiles["status"].set_value("Running" if running else "Stopped",
                                       C.GREEN if running else C.MUTED)
        self.tiles["wallets"].set_value(str(s.get("wallets", "—")))
        self.tiles["events"].set_value(f"{s.get('events', 0):,}")
        self.tiles["buys"].set_value(f"{s.get('buys', 0):,}")
        self.tiles["uptime"].set_value(_fmt_uptime(s.get("uptime_seconds")))
        hb = s.get("heartbeat_age")
        hb_col = C.MUTED
        if running and hb is not None:
            hb_col = C.GREEN if hb < 15 else (C.AMBER if hb < 60 else C.RED)
        self.tiles["heartbeat"].set_value(_fmt_age(hb) if running else "—", hb_col)
        self.tiles["helius"].set_value(_fmt_ms(s.get("helius_ms")) if running else "—")
        self.tiles["jupiter"].set_value(_fmt_ms(s.get("jupiter_ms")) if running else "—")
        fi = s.get("feed_idle")
        fi_col = C.MUTED
        if running and fi is not None:
            fi_col = C.GREEN if fi < 30 else (C.AMBER if fi < 90 else C.RED)
        self.tiles["feed"].set_value(_fmt_age(fi) if (running and fi is not None) else "—", fi_col)
        self.btn_test.setEnabled(running)
        self.btn_test_sl.setEnabled(running)
        self._refresh_telegram()
        self._refresh_wallets()
        if self.stack.currentIndex() == 4:
            self.config_view.refresh()

    def _refresh_wallets(self) -> None:
        ws = self.wallet.stats() if self.wallet else {}
        def sol(v): return f"{v:.4f} SOL" if v is not None else "—"
        def usd(v): return f"${v:,.2f}" if v is not None else "—"
        if ws.get("exists"):
            self.w_tiles["trading"].set_value(sol(ws.get("sol_balance")))
            self.w_tiles["trading_usd"].set_value(usd(ws.get("usd_balance")))
        else:
            self.w_tiles["trading"].set_value("no wallet", C.MUTED)
            self.w_tiles["trading_usd"].set_value("—")
        if ws.get("home_address"):
            self.w_tiles["home"].set_value(sol(ws.get("home_balance")))
            self.w_tiles["home_usd"].set_value(usd(ws.get("home_usd_balance")))
        else:
            self.w_tiles["home"].set_value("not set", C.MUTED)
            self.w_tiles["home_usd"].set_value("—")

    def _refresh_telegram(self) -> None:
        ts = self.telegram.stats() if self.telegram else {"running": False}
        run, conn = ts.get("running"), ts.get("connected")
        if not run:
            self.tg_tiles["status"].set_value("Off", C.MUTED)
        elif conn:
            self.tg_tiles["status"].set_value("Connected", C.GREEN)
        else:
            self.tg_tiles["status"].set_value("Connecting…", C.AMBER)
        self.tg_tiles["bot"].set_value(f"@{ts['bot_username']}" if ts.get("bot_username") else "—")
        self.tg_tiles["chat"].set_value(str(ts["owner_chat_id"]) if ts.get("owner_chat_id") else "not linked")
        self.tg_tiles["sent"].set_value(f"{ts.get('messages_sent', 0):,}")
        self.tg_tiles["recv"].set_value(f"{ts.get('commands_received', 0):,}")
        la = ts.get("last_activity")
        import time as _t
        self.tg_tiles["last"].set_value(_fmt_age(_t.time() - la) if la else "—")

    def _test_trade(self) -> None:
        if not (self.controller and self.controller.request_test_buy()):
            QMessageBox.information(self, "Test trade", "Press Start first — the engine must be running.")

    def _test_sl(self) -> None:
        if not self.controller or not self.controller.running:
            QMessageBox.information(self, "Test stop-loss", "Press Start first — the engine must be running.")
            return
        ok = QMessageBox.question(
            self, "Test stop-loss",
            "This opens a PAPER position and crashes it −17% so the −15% stop-loss fires.\n\n"
            "It will add one real losing trade to this paper session — your balance and "
            "win rate will drop. No real money. Proceed?")
        if ok != QMessageBox.StandardButton.Yes:
            return
        if not self.controller.request_test_sl():
            QMessageBox.information(self, "Test stop-loss",
                                   "Only available in paper mode with the engine running.")

    def _panic(self) -> None:
        if self.wallet is None or not self.wallet.exists():
            QMessageBox.information(self, "Panic Drain", "No trading wallet to drain.")
            return
        ok = require_password_and_touchid(
            self, self.auth, "Panic Drain",
            "Sell everything to SOL and sweep it ALL to your home wallet? This is a real "
            "on-chain drain and cannot be undone.")
        if not ok:
            return
        self.wallet.panic_drain()   # loads the key (Touch ID) + runs off-thread
