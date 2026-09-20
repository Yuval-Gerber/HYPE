"""macOS Keychain backend for KeyVault.

Secrets are stored in the login Keychain via `keyring` under a single service
name. Sensitive reads (the wallet private key) are gated behind Touch ID /
password via the isolated `touchid` module.

SECURITY (§6.1): the private key is decrypted into memory only after authorized
access and is never written to code, env, logs, or plaintext files. This module
is the only place that maps secret names to Keychain entries.
"""

from __future__ import annotations

from typing import Optional

import keyring
from keyring.errors import PasswordDeleteError

from . import touchid
from .keyvault import AuthenticationError, KeyVault

# All Hype secrets live under one Keychain service. Each secret `name` becomes
# the account within that service.
SERVICE_NAME = "Hype"


class MacKeychainVault(KeyVault):
    def __init__(self, service: str = SERVICE_NAME) -> None:
        self.service = service

    # --- storage -------------------------------------------------------------

    def set_secret(self, name: str, value: str) -> None:
        keyring.set_password(self.service, name, value)

    def has_secret(self, name: str) -> bool:
        # Reads metadata only; no value is exposed and no prompt is shown.
        return keyring.get_password(self.service, name) is not None

    def delete_secret(self, name: str) -> None:
        try:
            keyring.delete_password(self.service, name)
        except PasswordDeleteError:
            pass  # already absent — idempotent

    def get_secret(self, name: str, *, reason: Optional[str] = None) -> Optional[str]:
        if name in self.SENSITIVE:
            prompt = reason or f"Hype needs to access '{name}'"
            if not self.authenticate(prompt):
                raise AuthenticationError(f"Touch ID / password required to read '{name}'")
        return keyring.get_password(self.service, name)

    # --- auth ----------------------------------------------------------------

    def authenticate(self, reason: str) -> bool:
        ok, err = touchid.authenticate(reason)
        if not ok and err:
            # Surface the reason to the caller's logs WITHOUT touching secrets.
            from ..logging_setup import get_logger

            get_logger().warning("Authentication failed: %s", err)
        return ok
