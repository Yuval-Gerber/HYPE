"""Hype — personal automated Solana copy-trading bot.

This package is the core engine. It is written to be OS-agnostic: every
macOS-specific dependency (Keychain, Touch ID) is isolated behind the
`hype.security.keyvault.KeyVault` interface, so the engine can be lifted to a
Linux server later by swapping only the vault backend.

The design spec and roadmap live in SPEC.md.
"""

__version__ = "0.1.0"
