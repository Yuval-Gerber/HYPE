"""Owner authentication (§6.6, §6.3).

Provides the owner credential the dashboard's login screen + lock button and the
3-factor home-wallet change consume:

  - login        = password OR Touch ID
  - 3-factor     = password AND Touch ID AND secret nickname

Passwords and the nickname are NEVER stored in plaintext. We store only a salted
scrypt *verifier* in the KeyVault (Keychain), and compare in constant time.
Touch ID goes through the isolated `touchid` module, injectable for tests.

This layer is OS-agnostic except for the Touch ID function, which is swappable —
the cloud build (Phase 8) injects a password-only authenticator.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Callable, Optional, Tuple

from . import touchid
from .keyvault import KeyVault, get_vault

# scrypt parameters (interactive-login strength).
_N, _R, _P, _DKLEN, _SALT_LEN = 2 ** 14, 8, 1, 32, 16

TouchFn = Callable[[str], Tuple[bool, Optional[str]]]


def _hash(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)


def _make_verifier(secret: str) -> str:
    salt = os.urandom(_SALT_LEN)
    digest = _hash(secret, salt)
    return json.dumps({"salt": salt.hex(), "hash": digest.hex(),
                       "n": _N, "r": _R, "p": _P, "dklen": _DKLEN})


def _check_verifier(secret: str, blob: str) -> bool:
    try:
        d = json.loads(blob)
        salt = bytes.fromhex(d["salt"])
        expected = bytes.fromhex(d["hash"])
        actual = hashlib.scrypt(secret.encode("utf-8"), salt=salt,
                                n=d["n"], r=d["r"], p=d["p"], dklen=d["dklen"])
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


class AuthManager:
    def __init__(self, vault: Optional[KeyVault] = None, *, touch_fn: TouchFn = touchid.authenticate) -> None:
        self.vault = vault or get_vault()
        self.touch_fn = touch_fn

    # --- setup state ---------------------------------------------------------

    def is_password_set(self) -> bool:
        return self.vault.has_secret(KeyVault.OWNER_PASSWORD)

    def is_nickname_set(self) -> bool:
        return self.vault.has_secret(KeyVault.OWNER_NICKNAME)

    def is_setup(self) -> bool:
        return self.is_password_set() and self.is_nickname_set()

    # --- set credentials -----------------------------------------------------

    def set_password(self, password: str) -> None:
        if len(password) < 6:
            raise ValueError("password must be at least 6 characters")
        self.vault.set_secret(KeyVault.OWNER_PASSWORD, _make_verifier(password))

    def set_nickname(self, nickname: str) -> None:
        if not nickname.strip():
            raise ValueError("nickname cannot be empty")
        self.vault.set_secret(KeyVault.OWNER_NICKNAME, _make_verifier(nickname.strip()))

    # --- verify --------------------------------------------------------------

    def verify_password(self, password: str) -> bool:
        blob = self.vault.get_secret(KeyVault.OWNER_PASSWORD)
        return bool(blob) and _check_verifier(password, blob)

    def verify_nickname(self, nickname: str) -> bool:
        blob = self.vault.get_secret(KeyVault.OWNER_NICKNAME)
        return bool(blob) and _check_verifier(nickname.strip(), blob)

    def touch_id(self, reason: str) -> bool:
        ok, _ = self.touch_fn(reason)
        return ok

    # --- composite gates -----------------------------------------------------

    def login(self, *, password: Optional[str] = None, use_touch_id: bool = False,
              reason: str = "Unlock Hype") -> bool:
        """Login = password OR Touch ID (§6.6)."""
        if password is not None:
            return self.verify_password(password)
        if use_touch_id:
            return self.touch_id(reason)
        return False

    def verify_3factor(self, password: str, nickname: str, *,
                       reason: str = "Authorize home-wallet change") -> bool:
        """3-factor = password AND Touch ID AND nickname (§6.3).

        Evaluates all three explicitly (no short-circuit messaging differences),
        returns True only if every factor passes.
        """
        f_pw = self.verify_password(password)
        f_nick = self.verify_nickname(nickname)
        f_touch = self.touch_id(reason)
        return f_pw and f_nick and f_touch

    def change_home_wallet(self, cfg, new_address: str, *, password: str, nickname: str) -> bool:
        """Authorize + apply a home-wallet change via the 3-factor gate.

        Returns True if applied. The ONLY path that passes authorized=True to
        config.set_home_wallet_address — keeping the payout destination locked
        behind the full 3-factor check (§6.2/§6.3).
        """
        from .. import config as cfgmod

        if not self.verify_3factor(password, nickname,
                                   reason="Authorize changing the payout (home) wallet"):
            return False
        cfgmod.set_home_wallet_address(cfg, new_address, authorized=True)
        return True
