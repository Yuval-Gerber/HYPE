"""Telegram Bot API client (§8) — thin wrapper over the HTTP API.

We deliberately call the Bot HTTP API directly with `requests` instead of pulling
in the heavy `python-telegram-bot` async framework: our needs are small (send
messages + long-poll for a handful of owner-only commands), the app already
depends on `requests`, and this keeps the bundle light and the Phase-8 cloud move
trivial. The API is stable and documented at https://core.telegram.org/bots/api.

This module is transport-only — no Qt, no engine. The controller drives it.
"""

from __future__ import annotations

from typing import Optional

import requests


class TelegramError(Exception):
    pass


class TelegramClient:
    """Stateless client bound to one bot token. Thread-safe for concurrent
    sends (each call is an independent HTTP request)."""

    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout = timeout
        self._session = requests.Session()

    def _call(self, method: str, *, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        url = f"{self._base}/{method}"
        try:
            resp = self._session.get(url, params=params or {}, timeout=timeout or self._timeout)
        except requests.RequestException as e:
            raise TelegramError(f"network error: {e}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise TelegramError(f"bad response ({resp.status_code})") from e
        if not data.get("ok"):
            # 401 = bad token; 409 = webhook conflict; etc.
            raise TelegramError(data.get("description") or f"HTTP {resp.status_code}")
        return data["result"]

    # --- API surface used by the controller ---------------------------------

    def get_me(self) -> dict:
        """Verify the token; returns the bot account (has 'username')."""
        return self._call("getMe")

    def delete_webhook(self) -> None:
        """Ensure no webhook is set, otherwise getUpdates returns 409. Safe to
        call even when none is set."""
        try:
            self._call("deleteWebhook", params={"drop_pending_updates": "false"})
        except TelegramError:
            pass

    def send_message(self, chat_id: int, text: str, *, parse_mode: str = "HTML",
                     disable_preview: bool = True) -> dict:
        return self._call("sendMessage", params={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true" if disable_preview else "false",
        })

    def get_updates(self, *, offset: Optional[int] = None, timeout: int = 20) -> list[dict]:
        """Long-poll for new updates. `offset` = last handled update_id + 1.
        The HTTP timeout is padded past the long-poll `timeout` so the server,
        not the socket, ends the wait."""
        params: dict = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params=params, timeout=timeout + 10)
