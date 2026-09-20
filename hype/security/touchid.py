"""macOS Touch ID / password authentication (isolated).

This is the ONLY module that talks to LocalAuthentication. It is imported lazily
by the macOS KeyVault backend so that importing the engine on a non-mac platform
never pulls in pyobjc. Phase 8 swaps the backend and this module is simply
unused.

We use `LAPolicyDeviceOwnerAuthentication` so the system falls back to the
account password if biometrics are unavailable (matches §6: "Touch ID / password").
"""

from __future__ import annotations

import threading
from typing import Optional, Tuple

# LAPolicy values (from <LocalAuthentication/LAContext.h>):
#   0 = DeviceOwnerAuthenticationWithBiometrics (Touch ID only)
#   1 = DeviceOwnerAuthentication               (Touch ID OR password)  <- we use this
LA_POLICY_DEVICE_OWNER_AUTHENTICATION = 1


class TouchIDUnavailable(RuntimeError):
    """Raised when the platform cannot evaluate a Touch ID / password policy."""


def is_available() -> Tuple[bool, Optional[str]]:
    """Return (available, reason). Does NOT prompt the user."""
    try:
        import LocalAuthentication
    except Exception as e:  # pragma: no cover - non-mac
        return False, f"LocalAuthentication unavailable: {e}"

    ctx = LocalAuthentication.LAContext.alloc().init()
    ok, err = ctx.canEvaluatePolicy_error_(LA_POLICY_DEVICE_OWNER_AUTHENTICATION, None)
    if not ok:
        reason = str(err.localizedDescription()) if err is not None else "policy not available"
        return False, reason
    return True, None


def authenticate(reason: str, timeout: float = 60.0) -> Tuple[bool, Optional[str]]:
    """Prompt for Touch ID (or password). Blocks until the user responds.

    Returns (success, error_message). `reason` is shown in the system dialog.
    This requires an interactive GUI session — it cannot be satisfied headlessly.
    """
    try:
        import LocalAuthentication
    except Exception as e:  # pragma: no cover - non-mac
        raise TouchIDUnavailable(str(e))

    ctx = LocalAuthentication.LAContext.alloc().init()
    can, err = ctx.canEvaluatePolicy_error_(LA_POLICY_DEVICE_OWNER_AUTHENTICATION, None)
    if not can:
        msg = str(err.localizedDescription()) if err is not None else "policy not available"
        raise TouchIDUnavailable(msg)

    done = threading.Event()
    result = {"success": False, "error": None}

    def _reply(success, error):  # ObjC block: ^(BOOL, NSError*)
        result["success"] = bool(success)
        if error is not None:
            result["error"] = str(error.localizedDescription())
        done.set()

    ctx.evaluatePolicy_localizedReason_reply_(
        LA_POLICY_DEVICE_OWNER_AUTHENTICATION, reason, _reply
    )

    if not done.wait(timeout):
        return False, "authentication timed out"
    return result["success"], result["error"]
