"""Hype desktop dashboard (PyQt6, Phase 5).

A native macOS app: launches to a login screen, then a light, tabbed dashboard.
The engine runs in a background thread; the UI reads the SQLite DB and reacts to
Qt signals. All OS-specific auth flows through hype.security (KeyVault/Touch ID).
"""
