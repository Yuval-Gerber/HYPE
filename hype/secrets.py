"""Secrets handling — thin helpers over the KeyVault for API keys.

The engine asks for keys by purpose (e.g. `birdeye_api_key()`); the values live
only in the Keychain. API keys are not marked SENSITIVE, so they don't prompt
for Touch ID on every read (that would be unusable for a polling engine); the
wallet private key (P6) IS sensitive and always gated.

Nothing here ever logs or prints a secret value.
"""

from __future__ import annotations

from typing import Optional

from .security.keyvault import KeyVault, get_vault


def store_api_key(name: str, value: str, vault: Optional[KeyVault] = None) -> None:
    """Store an API key by its canonical KeyVault name."""
    (vault or get_vault()).set_secret(name, value)


def get_api_key(name: str, vault: Optional[KeyVault] = None) -> Optional[str]:
    """Fetch an API key, or None if not configured yet."""
    return (vault or get_vault()).get_secret(name)


def secrets_status(vault: Optional[KeyVault] = None) -> dict[str, bool]:
    """Which secrets are configured (booleans only — never the values).

    Used by the dashboard 'System' tab and the P1 startup status print.
    """
    v = vault or get_vault()
    names = [
        KeyVault.BIRDEYE_API_KEY,
        KeyVault.HELIUS_API_KEY,
        KeyVault.JUPITER_API_KEY,
        KeyVault.RUGCHECK_API_KEY,
        KeyVault.TELEGRAM_BOT_TOKEN,
        KeyVault.WALLET_PRIVATE_KEY,
    ]
    return {name: v.has_secret(name) for name in names}


# Convenience accessors used throughout later phases.
def birdeye_api_key(vault: Optional[KeyVault] = None) -> Optional[str]:
    return get_api_key(KeyVault.BIRDEYE_API_KEY, vault)


def helius_api_key(vault: Optional[KeyVault] = None) -> Optional[str]:
    return get_api_key(KeyVault.HELIUS_API_KEY, vault)


def jupiter_api_key(vault: Optional[KeyVault] = None) -> Optional[str]:
    return get_api_key(KeyVault.JUPITER_API_KEY, vault)


def rugcheck_api_key(vault: Optional[KeyVault] = None) -> Optional[str]:
    return get_api_key(KeyVault.RUGCHECK_API_KEY, vault)


def telegram_bot_token(vault: Optional[KeyVault] = None) -> Optional[str]:
    return get_api_key(KeyVault.TELEGRAM_BOT_TOKEN, vault)
