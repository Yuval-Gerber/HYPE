"""Reclaim token-account rent (§6.2-safe).

Every time Hype buys a NEW token, Solana opens an Associated Token Account that
locks ~0.00204 SOL of rent. Jupiter's swap does not close that account on sell,
so the rent stays locked and the wallet slowly bleeds (measured: ~$3 stuck across
many trades). This module closes EMPTY token accounts and refunds the rent.

SECURITY: the rent destination is ALWAYS the wallet's own owner pubkey — funds
never leave the trading wallet, so this adds no outbound-transfer path (§6.2).
CloseAccount only works on a zero-balance account, so it can never touch tokens
that still hold value.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from ..logging_setup import get_logger

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
SOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
BURN_IX = 8                   # SPL Token instruction index for Burn
CLOSE_ACCOUNT_IX = 9          # SPL Token instruction index for CloseAccount
MAX_CLOSES_PER_TX = 18        # keep the tx comfortably under the size limit
MAX_BURN_CLOSE_PER_TX = 8     # burn+close = 2 ix each → 8 accts ≈ 16 ix per tx


class RentError(Exception):
    pass


@dataclass(slots=True)
class ReclaimResult:
    closed: int = 0
    reclaimed_lamports: int = 0
    signatures: list = field(default_factory=list)

    @property
    def reclaimed_sol(self) -> float:
        return self.reclaimed_lamports / 1e9


def _all_token_accounts(helius, owner: Pubkey):
    """Every token account → [{pubkey, program, lamports, mint, amount, decimals}]."""
    out = []
    for prog in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        res = helius._rpc("getTokenAccountsByOwner",
                          [str(owner), {"programId": str(prog)}, {"encoding": "jsonParsed"}])
        for v in (res or {}).get("value", []):
            info = v["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"]
            out.append({
                "pubkey": Pubkey.from_string(v["pubkey"]), "program": prog,
                "lamports": int(v["account"]["lamports"]), "mint": info["mint"],
                "amount": int(amt["amount"]), "decimals": int(amt["decimals"])})
    return out


def _empty_token_accounts(helius, owner: Pubkey):
    """Every zero-balance token account → [(pubkey, program, lamports)]."""
    return [(a["pubkey"], a["program"], a["lamports"])
            for a in _all_token_accounts(helius, owner) if a["amount"] == 0]


def _close_ix(account: Pubkey, owner: Pubkey, program: Pubkey) -> Instruction:
    # CloseAccount: [account_to_close (w), rent_destination (w), authority (signer)].
    # Destination == owner, so rent returns to the wallet itself (no external send).
    return Instruction(
        program_id=program,
        accounts=[
            AccountMeta(pubkey=account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
        ],
        data=bytes([CLOSE_ACCOUNT_IX]),
    )


def _burn_ix(account: Pubkey, mint: Pubkey, owner: Pubkey, amount: int,
             program: Pubkey) -> Instruction:
    # Burn: [account (w), mint (w), authority (signer)], data = 8 || u64 amount.
    return Instruction(
        program_id=program,
        accounts=[
            AccountMeta(pubkey=account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
        ],
        data=bytes([BURN_IX]) + int(amount).to_bytes(8, "little"),
    )


def _send_batch(helius, keypair: Keypair, ixs: list) -> str:
    bh = (helius._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
          or {}).get("value", {}).get("blockhash")
    if not bh:
        raise RentError("could not fetch a recent blockhash")
    msg = MessageV0.try_compile(keypair.pubkey(), ixs, [], Hash.from_string(bh))
    tx = VersionedTransaction(msg, [keypair])
    raw = base64.b64encode(bytes(tx)).decode()
    sim = helius._rpc("simulateTransaction",
                      [raw, {"encoding": "base64", "commitment": "confirmed",
                             "replaceRecentBlockhash": True}])
    err = (sim or {}).get("value", {}).get("err")
    if err is not None:
        raise RentError(f"simulation failed: {err}")
    sig = helius._rpc("sendTransaction",
                      [raw, {"encoding": "base64", "skipPreflight": False,
                             "maxRetries": 3, "preflightCommitment": "confirmed"}])
    if not isinstance(sig, str):
        raise RentError(f"unexpected send result: {sig}")
    return sig


def reclaim_locked(helius, keypair: Keypair, *, jupiter=None, sol_price_usd: float = 0.0,
                   dust_max_usd: float = 0.05, skip_mints=None) -> ReclaimResult:
    """Recover ALL locked rent: close empty accounts, AND for WORTHLESS dust
    (no sell route or value < dust_max_usd) burn the dust then close to reclaim its
    rent too. Dust that still has real value is LEFT ALONE (the caller sells it
    first). `skip_mints` (open-position mints) are never touched. All rent returns
    to the wallet itself. Needs `jupiter` to value dust; without it, only empties
    are closed."""
    log = get_logger()
    owner = keypair.pubkey()
    skip = set(skip_mints or ())
    accts = [a for a in _all_token_accounts(helius, owner) if a["mint"] not in skip]
    result = ReclaimResult()

    empties = [(a["pubkey"], a["program"], a["lamports"]) for a in accts if a["amount"] == 0]
    # decide which dust to burn: worthless (no route) or below the tiny threshold
    burnable = []
    if jupiter is not None:
        for a in accts:
            if a["amount"] <= 0:
                continue
            try:
                q = jupiter.quote(a["mint"], str(SOL_MINT), a["amount"], slippage_bps=800)
            except Exception:  # noqa: BLE001
                q = None
            out_lamports = int((q or {}).get("outAmount") or 0)
            value_usd = (out_lamports / 1e9) * sol_price_usd if sol_price_usd else 0.0
            if q is None or out_lamports == 0 or value_usd < dust_max_usd:
                burnable.append(a)   # rugged/worthless → burn to reclaim rent

    # 1) close empties (rent refund)
    for i in range(0, len(empties), MAX_CLOSES_PER_TX):
        batch = empties[i:i + MAX_CLOSES_PER_TX]
        sig = _send_batch(helius, keypair, [_close_ix(a, owner, p) for a, p, _ in batch])
        result.signatures.append(sig)
        result.closed += len(batch)
        result.reclaimed_lamports += sum(l for _, _, l in batch)
    # 2) burn worthless dust then close (rent refund)
    for i in range(0, len(burnable), MAX_BURN_CLOSE_PER_TX):
        batch = burnable[i:i + MAX_BURN_CLOSE_PER_TX]
        ixs = []
        for a in batch:
            ixs.append(_burn_ix(a["pubkey"], Pubkey.from_string(a["mint"]), owner,
                                a["amount"], a["program"]))
            ixs.append(_close_ix(a["pubkey"], owner, a["program"]))
        sig = _send_batch(helius, keypair, ixs)
        result.signatures.append(sig)
        result.closed += len(batch)
        result.reclaimed_lamports += sum(a["lamports"] for a in batch)
    if result.closed:
        log.info("reclaim_locked: closed %d accounts, reclaimed %.4f SOL",
                 result.closed, result.reclaimed_sol)
    return result


def funds_breakdown(helius, owner_addr: str, *, jupiter=None, sol_price_usd: float = 0.0) -> dict:
    """Live snapshot of where the wallet's value sits: liquid SOL, locked rent
    (empty + dust accounts), and dust token value. Read-only (no signing)."""
    owner = Pubkey.from_string(owner_addr)
    lam = (helius._rpc("getBalance", [owner_addr]) or {}).get("value", 0)
    accts = _all_token_accounts(helius, owner)
    rent_lamports = sum(a["lamports"] for a in accts)
    n_empty = sum(1 for a in accts if a["amount"] == 0)
    dust = [a for a in accts if a["amount"] > 0]
    dust_val = 0.0
    if jupiter is not None and sol_price_usd:
        for a in dust:
            try:
                q = jupiter.quote(a["mint"], str(SOL_MINT), a["amount"], slippage_bps=800)
                dust_val += (int((q or {}).get("outAmount") or 0) / 1e9) * sol_price_usd
            except Exception:  # noqa: BLE001
                pass
    return {
        "liquid_sol": lam / 1e9, "liquid_usd": (lam / 1e9) * sol_price_usd,
        "rent_sol": rent_lamports / 1e9, "rent_usd": (rent_lamports / 1e9) * sol_price_usd,
        "dust_usd": dust_val, "n_accounts": len(accts), "n_empty": n_empty, "n_dust": len(dust),
    }


def close_empty_token_accounts(helius, keypair: Keypair, *,
                               simulate_only: bool = False) -> ReclaimResult:
    """Close every empty token account, refunding its rent to the wallet. Batches
    into as few transactions as the size limit allows; simulates each before send."""
    log = get_logger()
    owner = keypair.pubkey()
    empties = _empty_token_accounts(helius, owner)
    result = ReclaimResult()
    if not empties:
        return result

    for i in range(0, len(empties), MAX_CLOSES_PER_TX):
        batch = empties[i:i + MAX_CLOSES_PER_TX]
        bh = (helius._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
              or {}).get("value", {}).get("blockhash")
        if not bh:
            raise RentError("could not fetch a recent blockhash")
        ixs = [_close_ix(acct, owner, prog) for acct, prog, _ in batch]
        msg = MessageV0.try_compile(owner, ixs, [], Hash.from_string(bh))
        tx = VersionedTransaction(msg, [keypair])
        raw = base64.b64encode(bytes(tx)).decode()

        sim = helius._rpc("simulateTransaction",
                          [raw, {"encoding": "base64", "commitment": "confirmed",
                                 "replaceRecentBlockhash": True}])
        err = (sim or {}).get("value", {}).get("err")
        if err is not None:
            raise RentError(f"simulation failed: {err}")
        if simulate_only:
            result.closed += len(batch)
            result.reclaimed_lamports += sum(l for _, _, l in batch)
            continue

        sig = helius._rpc("sendTransaction",
                          [raw, {"encoding": "base64", "skipPreflight": False,
                                 "maxRetries": 3, "preflightCommitment": "confirmed"}])
        if not isinstance(sig, str):
            raise RentError(f"unexpected send result: {sig}")
        result.signatures.append(sig)
        result.closed += len(batch)
        result.reclaimed_lamports += sum(l for _, _, l in batch)
        log.info("reclaimed rent from %d token accounts (sig %s)", len(batch), sig)
    return result
