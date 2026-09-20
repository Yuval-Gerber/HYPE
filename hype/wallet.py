"""Trading wallet — key lifecycle for Hype's Solana hot wallet (§3, §6, Phase 6).

All private-key handling flows through the KeyVault (Touch-ID gated on read).
This module NEVER writes the key to disk, logs, or config. The public address is
stored separately (non-sensitive) so the dashboard can show the funding address
and QR without a Touch ID prompt.

Key format: Phantom/Solflare-compatible — the 64-byte secret key, base58-encoded.
That means a Backup string from here imports directly into Phantom, and vice
versa. RPC (balance reads, later sending) reuses the existing Helius client, so
no extra Solana RPC dependency is pulled in.
"""

from __future__ import annotations

from typing import Optional

import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from .security.keyvault import KeyVault, get_vault

LAMPORTS_PER_SOL = 1_000_000_000


class WalletError(Exception):
    pass


class WalletExistsError(WalletError):
    """Refuse to overwrite an existing funded wallet."""


def parse_secret(secret: str) -> Keypair:
    """Parse a base58 64-byte secret (Phantom format) into a Keypair. Also
    accepts a JSON array like '[12,34,...]' (Solana CLI id.json format)."""
    secret = secret.strip()
    try:
        if secret.startswith("["):
            import json
            raw = bytes(json.loads(secret))
        else:
            raw = base58.b58decode(secret)
    except Exception as e:  # noqa: BLE001
        raise WalletError(f"could not decode secret key: {e}") from e
    if len(raw) != 64:
        raise WalletError(f"secret key must be 64 bytes, got {len(raw)}")
    try:
        return Keypair.from_bytes(raw)
    except Exception as e:  # noqa: BLE001
        raise WalletError(f"invalid secret key: {e}") from e


def is_valid_address(addr: str) -> bool:
    """True if `addr` is a syntactically valid Solana (Base58) public key."""
    try:
        Pubkey.from_string(addr.strip())
        return True
    except Exception:  # noqa: BLE001
        return False


class WalletManager:
    """Create / load / back up the trading wallet. Key never leaves the vault
    except through the explicit, auth-gated reveal()/backup path."""

    def __init__(self, vault: Optional[KeyVault] = None) -> None:
        self.vault = vault or get_vault()

    # --- lifecycle -----------------------------------------------------------

    def exists(self) -> bool:
        return self.vault.has_secret(KeyVault.WALLET_PRIVATE_KEY)

    def address(self) -> Optional[str]:
        """Public address, no auth prompt (reads the cached public value)."""
        return self.vault.get_secret(KeyVault.WALLET_ADDRESS)

    def create(self) -> str:
        """Generate a fresh wallet, store the key (auth-gated) + address. Refuses
        to overwrite an existing wallet so funds can't be orphaned. Returns the
        public address."""
        if self.exists():
            raise WalletExistsError(
                "A trading wallet already exists. Back it up and delete it before "
                "creating a new one (or drain it to your home wallet first).")
        kp = Keypair()
        self._store(kp)
        return str(kp.pubkey())

    def import_secret(self, secret: str) -> str:
        """Restore a wallet from a base58/JSON secret. Refuses to overwrite."""
        if self.exists():
            raise WalletExistsError("A wallet already exists — delete it first.")
        kp = parse_secret(secret)
        self._store(kp)
        return str(kp.pubkey())

    def delete(self) -> None:
        """Remove the wallet from the vault (used for regenerate / after drain).
        Caller is responsible for making sure funds are moved out first."""
        self.vault.delete_secret(KeyVault.WALLET_PRIVATE_KEY)
        self.vault.delete_secret(KeyVault.WALLET_ADDRESS)

    def _store(self, kp: Keypair) -> None:
        secret_b58 = base58.b58encode(bytes(kp)).decode()
        self.vault.set_secret(KeyVault.WALLET_PRIVATE_KEY, secret_b58)
        self.vault.set_secret(KeyVault.WALLET_ADDRESS, str(kp.pubkey()))

    # --- sensitive (auth-gated) ----------------------------------------------

    def load_keypair(self, *, reason: str = "Authorize wallet access") -> Keypair:
        """Decrypt the key into memory for signing. Touch ID / password gated.
        Only the LiveExecutor + Backup call this."""
        secret = self.vault.get_secret(KeyVault.WALLET_PRIVATE_KEY, reason=reason)
        if not secret:
            raise WalletError("no trading wallet configured")
        return parse_secret(secret)

    def reveal_backup(self, *, reason: str = "Reveal wallet backup key") -> str:
        """Return the base58 secret for the owner to back up (auth-gated). This
        string imports directly into Phantom/Solflare or Hype on another Mac."""
        secret = self.vault.get_secret(KeyVault.WALLET_PRIVATE_KEY, reason=reason)
        if not secret:
            raise WalletError("no trading wallet configured")
        return secret

    # --- balances (public reads via Helius) ----------------------------------

    def sol_balance(self, helius) -> float:
        """Native SOL balance of the trading wallet (SOL, not lamports)."""
        addr = self.address()
        if not addr:
            return 0.0
        return self.sol_balance_of(helius, addr)

    @staticmethod
    def sol_balance_of(helius, address: str) -> float:
        res = helius._rpc("getBalance", [address], retries=1)
        lamports = (res or {}).get("value", 0) if isinstance(res, dict) else 0
        return (lamports or 0) / LAMPORTS_PER_SOL
