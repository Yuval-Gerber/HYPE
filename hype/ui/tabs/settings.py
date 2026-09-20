"""Settings tab (§7.4 / §9) — every config value, grouped and live-editable.

Reads/writes config.toml via the pydantic models. The home-wallet address is
NOT a normal field: it's shown read-only and can only be changed through the
3-factor dialog (password + nickname + Touch ID, §6.3). Funding address + QR are
placeholders until the wallet exists (P6).
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QStackedWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from ... import secrets
from ...config import HypeConfig, load_config, save_config
from ...security.auth import AuthManager
from ...security.keyvault import KeyVault
from ...wallet import is_valid_address
from ..dialogs import HomeWalletChangeDialog
from ..theme import C
from ..widgets import ToggleSwitch


def _qr_pixmap(data: str, scale: int = 5, border: int = 4) -> QPixmap:
    """Render a QR code for `data` to a QPixmap using QPainter (no Pillow dep)."""
    import qrcode
    qr = qrcode.QRCode(border=border, box_size=1)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    px = QPixmap(n * scale, n * scale)
    px.fill(QColor("white"))
    p = QPainter(px)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, cell in enumerate(row):
            if cell:
                p.drawRect(x * scale, y * scale, scale, scale)
    p.end()
    return px

# (attr, label, kind) per group. kind drives the editor + value extraction.
GROUPS = [
    # Trading is split across three sub-tabs so no single page is cramped (all
    # point at the same `trading` config section).
    ("Sizing", "trading", [
        ("trading_allowance_usd", "Trading allowance (max Hype trades with; 0 = all balance)", "usd"),
        ("per_trade_pct", "Per-trade size (% of balance)", "pct"),
        ("per_trade_cap_usd", "Per-trade cap", "usd"),
        ("min_trade_usd", "Min trade size (skip below — gas)", "usd"),
        ("max_open_positions", "Max open positions", "int"),
        ("gas_reserve_sol", "Gas reserve", "sol"),
        ("paper_starting_balance_usd", "Paper starting balance", "usd"),
    ]),
    ("Exits", "trading", [
        ("take_profit_pct", "Take-profit ceiling", "pct"),
        ("trail_activate_pct", "Trailing: arm at profit % (0 = off)", "pct"),
        ("trail_gap_pct", "Trailing: sell if it drops % from peak", "pct"),
        ("stop_loss_pct", "Stop-loss", "pct"),
    ]),
    ("Execution", "trading", [
        ("slippage_bps", "Live slippage cap — buys", "bps"),
        ("exit_slippage_bps", "Live slippage — exits/sells", "bps"),
        ("max_buy_impact_pct", "Max buy price impact (skip thin pools)", "pct"),
        ("dynamic_slippage", "Dynamic slippage (Jupiter RTSE)", "bool"),
        ("use_sender", "Fast landing via Helius Sender", "bool"),
        ("jito_tip_lamports", "Sender Jito tip (lamports)", "int"),
        ("max_priority_lamports", "Live priority fee cap (lamports)", "int"),
        ("paper_slippage_bps", "Paper modeled slippage", "bps"),
        ("paper_fee_bps", "Paper modeled fee", "bps"),
    ]),
    ("Ranking", "ranker", [
        ("birdeye_rank_window_days", "Rank window (days)", "int"),
        ("wallet_min_trades", "Min trades to qualify", "int"),
        ("follow_top_n", "Follow top N wallets", "int"),
        ("wallet_max_idle_hours", "Drop wallets idle over (hours, 0=off)", "float"),
        ("rank_refresh_minutes", "Birdeye leaderboard refresh (minutes)", "int"),
        ("roster_refresh_minutes", "Active-roster refresh (minutes)", "int"),
        ("rank_pool_size", "Leaderboard scan size", "int"),
    ]),
    ("Safety filters", "filters", [
        ("mint_authority_revoked", "Require mint authority revoked", "bool"),
        ("freeze_authority_revoked", "Require freeze authority revoked", "bool"),
        ("min_liquidity_usd", "Min liquidity", "usd"),
        ("max_top10_holders_pct", "Max top-10 holder concentration (0 = off)", "pct"),
        ("sellability_sim", "Require sellability simulation", "bool"),
        ("sellability_notional_usd", "Sellability test size", "usd"),
        ("sellability_max_price_impact_pct", "Max sell price impact", "pct"),
        ("min_token_age_minutes", "Min token age (minutes)", "opt_int"),
        ("rugcheck_enabled", "Use RugCheck verdict", "bool"),
    ]),
    ("Investigation", "investigation", [
        ("grace_period_trades", "Grace period (trades)", "int"),
        ("invest_consecutive_losses", "Consecutive losses to investigate", "int"),
        ("invest_losses_gt_wins", "Investigate if losses > wins", "bool"),
        ("rug_loss_pct", "Rug: single loss % that benches a trader now", "pct"),
        ("invest_net_negative", "Investigate if copied P&L goes negative", "bool"),
        ("probation_window_trades", "Probation window (trades)", "int"),
        ("probation_losses_to_trip", "Losses in window to trip", "int"),
        ("auto_drop_ceiling", "Auto-drop ceiling (level; Off = never)", "opt_int"),
    ]),
    ("Payout", "payout", [
        ("enabled", "Auto-deposit enabled", "bool"),
        ("interval_hours", "Sweep interval (hours)", "int"),
        ("fixed_amount_usd", "Fixed daily amount", "opt_usd"),
        ("asset", "Payout asset", "asset"),
        ("min_working_balance_usd", "Min working balance", "usd"),
    ]),
    ("Engine", "engine", [
        ("position_poll_seconds", "Position poll interval (s)", "sec"),
        ("price_cache_ttl_seconds", "Price cache TTL (s)", "sec"),
        ("dedupe_same_token", "Skip if already holding token", "bool"),
        ("prevent_sleep", "Keep Mac awake (turn off if it never sleeps)", "bool"),
        ("gas_reserve_sol_price_usd", "SOL price for gas reserve (auto; fallback only)", "usd"),
    ]),
]


class SettingsTab(QWidget):
    def __init__(self, auth: AuthManager, notify: Optional[Callable[[str], None]] = None,
                 telegram=None, wallet=None) -> None:
        super().__init__()
        self.auth = auth
        self.notify = notify
        self.telegram = telegram
        self.wallet = wallet
        self.cfg: HypeConfig = load_config()
        # In-memory snapshot of the last applied state (open / last save).
        self._snapshot: HypeConfig = self.cfg.model_copy(deep=True)
        self.editors: list[tuple[str, str, QWidget, str]] = []
        self._build()

    # --- build ---------------------------------------------------------------

    SECTION_LABELS = ["Sizing", "Exits", "Execution", "Ranking", "Filters", "Investigation",
                      "Payout", "Engine", "Wallet", "Telegram"]

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Settings")
        title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {C.OCEAN_DARK};")
        header.addWidget(title)
        note = QLabel("Mode is set by the top-bar toggle · engine settings apply on the next Start")
        note.setStyleSheet(f"color: {C.MUTED};")
        header.addStretch()
        header.addWidget(note)
        outer.addLayout(header)

        # Section sub-tabs (pills) + stacked pages.
        self.stack = QStackedWidget()
        for _t, group_attr, fields in GROUPS:
            self.stack.addWidget(self._group_card(group_attr, fields))
        self.stack.addWidget(self._payout_destination_card())
        self.stack.addWidget(self._telegram_card())

        pillrow = QHBoxLayout()
        pillrow.setSpacing(6)
        self.pills: list[QPushButton] = []
        for i, label in enumerate(self.SECTION_LABELS):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self._select_section(idx))
            self.pills.append(b)
            pillrow.addWidget(b)
        pillrow.addStretch()
        outer.addLayout(pillrow)
        outer.addWidget(self.stack, 1)
        self._select_section(0)

        # Save bar
        bar = QHBoxLayout()
        bar.addStretch()
        discard = QPushButton("Discard changes")
        discard.setToolTip("Revert to the last saved settings")
        discard.clicked.connect(self._reload)
        restore = QPushButton("Restore defaults")
        restore.setToolTip("Set every value back to the standard default (then Save to keep)")
        restore.clicked.connect(self._reset)
        save = QPushButton("Save changes")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        bar.addWidget(discard)
        bar.addWidget(restore)
        bar.addWidget(save)
        outer.addLayout(bar)

    def _select_section(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        for i, b in enumerate(self.pills):
            b.setChecked(i == idx)
            if i == idx:
                b.setStyleSheet(
                    f"QPushButton {{ background: {C.OCEAN_TINT}; color: {C.OCEAN_DARK}; "
                    f"border: none; border-radius: 9px; padding: 8px 16px; font-weight: 700; }}")
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background: transparent; color: {C.MUTED}; border: none; "
                    f"border-radius: 9px; padding: 8px 16px; font-weight: 600; }}"
                    f"QPushButton:hover {{ color: {C.TEXT}; }}")

    def _group_card(self, group_attr: str, fields) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        group = getattr(self.cfg, group_attr)
        for attr, label, kind in fields:
            w = self._make_editor(kind, getattr(group, attr))
            self.editors.append((group_attr, attr, w, kind))
            lab = QLabel(label)
            lab.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
            form.addRow(lab, w)
        lay.addLayout(form)
        return card

    def _payout_destination_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 14, 18, 16)
        lay.setSpacing(10)

        # --- Trading wallet (hot wallet) ---
        t = QLabel("Trading wallet")
        t.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        lay.addWidget(t)
        self.tw_hint = QLabel()
        self.tw_hint.setWordWrap(True)
        self.tw_hint.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        lay.addWidget(self.tw_hint)

        arow = QHBoxLayout()
        self.tw_addr = QLineEdit(); self.tw_addr.setReadOnly(True)
        self.tw_addr.setPlaceholderText("No trading wallet yet")
        self.tw_copy = QPushButton("Copy"); self.tw_copy.clicked.connect(self._copy_tw_address)
        arow.addWidget(self.tw_addr, 1); arow.addWidget(self.tw_copy)
        lay.addLayout(arow)

        midrow = QHBoxLayout()
        self.tw_qr = QLabel(); self.tw_qr.setFixedSize(140, 140)
        self.tw_qr.setStyleSheet("background: transparent;")
        midrow.addWidget(self.tw_qr)
        self.tw_balance = QLabel("—")
        self.tw_balance.setStyleSheet(f"color: {C.TEXT}; background: transparent; font-weight: 700;")
        midrow.addWidget(self.tw_balance, 1, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(midrow)

        brow = QHBoxLayout()
        self.tw_create = QPushButton("Create wallet"); self.tw_create.setObjectName("primary")
        self.tw_create.clicked.connect(self._create_wallet)
        self.tw_backup = QPushButton("Back up"); self.tw_backup.clicked.connect(self._backup_wallet)
        self.tw_restore = QPushButton("Restore…"); self.tw_restore.clicked.connect(self._restore_wallet)
        self.tw_livetest = QPushButton("Test live buy ($1)")
        self.tw_livetest.setToolTip("Do ONE real ~$1 Jupiter swap (SOL→BONK) to prove live "
                                    "trading works on-chain. Real money · Touch ID confirms.")
        self.tw_livetest.clicked.connect(self._test_live_buy)
        self.tw_sellall = QPushButton("Sell to SOL")
        self.tw_sellall.setToolTip("Sell every token the trading wallet holds back to SOL "
                                   "(recovers a test buy). Real swap · Touch ID confirms.")
        self.tw_sellall.clicked.connect(self._sell_all)
        # (Rent reclaim lives in System → Funds, next to the live locked-money view.)
        brow.addWidget(self.tw_create); brow.addWidget(self.tw_backup)
        brow.addWidget(self.tw_restore); brow.addWidget(self.tw_livetest)
        brow.addWidget(self.tw_sellall); brow.addStretch()
        lay.addLayout(brow)

        sep = QLabel(""); sep.setFixedHeight(4); lay.addWidget(sep)

        # --- Home wallet (payout destination) ---
        h = QLabel("Payout destination")
        h.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        lay.addWidget(h)
        row = QHBoxLayout()
        lab = QLabel("Home wallet (3-factor to change)")
        lab.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        self.home_display = QLineEdit(self.cfg.payout.home_wallet_address or "")
        self.home_display.setReadOnly(True)
        self.home_display.setPlaceholderText("Not set")
        change = QPushButton("Change…")
        change.clicked.connect(self._change_home_wallet)
        row.addWidget(lab)
        row.addWidget(self.home_display, 1)
        row.addWidget(change)
        lay.addLayout(row)

        if self.wallet is not None:
            self.wallet.changed.connect(self._refresh_wallet_status)
        self._refresh_wallet_status()
        return card

    # --- trading-wallet actions ---------------------------------------------

    def _refresh_wallet_status(self) -> None:
        if self.wallet is None or not hasattr(self, "tw_addr"):
            return
        exists = self.wallet.exists()
        addr = self.wallet.address() or ""
        self.tw_addr.setText(addr)
        self.tw_create.setVisible(not exists)
        self.tw_backup.setVisible(exists)
        self.tw_livetest.setVisible(exists)
        self.tw_sellall.setVisible(exists)
        self.tw_copy.setEnabled(exists)
        if exists and addr:
            self.tw_qr.setPixmap(_qr_pixmap(addr).scaled(140, 140))
            self.tw_hint.setText("Send SOL to this address to fund Hype. Deposits are "
                                 "auto-detected. Back up the key before funding heavily.")
            s = self.wallet.stats()
            usd = s.get("usd_balance")
            usd_txt = f" (~${usd:,.2f})" if usd is not None else ""
            self.tw_balance.setText(f"Balance: {s['sol_balance']:.4f} SOL{usd_txt}")
        else:
            self.tw_qr.clear()
            self.tw_hint.setText("Create a Solana hot wallet for live trading. The private key "
                                 "is generated here and stored in the macOS Keychain (Touch ID "
                                 "gated) — it never leaves this Mac.")
            self.tw_balance.setText("—")

    def _test_live_buy(self) -> None:
        if self.wallet is None:
            return
        if QMessageBox.question(
                self, "Live test buy",
                "Spend ~$1 of SOL on a REAL Jupiter swap (BONK) to verify live trading works "
                "on-chain?\n\nThis is real money. Touch ID will confirm, then check Solscan.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.wallet.test_live_buy(1.0)

    def _sell_all(self) -> None:
        if self.wallet is None:
            return
        if QMessageBox.question(
                self, "Sell to SOL",
                "Sell every token the trading wallet holds back to SOL?\n\n"
                "This is a real on-chain swap. Touch ID will confirm.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.wallet.sell_all_to_sol()

    def _copy_tw_address(self) -> None:
        addr = self.wallet.address() if self.wallet else None
        if addr:
            QGuiApplication.clipboard().setText(addr)
            self._toast("Address copied")

    def _create_wallet(self) -> None:
        if self.wallet is None:
            return
        if QMessageBox.question(self, "Create trading wallet",
                                "Generate a new Solana hot wallet on this Mac?\n\nThe key is stored "
                                "in the Keychain (Touch ID gated). Back it up right after.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.wallet.create_wallet()
        self._refresh_wallet_status()

    def _backup_wallet(self) -> None:
        if self.wallet is None:
            return
        secret = self.wallet.reveal_backup()   # Touch ID prompt
        if not secret:
            return
        dlg = QDialog(self); dlg.setWindowTitle("Wallet backup key")
        v = QVBoxLayout(dlg)
        warn = QLabel("⚠️ Anyone with this key controls the wallet. Store it offline / in a "
                      "password manager. Never share it. This imports into Phantom or Hype on "
                      "another Mac.")
        warn.setWordWrap(True); warn.setStyleSheet(f"color: {C.RED};")
        v.addWidget(warn)
        box = QTextEdit(); box.setPlainText(secret); box.setReadOnly(True); box.setFixedHeight(80)
        v.addWidget(box)
        cp = QPushButton("Copy to clipboard")
        cp.clicked.connect(lambda: (QGuiApplication.clipboard().setText(secret), self._toast("Backup key copied")))
        v.addWidget(cp)
        done = QPushButton("Done"); done.setObjectName("primary"); done.clicked.connect(dlg.accept)
        v.addWidget(done)
        dlg.exec()

    def _restore_wallet(self) -> None:
        if self.wallet is None:
            return
        if self.wallet.exists():
            QMessageBox.information(self, "Restore", "A wallet already exists. Remove it first "
                                    "(after draining or backing up) to restore a different one.")
            return
        dlg = QDialog(self); dlg.setWindowTitle("Restore wallet")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("Paste your base58 secret key (Phantom export or a Hype backup):"))
        box = QTextEdit(); box.setFixedHeight(80); v.addWidget(box)
        row = QHBoxLayout(); row.addStretch()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(dlg.reject)
        ok = QPushButton("Restore"); ok.setObjectName("primary"); ok.clicked.connect(dlg.accept)
        row.addWidget(cancel); row.addWidget(ok); v.addLayout(row)
        if dlg.exec() and box.toPlainText().strip():
            self.wallet.restore_wallet(box.toPlainText().strip())
            self._refresh_wallet_status()

    def _telegram_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(12)

        t = QLabel("Telegram bot")
        t.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {C.TEXT};")
        lay.addWidget(t)
        hint = QLabel("Control Hype and get alerts from your phone. The token is stored in the "
                      "macOS Keychain — never in the config file. The bot obeys only your chat.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        lay.addWidget(hint)

        # Token row (secret) — stored straight to Keychain, not config.
        token_row = QHBoxLayout()
        tl = QLabel("Bot token")
        tl.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        self.tg_token = QLineEdit()
        self.tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.tg_token.setPlaceholderText(
            "saved ✓ — paste again only to change" if secrets.telegram_bot_token()
            else "paste the token from @BotFather")
        save_tok = QPushButton("Save token")
        save_tok.clicked.connect(self._save_telegram_token)
        token_row.addWidget(tl)
        token_row.addWidget(self.tg_token, 1)
        token_row.addWidget(save_tok)
        lay.addLayout(token_row)

        # Action buttons.
        btns = QHBoxLayout()
        self.tg_connect = QPushButton("Connect")
        self.tg_connect.setObjectName("primary")
        self.tg_connect.clicked.connect(self._toggle_telegram)
        link = QPushButton("Link my account")
        link.setToolTip("Arm linking, then message your bot — it captures your chat automatically.")
        link.clicked.connect(self._link_telegram)
        test = QPushButton("Send test message")
        test.clicked.connect(self._test_telegram)
        btns.addWidget(self.tg_connect)
        btns.addWidget(link)
        btns.addWidget(test)
        btns.addStretch()
        lay.addLayout(btns)

        # Live status line.
        self.tg_status = QLabel("—")
        self.tg_status.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
        lay.addWidget(self.tg_status)

        # Alert toggles — persisted via the normal Save (registered as editors).
        form = QFormLayout()
        form.setHorizontalSpacing(24); form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        for attr, label in (
            ("alert_trades", "Alert on trades (open/close)"),
            ("alert_daily", "Daily P&L summary"),
            ("alert_investigations", "Alert on investigations"),
            ("alert_errors", "Alert on errors"),
            ("alert_panic", "Alert on panic-drain"),
        ):
            w = self._make_editor("bool", getattr(self.cfg.telegram, attr))
            self.editors.append(("telegram", attr, w, "bool"))
            lab = QLabel(label); lab.setStyleSheet(f"color: {C.MUTED}; background: transparent;")
            form.addRow(lab, w)
        lay.addLayout(form)

        if self.telegram is not None:
            self.telegram.statsChanged.connect(self._refresh_telegram_status)
        self._refresh_telegram_status()
        return card

    def _refresh_telegram_status(self) -> None:
        if self.telegram is None or not hasattr(self, "tg_status"):
            return
        s = self.telegram.stats()
        if not s["running"]:
            txt, col = "● Off", C.MUTED
        elif not s["connected"]:
            txt, col = f"● Connecting… {s['last_error'] or ''}".strip(), C.AMBER
        else:
            who = f"@{s['bot_username']}" if s["bot_username"] else "connected"
            linked = f" · linked to chat {s['owner_chat_id']}" if s["owner_chat_id"] else " · not linked yet"
            armed = " · waiting for your message…" if s.get("link_armed") else ""
            txt, col = f"● {who}{linked}{armed}", C.GREEN
        self.tg_status.setText(txt)
        self.tg_status.setStyleSheet(f"color: {col}; background: transparent;")
        self.tg_connect.setText("Disconnect" if s["running"] else "Connect")

    def _save_telegram_token(self) -> None:
        tok = self.tg_token.text().strip()
        if not tok:
            self._toast("Paste a bot token first")
            return
        try:
            secrets.store_api_key(KeyVault.TELEGRAM_BOT_TOKEN, tok)
        except Exception as e:  # noqa: BLE001
            self._toast(f"Could not save token: {e}")
            return
        self.tg_token.clear()
        self.tg_token.setPlaceholderText("saved ✓ — paste again only to change")
        self._toast("Telegram token saved to Keychain")
        if self.telegram is not None and self.telegram.running:
            self.telegram.restart()

    def _toggle_telegram(self) -> None:
        if self.telegram is None:
            return
        if self.telegram.running:
            try:
                cfg = load_config(); cfg.telegram.enabled = False; save_config(cfg)
            except Exception:  # noqa: BLE001
                pass
            self.telegram.stop()
        else:
            try:
                cfg = load_config(); cfg.telegram.enabled = True; save_config(cfg)
            except Exception:  # noqa: BLE001
                pass
            self.telegram.start()
        self._refresh_telegram_status()

    def _link_telegram(self) -> None:
        if self.telegram is not None:
            self.telegram.begin_link()
            self._refresh_telegram_status()

    def _test_telegram(self) -> None:
        if self.telegram is not None:
            self.telegram.send_test()

    # --- editors -------------------------------------------------------------

    def _make_editor(self, kind: str, value) -> QWidget:
        w = self._build_editor(kind, value)
        # Numeric/select inputs get a consistent, readable height so digits are
        # never clipped and every settings tab looks the same (toggles keep theirs).
        if not isinstance(w, ToggleSwitch):
            w.setMinimumHeight(32)
            w.setMinimumWidth(150)
        return w

    def _build_editor(self, kind: str, value) -> QWidget:
        if kind == "bool":
            return ToggleSwitch(bool(value))
        if kind == "asset":
            cb = QComboBox(); cb.addItems(["USDC", "SOL"])
            cb.setCurrentText(value or "USDC")
            return cb
        if kind in ("int", "bps"):
            s = QSpinBox(); s.setRange(0, 1_000_000)
            if kind == "bps":
                s.setSuffix(" bps")
            s.setValue(int(value or 0))
            return s
        if kind == "opt_int":
            s = QSpinBox(); s.setRange(0, 1_000_000); s.setSpecialValueText("Off")
            s.setValue(int(value) if value else 0)
            return s
        # float-ish
        d = QDoubleSpinBox()
        if kind == "pct":
            d.setRange(-100, 100000); d.setDecimals(1); d.setSuffix(" %")
        elif kind == "usd":
            d.setRange(0, 1_000_000_000); d.setDecimals(2); d.setPrefix("$")
        elif kind == "sol":
            d.setRange(0, 100000); d.setDecimals(3); d.setSuffix(" SOL")
        elif kind == "sec":
            d.setRange(0.5, 3600); d.setDecimals(1); d.setSuffix(" s")
        elif kind == "opt_usd":
            d.setRange(0, 1_000_000_000); d.setDecimals(2); d.setPrefix("$")
            d.setSpecialValueText("Not set")
        d.setValue(float(value) if value is not None else 0.0)
        return d

    def _extract(self, w: QWidget, kind: str):
        if kind == "bool":
            return w.isChecked()
        if kind == "asset":
            return w.currentText()
        if kind in ("int", "bps"):
            return int(w.value())
        if kind == "opt_int":
            return None if w.value() == 0 else int(w.value())
        if kind == "opt_usd":
            return None if w.value() == 0 else float(w.value())
        return float(w.value())

    # --- actions -------------------------------------------------------------

    def _save(self) -> None:
        try:
            for group_attr, attr, w, kind in self.editors:
                setattr(getattr(self.cfg, group_attr), attr, self._extract(w, kind))
            save_config(self.cfg)  # leaves home_wallet_address untouched
            self._snapshot = self.cfg.model_copy(deep=True)
        except Exception as e:
            self._toast(f"Save failed: {e}")
            return
        self._toast("Settings saved")

    def _apply_to_editors(self) -> None:
        """Push the current self.cfg values into every editor widget."""
        for group_attr, attr, w, kind in self.editors:
            val = getattr(getattr(self.cfg, group_attr), attr)
            if kind == "bool":
                w.setChecked(bool(val))
            elif kind == "asset":
                w.setCurrentText(val or "USDC")
            elif kind == "opt_int":
                w.setValue(int(val) if val else 0)
            elif kind in ("int", "bps"):
                w.setValue(int(val or 0))
            elif kind == "opt_usd":
                w.setValue(float(val) if val else 0.0)
            else:
                w.setValue(float(val) if val is not None else 0.0)
        self.home_display.setText(self.cfg.payout.home_wallet_address or "")
        # Force the visible page to repaint immediately.
        cur = self.stack.currentWidget()
        if cur is not None:
            cur.update()

    def _reset(self) -> None:
        """Restore the §9 default values (not yet saved — click Save to keep).

        Preserves the home wallet so restoring defaults can't trigger the
        3-factor guard or wipe the payout destination.
        """
        try:
            defaults = HypeConfig()
            defaults.payout.home_wallet_address = self.cfg.payout.home_wallet_address
            self.cfg = defaults
            self._apply_to_editors()
        except Exception as e:
            self._toast(f"Restore failed: {e}")
            return
        self._toast("Defaults restored — click Save to keep")

    def refresh_from_disk(self) -> None:
        """Reload config from disk into every editor. Called when the config
        was changed elsewhere (e.g. setting the paper balance from the top bar)
        so the Settings fields stay in sync."""
        try:
            self.cfg = load_config()
            self._snapshot = self.cfg.model_copy(deep=True)
            self._apply_to_editors()
        except Exception:
            pass

    def _reload(self) -> None:
        """Discard unsaved edits — revert every field to the last applied
        state (when the tab opened, or the last Save)."""
        try:
            self.cfg = self._snapshot.model_copy(deep=True)
            self._apply_to_editors()
        except Exception as e:
            self._toast(f"Discard failed: {e}")
            return
        self._toast("Reverted to last saved")

    def _change_home_wallet(self) -> None:
        dlg = HomeWalletChangeDialog(self.auth, self.cfg, self)
        if dlg.exec() and dlg.new_address:
            self.home_display.setText(dlg.new_address)
            self._toast("Home wallet updated")

    def _toast(self, msg: str) -> None:
        if self.notify:
            self.notify(msg)
