"""Shared catalog of trade-block reasons.

Every time Hype declines a buy it logs a reason: either a `filter` rejection
(one or more failed safety checks, §5.3) or a `skip` (dedupe, sizing, paused
trader). This module maps each logged reason key to a short, human explanation
so BOTH the System → Blocks tab and the Activity Log detail view can show the
same wording. Keep it as the single source of truth for block copy.
"""

from __future__ import annotations

# key -> (short title, one-line explanation)
REASONS: dict[str, tuple[str, str]] = {
    # --- safety-filter rejections (category = 'filter') ----------------------
    "rugcheck": (
        "RugCheck danger flag",
        "RugCheck reported a 'danger'-level risk for this token. Best-effort: it "
        "only blocks when RugCheck actively flags danger, and is skipped if "
        "RugCheck is unreachable."),
    "top10_concentration": (
        "Holders too concentrated",
        "The top-10 holders own more than the allowed maximum (default 65%). A "
        "few wallets could dump and crash the price."),
    "min_liquidity": (
        "Liquidity too low",
        "Pool liquidity is below the minimum (default $30,000) — too thin to "
        "enter and exit without heavy slippage."),
    "sellability": (
        "Not sellable / honeypot",
        "The reverse sell-quote failed or the price impact was too high — the "
        "token may be a honeypot you can buy but not sell."),
    "mint_authority_revoked": (
        "Mint authority active",
        "The mint authority is not revoked — the creator can print unlimited new "
        "tokens and dilute holders. Hype requires it revoked."),
    "freeze_authority_revoked": (
        "Freeze authority active",
        "The freeze authority is not revoked — the creator can freeze your "
        "balance so you can't sell. Hype requires it revoked."),
    "min_token_age": (
        "Token too new",
        "The token is younger than the minimum age set in Settings."),
    # --- skips (category = 'skip') -------------------------------------------
    "dedupe": (
        "Already holding",
        "Hype already has an open position in this token and won't double up."),
    "not_copyable": (
        "Trader paused",
        "The trigger trader is paused, under investigation, or dropped, so their "
        "buys aren't being copied right now."),
    "not_sized": (
        "Position not sized",
        "Sizing declined the trade — the per-trade cap, the max-open-positions "
        "limit, or too little available cash."),
    "filters_unavailable": (
        "Filters unavailable",
        "The safety filters couldn't be reached to vet the token, so Hype "
        "skipped it to stay safe."),
    "other_skip": (
        "Other skip",
        "The buy was skipped for another logged reason (see the message)."),
}

# Display order for the Blocks tab (documentation-style; the ones the owner
# cares about most first, then the rest).
ORDER: list[str] = [
    "rugcheck", "top10_concentration", "min_liquidity", "sellability",
    "mint_authority_revoked", "freeze_authority_revoked", "min_token_age",
    "dedupe", "not_copyable", "not_sized", "filters_unavailable", "other_skip",
]

# Reason keys that appear inside a 'filter' rejection message.
FILTER_KEYS = frozenset({
    "mint_authority_revoked", "freeze_authority_revoked", "min_liquidity",
    "top10_concentration", "sellability", "min_token_age", "rugcheck",
})


def classify(category: str, message: str) -> list[str]:
    """Return the reason key(s) for one blocked-buy log row.

    'filter' rows look like ``token rejected: a, b, c`` (one or more keys).
    'skip' rows carry a free-text reason we map to a single key.
    Returns [] for rows that aren't blocks.
    """
    if category == "filter":
        if ":" not in message:
            return []
        tail = message.split(":", 1)[1]
        return [t.strip() for t in tail.split(",") if t.strip() in FILTER_KEYS]
    if category == "skip":
        m = message.lower()
        if "already holding" in m:
            return ["dedupe"]
        if "not copyable" in m:
            return ["not_copyable"]
        if m.startswith("not sized"):
            return ["not_sized"]
        if "filters unavailable" in m:
            return ["filters_unavailable"]
        return ["other_skip"]
    return []
