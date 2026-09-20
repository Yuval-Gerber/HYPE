"""Main window — login → dashboard flow, collapsible left nav, and lock/blur.

Shell phase: the pages are styled placeholders. The next build fills them with
live data (Positions, Performance, Traders, Settings, Activity, System).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog, QDoubleSpinBox, QGraphicsBlurEffect, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import db
from ..config import HypeConfig, load_config, save_config
from ..engine.account import Account
from ..logging_setup import log_activity
from ..security.auth import AuthManager
from .balance_popover import BalancePopover
from .confirm_popover import ConfirmPopover
from .engine_controller import EngineController
from .lock import LockOverlay
from .login import LoginView
from .sidebar import CollapsibleSidebar
from .telegram_controller import TelegramController
from .wallet_controller import WalletController
from .tabs.activity import ActivityLogTab
from .tabs.performance import PerformanceTab
from .tabs.positions import PositionsTab
from .tabs.settings import SettingsTab
from .tabs.system import SystemTab
from .tabs.traders import TradersTab
from .theme import C
from .toast import ToastManager
from .topbar import TopBar

# (icon_name, label) per page.
TABS = [
    ("nav_positions", "Positions"),
    ("nav_performance", "Performance"),
    ("nav_traders", "Traders"),
    ("nav_settings", "Settings"),
    ("nav_activity", "Activity Log"),
    ("nav_system", "System"),
]
STATUS_NAMES = ["Birdeye", "Helius", "Jupiter", "Wallet", "Telegram"]


class _Placeholder(QWidget):
    """A styled empty-state for a page (replaced in the tabs build)."""

    def __init__(self, name: str) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addStretch()
        t = QLabel(name)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {C.OCEAN_DARK};")
        s = QLabel("Ready — live data arrives in the next build step.")
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s.setStyleSheet(f"color: {C.MUTED};")
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addStretch()


class HypeWindow(QMainWindow):
    def __init__(self, auth: AuthManager) -> None:
        super().__init__()
        self.auth = auth
        self.setWindowTitle("Hype")
        # Small floor so the window can shrink to fit compact screens (content
        # scrolls if narrower); actual size is fitted to the screen below.
        self.setMinimumSize(720, 500)
        self._fitted = False
        self._fit_to_screen()

        # Shared DB connection (UI reads; the engine uses its own writer
        # connection — WAL mode allows concurrent readers + one writer).
        self.conn = db.init_db()
        self.engine = EngineController()
        self.telegram = TelegramController()
        self.wallet_ctrl = WalletController()
        self.engine.wallet_ctrl = self.wallet_ctrl   # live keypair + balance for sizing

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.login = LoginView(auth)
        self.login.authenticated.connect(self._on_authenticated)
        self.stack.addWidget(self.login)

        self.dashboard = self._build_dashboard()
        self.stack.addWidget(self.dashboard)

        self.lock_overlay: LockOverlay | None = None
        self.toasts = ToastManager(self)
        self.stack.setCurrentWidget(self.login)

        # Engine controller (paper engine in a background thread).
        self.engine.stateChanged.connect(self._on_engine_state)
        self.engine.lightChanged.connect(self.sidebar.set_light)
        self.engine.notify.connect(lambda m, k: self.toasts.show(m, kind=k))
        # Telegram bot: drives the sidebar 'Telegram' light + toasts.
        self.telegram.lightChanged.connect(self.sidebar.set_light)
        self.telegram.notify.connect(lambda m, k: self.toasts.show(m, kind=k))
        # /start and /stop from Telegram run on the MAIN thread (queued signal →
        # QObject slot), so the engine is driven exactly like the top-bar buttons.
        self.telegram.engineStartRequested.connect(self._on_tg_start)
        self.telegram.engineStopRequested.connect(self._on_tg_stop)
        self.telegram.panicRequested.connect(self._on_tg_panic)
        self.engine.telegram = self.telegram   # engine forwards live alerts to Telegram
        # Trading wallet: sidebar 'Wallet' light + live balance in the top bar.
        self.wallet_ctrl.lightChanged.connect(self.sidebar.set_light)
        self.wallet_ctrl.notify.connect(lambda m, k: self.toasts.show(m, kind=k))
        self.wallet_ctrl.changed.connect(self._tick)
        # Command router: turns owner commands into replies (own DB conn + clients).
        from ..notify.commands import CommandRouter
        self._tg_router = CommandRouter(
            engine_stats=self.engine.stats,
            start_cb=self.telegram.engineStartRequested.emit,
            stop_cb=self.telegram.engineStopRequested.emit,
            panic_cb=self.telegram.panicRequested.emit,
        )
        self.telegram.set_command_provider(self._tg_router.handle)
        traders_tab = self.page_widgets.get("Traders")
        if traders_tab is not None:
            self.engine.tradersChanged.connect(traders_tab._refresh)

        # Periodic UI refresh: balances from the DB.
        self._paper_start = load_config().trading.paper_starting_balance_usd
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._tick)
        self._ui_timer.start(1000)

    # --- dashboard -----------------------------------------------------------

    def _build_dashboard(self) -> QWidget:
        root = QWidget()
        root.setObjectName("root")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        self.topbar = TopBar()
        self.topbar.run_changed.connect(self._on_run)
        self.topbar.lock_clicked.connect(self.lock)
        self.topbar.mode_changed.connect(self._on_mode_changed)
        self.topbar.balances.paperClicked.connect(self._edit_paper_balance)
        self.topbar.balances.liveClicked.connect(self._edit_live_balance)
        lay.addWidget(self.topbar)

        # Content row: collapsible sidebar + stacked pages.
        content = QHBoxLayout()
        content.setSpacing(12)

        self.sidebar = CollapsibleSidebar(TABS, STATUS_NAMES)
        self.sidebar.currentChanged.connect(lambda i: self.pages.setCurrentIndex(i))
        content.addWidget(self.sidebar)

        self.pages = QStackedWidget()
        self.page_widgets = {}
        for _, name in TABS:
            if name == "System":
                w = SystemTab(self.auth, self.engine, self.conn, telegram=self.telegram,
                              wallet=self.wallet_ctrl)
            elif name == "Settings":
                w = SettingsTab(self.auth, notify=lambda m: self.toasts.show(m, kind="ok"),
                                telegram=self.telegram, wallet=self.wallet_ctrl)
            elif name == "Activity Log":
                w = ActivityLogTab(self.conn)
            elif name == "Positions":
                w = PositionsTab(self.conn, self.engine)
            elif name == "Performance":
                w = PerformanceTab(self.conn, self.engine)
            elif name == "Traders":
                w = TradersTab(self.conn, self.engine)
            else:
                w = _Placeholder(name)
            self.page_widgets[name] = w
            self.pages.addWidget(w)
        content.addWidget(self.pages, 1)

        lay.addLayout(content)
        return root

    # --- auth flow -----------------------------------------------------------

    def _log(self, category: str, message: str, level: str = "INFO") -> None:
        try:
            log_activity(self.conn, category, message, level=level)
        except Exception:
            pass

    def _on_authenticated(self) -> None:
        self.stack.setCurrentWidget(self.dashboard)
        self._log("system", "Signed in to the dashboard")
        self.wallet_ctrl.start()   # sets the Wallet light + reads live balance
        # Auto-start the Telegram bot if the owner previously enabled it.
        try:
            if load_config().telegram.enabled:
                self.telegram.start()
        except Exception:
            pass

    def _on_mode_changed(self, mode: str) -> None:
        if mode == "live":
            if self.engine.running:
                self.toasts.show("Stop the engine before switching to Live.", kind="info")
                self.topbar.mode.set_key("paper"); return
            if not self.wallet_ctrl.exists():
                self.toasts.show("Create and fund a trading wallet first (Settings → Wallet).",
                                 kind="info")
                self.topbar.mode.set_key("paper"); return
            # Anchored confirmation card under the toggle (not an OS popup window).
            pop = ConfirmPopover(
                self, title="⚠ WARNING: real money involved",
                message="Live mode trades your real wallet funds automatically once you press "
                        "Start (Touch ID unlocks the key). Switch to Live?",
                yes_label="Yes, go Live", no_label="No",
                on_yes=lambda: self._set_mode("live"),
                on_no=lambda: self.topbar.mode.set_key("paper"))
            self._show_popover(pop, self.topbar.mode)
        else:
            self._set_mode("paper")

    def _set_mode(self, mode: str) -> None:
        try:
            cfg = load_config(); cfg.mode = mode; save_config(cfg)
        except Exception as e:  # noqa: BLE001
            self.toasts.show(f"Could not switch mode: {e}", kind="stop"); return
        self._log("mode", f"Switched to {mode.upper()}")
        perf = self.page_widgets.get("Performance")
        if perf is not None and hasattr(perf, "set_mode"):
            perf.set_mode(mode)   # Performance tab auto-follows the active mode
        self.toasts.show(f"{mode.capitalize()} mode", kind="ok" if mode == "paper" else "stop")

    def _on_run(self, state: str) -> None:
        if state == "start":
            self._log("system", "Start requested")
            self.engine.start()
        else:
            self._log("system", "Stop requested")
            self.engine.stop()
            self.toasts.show("Bot stopped", kind="stop")

    def _on_tg_start(self) -> None:
        """/start from Telegram — same path as the top-bar Start button."""
        self._log("system", "Start requested (Telegram)")
        self.engine.start()
        self.topbar.run.set_key("start")

    def _on_tg_stop(self) -> None:
        """/stop from Telegram — same path as the top-bar Stop button."""
        self._log("system", "Stop requested (Telegram)")
        self.engine.stop()
        self.topbar.run.set_key("stop")

    def _on_tg_panic(self) -> None:
        """/panic from Telegram. Reuses the live engine's already-unlocked key so
        it works remotely (no Touch ID). If the engine isn't live-running, the key
        isn't in memory — tell the owner to use the dashboard button."""
        kp = getattr(self.engine, "_live_keypair", None)
        if kp is None:
            self.telegram.alert("⛔ /panic needs the engine running in LIVE mode (key unlocked). "
                                "Use the dashboard Panic Drain button otherwise.")
            return
        self._log("panic", "Panic drain requested via Telegram", level="WARNING")
        self.wallet_ctrl.panic_drain_with_key(kp)

    def _on_engine_state(self, running: bool) -> None:
        # Keep the toggle in sync if the engine stops itself (e.g. on error).
        self.topbar.run.set_key("start" if running else "stop")

    def _tick(self) -> None:
        try:
            acct = Account(self.conn, "paper")
            sess = acct.active_session()
            live = self.wallet_ctrl.usd_balance() if hasattr(self, "wallet_ctrl") else None
            if sess is not None:
                eq = acct.equity_stored(sess["id"])
                self.topbar.set_balances(paper=eq.equity_usd, live=live)
            else:
                self.topbar.set_balances(paper=self._paper_start, live=live)
        except Exception:
            pass

    # --- balance editing -----------------------------------------------------

    def _edit_paper_balance(self) -> None:
        acct = Account(self.conn, "paper")
        sess = acct.active_session()
        current = acct.equity_stored(sess["id"]).equity_usd if sess else self._paper_start
        # "Reset" always restores the standard default ($1,000), independent of
        # whatever balance was last set.
        reset_default = HypeConfig().trading.paper_starting_balance_usd
        pop = BalancePopover(self, mode="paper", current=current, default=reset_default,
                             on_submit=self._apply_paper_balance)
        self._show_popover(pop, self.topbar.balances.paper)

    def _apply_paper_balance(self, amount: float) -> None:
        if self.engine.running:
            self.toasts.show("Stop the bot to reset the paper balance", kind="stop")
            return
        Account(self.conn, "paper").reset_session(amount)
        # Persist as the paper starting balance so Settings + System agree with
        # the top bar (reset_session only changes the live session row).
        try:
            cfg = load_config()
            cfg.trading.paper_starting_balance_usd = amount
            save_config(cfg)
            self._paper_start = amount
        except Exception:
            pass
        # Reload the Settings tab so its editor shows the new value immediately.
        settings = self.page_widgets.get("Settings")
        if settings is not None and hasattr(settings, "refresh_from_disk"):
            settings.refresh_from_disk()
        self._log("system", f"Paper balance set to ${amount:,.2f}")
        self.toasts.show(f"Paper balance set to ${amount:,.2f}", kind="ok")
        self._tick()

    def _edit_live_balance(self) -> None:
        """Click the live balance → send SOL to the MetaMask home wallet. The
        destination is the allowlisted home wallet only (read inside the
        controller); this dialog never accepts an arbitrary address."""
        if not self.wallet_ctrl.exists():
            QMessageBox.information(self, "Send to MetaMask",
                                    "Create and fund a trading wallet first (Settings → Wallet).")
            return
        if not load_config().payout.home_wallet_address:
            QMessageBox.information(self, "Send to MetaMask",
                                    "Set your MetaMask home wallet in Settings → Wallet first.")
            return
        ctx = {
            "sol_price": self.wallet_ctrl.sol_price_usd,
            "max_sol": self.wallet_ctrl.max_withdrawable_sol(),
            "gas_reserve_sol": load_config().trading.gas_reserve_sol,
        }
        pop = BalancePopover(self, mode="live", current=0.0, default=0.0,
                             on_submit=self.wallet_ctrl.send_to_home, live_ctx=ctx)
        self._show_popover(pop, self.topbar.balances.live)

    def _show_popover(self, pop, anchor) -> None:
        pop.adjustSize()
        gp = anchor.mapToGlobal(anchor.rect().bottomLeft())
        # Offset by the popover's transparent shadow margin so the card aligns
        # just under the anchor.
        pop.move(gp.x() - 14, gp.y() - 4)
        pop.show()

    # --- lock (blur the dashboard only; keep the auth card crisp) ------------

    def lock(self) -> None:
        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(16)
        self.dashboard.setGraphicsEffect(blur)
        if self.lock_overlay is None:
            # Parented to the WINDOW, not the dashboard, so the blur effect on
            # the dashboard does not blur the re-auth card.
            self.lock_overlay = LockOverlay(self.auth, self)
            self.lock_overlay.unlocked.connect(self._unlock)
        self.lock_overlay.setGeometry(self.centralWidget().geometry())
        self.lock_overlay.show()
        self.lock_overlay.raise_()
        self.lock_overlay.focus_password()
        self._log("system", "Dashboard locked")

    def _unlock(self) -> None:
        if self.lock_overlay:
            self.lock_overlay.hide()
        self.dashboard.setGraphicsEffect(None)
        self._log("system", "Dashboard unlocked")

    def resizeEvent(self, e):
        if self.lock_overlay and self.lock_overlay.isVisible():
            self.lock_overlay.setGeometry(self.centralWidget().geometry())
        self.toasts.reflow()
        super().resizeEvent(e)

    def closeEvent(self, e):
        try:
            self.engine.stop()
        except Exception:
            pass
        try:
            self.telegram.stop()
        except Exception:
            pass
        super().closeEvent(e)

    def _fit_to_screen(self) -> None:
        """Size the window to the CURRENT screen (capped at a comfortable max),
        centered. Runs on launch and whenever the window moves to a different
        monitor, so Hype always fits — on a small laptop or a big external."""
        if self.isFullScreen() or self.isMaximized():
            return   # the OS already fills the screen in these modes
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        a = screen.availableGeometry()
        w = max(720, min(1180, int(a.width() * 0.92)))
        h = max(500, min(760, int(a.height() * 0.92)))
        self.resize(w, h)
        self.move(a.x() + (a.width() - w) // 2, a.y() + (a.height() - h) // 2)

    def showEvent(self, e):
        super().showEvent(e)
        if not self._fitted:
            self._fitted = True
            self._fit_to_screen()
            wh = self.windowHandle()
            if wh is not None:   # re-fit when dragged to a different-size monitor
                wh.screenChanged.connect(lambda _s: self._fit_to_screen())
