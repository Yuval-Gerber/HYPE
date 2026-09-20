"""Keep the machine awake while the engine runs (§12 portability).

Problem 1 root cause: a local Mac app gets suspended by system sleep / App Nap
when idle or backgrounded, which freezes the asyncio engine — no polling, no
TP/SL, no WebSocket. There is no code path that can recover a *suspended*
process, so the only real fix is to stop the OS from suspending it.

On macOS we hold a power assertion via the built-in `caffeinate` tool for as
long as the engine is running. This is deliberately isolated here so the Linux
cloud build (Phase 8) can no-op it — a server never sleeps.
"""

from __future__ import annotations

import subprocess
import sys

from .logging_setup import get_logger


class KeepAwake:
    """Prevent idle/system/display sleep while active. macOS-only; a no-op
    elsewhere. Safe to start()/stop() repeatedly."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self.log = get_logger()

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.active or sys.platform != "darwin":
            return
        try:
            # -i idle, -m disk, -s system (on AC). We deliberately DROP -d
            # (display): the engine only needs the SYSTEM awake, not the screen —
            # letting the display sleep saves power and avoids a display-assertion
            # quirk that could yank a native-fullscreen Space back to the desktop.
            # caffeinate exits on its own if this process dies (no leaked assertion).
            self._proc = subprocess.Popen(
                ["caffeinate", "-ims"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.log.info("KeepAwake: sleep prevented (caffeinate pid %s)", self._proc.pid)
        except Exception as e:  # noqa: BLE001 - never let this block the engine
            self.log.warning("KeepAwake unavailable: %s", e)
            self._proc = None

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
            self.log.info("KeepAwake: sleep assertion released")
