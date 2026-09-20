"""Deterministic, friendly auto-nicknames for trader wallets.

Every followed wallet gets a stable, human-readable handle derived from its
address (same address → same name), so the dashboard, activity log, and
performance leaders never show a raw 44-char key. The owner can override any
name (stored in traders.label); auto-names only ever fill a BLANK label, and a
rename is never overwritten.
"""

from __future__ import annotations

import hashlib
import sqlite3

_ANIMAL = [
    "Dog", "Cat", "Wolf", "Fox", "Bear", "Whale", "Shark", "Otter", "Hawk",
    "Bull", "Ape", "Lion", "Tiger", "Panda", "Koala", "Moose", "Bison", "Lynx",
    "Cobra", "Viper", "Falcon", "Eagle", "Owl", "Raven", "Crow", "Seal", "Orca",
    "Ram", "Goat", "Mule", "Elk", "Deer", "Hare", "Mole", "Toad", "Frog", "Newt",
    "Crab", "Squid", "Eel", "Bat", "Rat", "Mouse", "Boar", "Yak", "Llama",
    "Camel", "Gecko", "Iguana", "Sloth",
]


def generate(address: str) -> str:
    """A stable 'AnimalNN' handle for a wallet address (e.g. 'Dog09')."""
    h = hashlib.sha256(address.encode("utf-8")).digest()
    animal = _ANIMAL[h[0] % len(_ANIMAL)]
    num = ((h[1] << 8) | h[2]) % 100  # 00..99 — widens the space, cuts collisions
    return f"{animal}{num:02d}"


def ensure(conn: sqlite3.Connection) -> int:
    """Keep auto-nicknames current. Fills a blank label, and refreshes an
    auto-generated label (label_auto=1) whose value is stale — e.g. after the
    naming format changes. NEVER touches a name the owner set (label_auto=0).
    Returns how many labels were written."""
    try:
        rows = conn.execute("SELECT wallet_address, label, label_auto FROM traders").fetchall()
    except Exception:
        return 0
    n = 0
    for r in rows:
        addr = r["wallet_address"]
        want = generate(addr)
        cur = r["label"]
        auto = r["label_auto"] if "label_auto" in r.keys() else 1
        if not cur:
            conn.execute("UPDATE traders SET label=?, label_auto=1 WHERE wallet_address=?",
                         (want, addr))
            n += 1
        elif auto and cur != want:
            conn.execute("UPDATE traders SET label=? WHERE wallet_address=?", (want, addr))
            n += 1
    if n:
        conn.commit()
    return n
