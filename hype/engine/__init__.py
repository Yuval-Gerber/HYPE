"""Paper trading engine (Phase 4).

Ties the data layer (P2 signals) and safety filters (P3) into simulated trades:
executor, sizing, position manager (TP/SL), investigation engine, and equity
tracking. Identical logic will drive live mode (P6) — only the Executor swaps
from PaperExecutor to LiveExecutor (§11).
"""
