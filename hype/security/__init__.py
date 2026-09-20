"""Security layer.

All OS-specific secret handling lives here, behind the `KeyVault` interface, so
the rest of the engine never imports macOS APIs directly. A Linux/cloud build
(Phase 8) swaps only the backend (e.g. a password-gated OS secret store) without
touching the engine.
"""
