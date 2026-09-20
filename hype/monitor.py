"""Live wallet monitor (§5.2) — Helius WebSocket logsSubscribe.

Subscribes to each followed wallet's transactions via the Helius enhanced
WebSocket (verified live: `logsSubscribe` with a `mentions` filter delivers
real-time events). On each event we fetch the signature, parse it via the
Enhanced Transactions API, and emit a BuySignal when the wallet bought a token.

Design:
  - one WebSocket connection, one subscription per wallet (mentions filter),
  - a parse queue + worker so the WS read loop never blocks on HTTP,
  - signature de-duplication (logs can repeat across commitment levels),
  - automatic reconnect with backoff.

This is the upgrade point for live mode (§5.2): swap logsSubscribe for
LaserStream gRPC / transactionSubscribe later without changing the buy-detection
or signal interface.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

import websockets

from .data.helius import HeliusClient
from .logging_setup import get_logger
from .models import BuySignal

# Callbacks may be sync or async.
BuyCallback = Callable[[BuySignal], Optional[Awaitable[None]]]
EventCallback = Callable[[str, dict], Optional[Awaitable[None]]]  # (wallet, parsed_tx)


class LiveMonitor:
    def __init__(
        self,
        helius: HeliusClient,
        wallets: Iterable[str],
        *,
        on_buy: BuyCallback,
        on_event: Optional[EventCallback] = None,
        commitment: str = "confirmed",
        max_seen: int = 5000,
        idle_timeout: float = 90.0,
    ) -> None:
        self.helius = helius
        self.wallets: List[str] = list(dict.fromkeys(wallets))  # de-dupe, keep order
        self.on_buy = on_buy
        self.on_event = on_event
        self.commitment = commitment
        # If NO message arrives for this long, the subscription is presumed dead
        # (Helius can silently stop delivering while TCP stays open) -> reconnect.
        self.idle_timeout = idle_timeout
        self._last_msg = time.monotonic()
        self.log = get_logger()

        self._sub_to_wallet: Dict[int, str] = {}   # subscription id -> wallet
        self._reqid_to_wallet: Dict[int, str] = {}  # request id -> wallet
        self._seen: "OrderedSet" = OrderedSet(max_seen)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self._ws = None                 # active websocket (for live re-subscribe)
        self._intentional_reconnect = False
        self.events_seen = 0
        self.buys_emitted = 0

    def stop(self) -> None:
        self._stop.set()

    def seconds_since_message(self) -> float:
        """Age of the last WebSocket message (any kind). A large value means the
        feed has gone quiet — surfaced in the System tab as 'Feed last event'."""
        return time.monotonic() - self._last_msg

    def update_wallets(self, wallets: Iterable[str]) -> None:
        """Swap the followed wallet set live. Applies on a quick reconnect
        (reuses the tested reconnect path), so the new set is subscribed within
        a second without restarting the engine. No-op if unchanged."""
        new = list(dict.fromkeys(wallets))
        if new == self.wallets:
            return
        self.wallets = new
        ws = self._ws
        if ws is not None:
            self._intentional_reconnect = True
            asyncio.create_task(ws.close())

    async def run(self) -> None:
        """Connect, subscribe, and process events until stop() (with reconnect)."""
        worker = asyncio.create_task(self._parse_worker())
        backoff = 1
        try:
            while not self._stop.is_set():
                try:
                    await self._session()
                    backoff = 1  # clean exit -> reset backoff
                except Exception as e:  # noqa: BLE001 - reconnect on any WS error
                    if self._stop.is_set():
                        break
                    if self._intentional_reconnect:
                        # We closed the socket on purpose to re-subscribe a new
                        # wallet set — reconnect immediately, no warning.
                        self._intentional_reconnect = False
                        backoff = 1
                        continue
                    self.log.warning("Monitor WS error: %s — reconnecting in %ds", e, backoff)
                    await asyncio.wait([asyncio.create_task(self._stop.wait())], timeout=backoff)
                    backoff = min(backoff * 2, 30)
        finally:
            worker.cancel()

    async def _session(self) -> None:
        async with websockets.connect(
            self.helius.ws_url, ping_interval=20, ping_timeout=20, max_size=8_000_000
        ) as ws:
            self._ws = ws
            try:
                await self._subscribe_all(ws)
                self._last_msg = time.monotonic()
                self.log.info("Monitor: watching %d wallets (commitment=%s).",
                              len(self.wallets), self.commitment)
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout)
                    except asyncio.TimeoutError:
                        # Silent subscription (no data at all) — bail out so run()
                        # reconnects + re-subscribes. This is the real fix for the
                        # "engine alive but feed deaf" hang.
                        self.log.warning(
                            "Monitor: no WS data for %.0fs — reconnecting (stale feed).",
                            self.idle_timeout)
                        return
                    self._last_msg = time.monotonic()
                    await self._handle_message(json.loads(raw))
            finally:
                self._ws = None

    async def _subscribe_all(self, ws) -> None:
        self._sub_to_wallet.clear()
        self._reqid_to_wallet.clear()
        for i, wallet in enumerate(self.wallets, start=1):
            self._reqid_to_wallet[i] = wallet
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": i,
                "method": "logsSubscribe",
                "params": [{"mentions": [wallet]}, {"commitment": self.commitment}],
            }))

    async def _handle_message(self, msg: dict) -> None:
        # Subscription confirmation: {"id": reqid, "result": subid}
        if "result" in msg and "id" in msg and isinstance(msg["result"], int):
            wallet = self._reqid_to_wallet.get(msg["id"])
            if wallet:
                self._sub_to_wallet[msg["result"]] = wallet
            return
        if msg.get("method") != "logsNotification":
            return
        params = msg.get("params", {})
        subid = params.get("subscription")
        wallet = self._sub_to_wallet.get(subid)
        value = params.get("result", {}).get("value", {})
        sig = value.get("signature")
        if not wallet or not sig or value.get("err") is not None:
            return
        if self._seen.add(sig):  # True if newly added
            self.events_seen += 1
            await self._queue.put((wallet, sig))

    async def _parse_worker(self) -> None:
        """Parse queued signatures off the hot path and emit buys."""
        while True:
            wallet, sig = await self._queue.get()
            try:
                parsed = await asyncio.to_thread(self.helius.parse_transactions, [sig])
                if not parsed:
                    continue
                tx = parsed[0]
                if self.on_event is not None:
                    await _maybe_await(self.on_event(wallet, tx))
                signal = HeliusClient.detect_buy(tx, wallet)
                if signal is not None:
                    self.buys_emitted += 1
                    await _maybe_await(self.on_buy(signal))
            except Exception as e:  # noqa: BLE001 - never let one tx kill the worker
                self.log.debug("parse/emit error for %s: %s", sig[:12], e)
            finally:
                self._queue.task_done()


async def _maybe_await(result) -> None:
    if asyncio.iscoroutine(result):
        await result


class OrderedSet:
    """Bounded insertion-ordered set for signature de-duplication."""

    def __init__(self, maxlen: int) -> None:
        self.maxlen = maxlen
        self._d: "dict[str, None]" = {}

    def add(self, key: str) -> bool:
        """Add key; return True if newly added, False if already present."""
        if key in self._d:
            return False
        self._d[key] = None
        if len(self._d) > self.maxlen:
            self._d.pop(next(iter(self._d)))
        return True
