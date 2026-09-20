"""KeyVault — the single interface for all secret handling.

Every secret (API keys now; the trading wallet's private key in P6) flows
through this interface. The engine NEVER imports a concrete backend or an OS
secret API directly — it depends only on `KeyVault`. To move to Linux/cloud
(Phase 8), implement a new backend and swap it in `get_vault()`.

Threat model (§6.7): no system is 100% safe. This is *containment* —
  - secrets live in the OS secret store, never in code/env/logs/plaintext,
  - sensitive reads (the wallet key) are gated behind Touch ID / password,
  - there is no arbitrary-send path anywhere in the codebase.
"""

from __future__ import annotations

import abc
from typing import Optional


class AuthenticationError(RuntimeError):
    """Raised when a required Touch ID / password authentication fails."""


class KeyVault(abc.ABC):
    """Abstract secret store. Backends: macOS Keychain (now), server (Phase 8)."""

    # Canonical secret names. Centralized so there are no stray string keys.
    BIRDEYE_API_KEY = "birdeye_api_key"
    HELIUS_API_KEY = "helius_api_key"
    JUPITER_API_KEY = "jupiter_api_key"
    RUGCHECK_API_KEY = "rugcheck_api_key"
    TELEGRAM_BOT_TOKEN = "telegram_bot_token"
    # The trading wallet private key (added/used in P6). Always auth-gated.
    WALLET_PRIVATE_KEY = "wallet_private_key"
    # The trading wallet PUBLIC address — non-sensitive, shown freely (funding
    # QR, top-bar balance) without a Touch ID prompt. Stored so we never need the
    # private key just to display the address.
    WALLET_ADDRESS = "wallet_address"
    # Owner-auth verifiers (salted hashes, not the secrets themselves).
    OWNER_PASSWORD = "owner_password"
    OWNER_NICKNAME = "owner_nickname"

    # Names that ALWAYS require Touch ID / password on read.
    SENSITIVE = frozenset({WALLET_PRIVATE_KEY})

    @abc.abstractmethod
    def set_secret(self, name: str, value: str) -> None:
        """Store/overwrite a secret. Never logs the value."""

    @abc.abstractmethod
    def get_secret(self, name: str, *, reason: Optional[str] = None) -> Optional[str]:
        """Retrieve a secret, or None if absent.

        If `name` is in SENSITIVE, the backend MUST require Touch ID / password
        before returning the value, raising AuthenticationError on failure.
        """

    @abc.abstractmethod
    def delete_secret(self, name: str) -> None:
        """Remove a secret if present."""

    @abc.abstractmethod
    def has_secret(self, name: str) -> bool:
        """True if a secret is stored (no value returned, no auth prompt)."""

    @abc.abstractmethod
    def authenticate(self, reason: str) -> bool:
        """Prompt for Touch ID / password directly. Returns True on success.

        Used for gating actions (mode switch, payout-address change, panic
        drain) independent of reading a particular secret.
        """


_vault: Optional[KeyVault] = None


def get_vault() -> KeyVault:
    """Return the process-wide vault, selecting the backend for this platform.

    This is the single swap point for Phase 8 (Linux/cloud).
    """
    global _vault
    if _vault is None:
        import sys

        if sys.platform == "darwin":
            from .keychain_macos import MacKeychainVault

            _vault = MacKeychainVault()
        else:  # pragma: no cover - exercised on the cloud build (Phase 8)
            raise NotImplementedError(
                "No KeyVault backend for this platform yet. The Linux/cloud "
                "backend is built in Phase 8."
            )
    return _vault


def set_vault(vault: KeyVault) -> None:
    """Override the active vault (used by tests and Phase 8)."""
    global _vault
    _vault = vault


class InMemoryKeyVault(KeyVault):
    """Non-persistent vault for tests (and a reference for Phase 8 backends).

    `auth_result` controls what authenticate()/sensitive reads return, so tests
    can simulate Touch ID success/failure without a real prompt.
    """

    def __init__(self, auth_result: bool = True) -> None:
        self._store: dict[str, str] = {}
        self.auth_result = auth_result

    def set_secret(self, name: str, value: str) -> None:
        self._store[name] = value

    def get_secret(self, name: str, *, reason: Optional[str] = None) -> Optional[str]:
        if name in self.SENSITIVE and not self.authenticate(reason or f"read {name}"):
            raise AuthenticationError(f"auth required to read '{name}'")
        return self._store.get(name)

    def delete_secret(self, name: str) -> None:
        self._store.pop(name, None)

    def has_secret(self, name: str) -> bool:
        return name in self._store

    def authenticate(self, reason: str) -> bool:
        return self.auth_result
