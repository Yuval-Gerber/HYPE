"""TelegramController — runs the Telegram bot in a background thread (§8).

Mirrors EngineController's shape: a daemon thread long-polls the Bot API for
owner commands, Qt signals talk back to the UI (the sidebar 'Telegram' light,
toasts, stats). Sending is done off-thread so the UI never blocks on the network.

Security (§8): the bot obeys ONLY the owner's chat id. Any message from another
chat is refused. The owner id is captured once via "Link my account" (the first
message received while link-mode is armed) and stored in config. The token lives
in the Keychain, never here.

Checkpoint 1 wires connection + link + test + basic replies. The rich commands
(/status, /pnl, /panic, …) attach in a later checkpoint via set_command_provider.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from .. import secrets
from ..config import load_config, save_config
from ..logging_setup import get_logger
from ..notify.telegram import TelegramClient, TelegramError

HELP_TEXT = (
    "<b>Hype bot</b> — you're the owner.\n\n"
    "📊 <b>/status</b> — full health: APIs, balance, mode, P&amp;L\n"
    "📈 <b>/pnl</b> — profit/loss summary\n"
    "📋 <b>/positions</b> — open positions\n"
    "▶️ <b>/start</b> — start the engine\n"
    "⏹ <b>/stop</b> — stop the engine\n"
    "🚨 <b>/panic</b> — drain everything to your home wallet\n"
    "🔀 <b>/mode</b> — show paper/live mode\n"
    "🏓 <b>/ping</b> — check the bot is alive\n"
    "❔ <b>/help</b> — this message"
)

# Command provider signature: fn(cmd: str, args: list[str]) -> Optional[str]
CommandProvider = Callable[[str, list], Optional[str]]


class TelegramController(QObject):
    lightChanged = pyqtSignal(str, str)   # ("Telegram", state)
    notify = pyqtSignal(str, str)         # (message, kind)
    linked = pyqtSignal(int)              # owner chat id captured
    statsChanged = pyqtSignal()
    engineStartRequested = pyqtSignal()   # /start — marshalled to the main thread
    engineStopRequested = pyqtSignal()    # /stop  — marshalled to the main thread
    panicRequested = pyqtSignal()         # /panic — marshalled to the main thread

    def __init__(self) -> None:
        super().__init__()
        self.log = get_logger()
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._link_mode = threading.Event()
        self._client: Optional[TelegramClient] = None
        self._offset: Optional[int] = None
        self._provider: Optional[CommandProvider] = None

        self.owner_chat_id: Optional[int] = None
        self.bot_username: Optional[str] = None
        self.connected = False
        # Stats (System tab).
        self.messages_sent = 0
        self.commands_received = 0
        self.last_activity_ts: Optional[float] = None
        self.last_error: Optional[str] = None

    # --- lifecycle -----------------------------------------------------------

    def set_command_provider(self, fn: CommandProvider) -> None:
        """Inject the engine-backed command handler (attached by the main window)."""
        self._provider = fn

    def start(self) -> bool:
        """Start polling. Returns False if there's no token yet."""
        if self.running:
            return True
        token = secrets.telegram_bot_token()
        if not token:
            self.lightChanged.emit("Telegram", "grey")
            self.notify.emit("Telegram: add your bot token in Settings first", "info")
            return False
        self.owner_chat_id = load_config().telegram.owner_chat_id
        self._stop.clear()
        self.running = True
        self._client = TelegramClient(token)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        self.connected = False
        self._link_mode.clear()
        self.lightChanged.emit("Telegram", "grey")
        self.statsChanged.emit()

    def restart(self) -> None:
        self.stop()
        # brief grace so the poll thread unwinds; start reads a fresh token.
        threading.Timer(0.3, self.start).start()

    # --- owner actions -------------------------------------------------------

    def begin_link(self) -> bool:
        """Arm link-mode: the next message the bot receives (from anyone) becomes
        the owner. Returns False if not running."""
        if not self.running:
            self.notify.emit("Telegram: enable the bot first", "info")
            return False
        self._link_mode.set()
        self.notify.emit("Telegram: now send any message to your bot to link", "info")
        return True

    def send_test(self) -> bool:
        if not (self.connected and self.owner_chat_id):
            self.notify.emit("Telegram: not linked yet — connect + link first", "info")
            return False
        self._send_async(self.owner_chat_id,
                         "✅ <b>Hype</b> test message — the bot is connected and this chat is the owner.")
        self.notify.emit("Telegram: test message sent", "ok")
        return True

    def alert(self, text: str) -> None:
        """Send an owner alert (used by engine hooks in a later checkpoint).
        No-op unless connected + linked + enabled."""
        if self.connected and self.owner_chat_id and load_config().telegram.enabled:
            self._send_async(self.owner_chat_id, text)

    def stats(self) -> dict:
        return {
            "running": self.running,
            "connected": self.connected,
            "bot_username": self.bot_username,
            "owner_chat_id": self.owner_chat_id,
            "messages_sent": self.messages_sent,
            "commands_received": self.commands_received,
            "last_activity": self.last_activity_ts,
            "last_error": self.last_error,
            "link_armed": self._link_mode.is_set(),
        }

    # --- background poller ---------------------------------------------------

    def _run(self) -> None:
        client = self._client
        assert client is not None
        try:
            me = client.get_me()
            self.bot_username = me.get("username")
            client.delete_webhook()   # getUpdates 409s if a webhook is set
            self.connected = True
            self.last_error = None
            self.lightChanged.emit("Telegram", "ok")
            self.notify.emit(f"Telegram connected: @{self.bot_username}", "ok")
            self.statsChanged.emit()
        except TelegramError as e:
            self.connected = False
            self.last_error = str(e)
            self.lightChanged.emit("Telegram", "down")
            self.notify.emit(f"Telegram connect failed: {e}", "stop")
            self.running = False
            self.statsChanged.emit()
            return

        while not self._stop.is_set():
            try:
                updates = client.get_updates(offset=self._offset, timeout=20)
            except TelegramError as e:
                if self._stop.is_set():
                    break
                self.last_error = str(e)
                self.lightChanged.emit("Telegram", "warn")
                self.statsChanged.emit()
                # brief backoff so a persistent error doesn't hot-loop
                self._stop.wait(3)
                continue
            if self.connected:
                self.lightChanged.emit("Telegram", "ok")
            for u in updates:
                self._offset = u["update_id"] + 1
                msg = u.get("message")
                if msg:
                    try:
                        self._handle_message(msg)
                    except Exception as ex:  # noqa: BLE001 - one bad msg mustn't kill the loop
                        self.log.debug("telegram msg error: %s", ex)
            if updates:
                self.statsChanged.emit()

    def _handle_message(self, msg: dict) -> None:
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None:
            return
        self.last_activity_ts = time.time()

        # Link mode: the first message binds this chat as the owner.
        if self._link_mode.is_set():
            self._link_mode.clear()
            self.owner_chat_id = int(chat_id)
            try:
                cfg = load_config()
                cfg.telegram.owner_chat_id = int(chat_id)
                cfg.telegram.enabled = True
                save_config(cfg)
            except Exception as e:  # noqa: BLE001
                self.log.warning("telegram: failed to persist owner id: %s", e)
            self._send(chat_id,
                       "🔗 <b>Linked.</b> This chat is now Hype's owner — only you can "
                       "control the bot. Send /help to see what's available.")
            self.linked.emit(int(chat_id))
            self.notify.emit("Telegram linked to this chat", "ok")
            self.statsChanged.emit()
            return

        # Owner-only auth (§8): reject everyone else.
        if self.owner_chat_id is None or int(chat_id) != int(self.owner_chat_id):
            self._send(chat_id, "⛔ This is a private Hype bot. You are not authorized.")
            return

        self.commands_received += 1
        self._dispatch(text, chat_id)

    def _dispatch(self, text: str, chat_id: int) -> None:
        parts = text.split()
        cmd = parts[0].lstrip("/").lower() if parts else ""
        args = parts[1:]
        if cmd in ("start", "help"):
            self._send(chat_id, HELP_TEXT)
            return
        if cmd == "ping":
            self._send(chat_id, "🏓 pong — Hype bot is alive.")
            return
        # Engine-backed commands (attached in a later checkpoint).
        if self._provider is not None:
            try:
                reply = self._provider(cmd, args)
            except Exception as e:  # noqa: BLE001
                reply = f"⚠️ command error: {e}"
            if reply:
                self._send(chat_id, reply)
                return
        self._send(chat_id, "That command isn't wired up yet. Send /help for what's available.")

    # --- sending -------------------------------------------------------------

    def _send(self, chat_id: int, text: str) -> None:
        """Synchronous send (called from the poll thread)."""
        if self._client is None:
            return
        try:
            self._client.send_message(chat_id, text)
            self.messages_sent += 1
        except TelegramError as e:
            self.last_error = str(e)
            self.log.debug("telegram send failed: %s", e)

    def _send_async(self, chat_id: int, text: str) -> None:
        """Fire-and-forget send from the UI thread (never blocks the UI)."""
        threading.Thread(target=self._send, args=(chat_id, text), daemon=True).start()
