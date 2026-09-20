"""Native SOL transfer — the ONLY outbound-funds path in Hype (§6.2).

This builds, simulates, signs, and sends a plain SOL transfer. It is used ONLY
to move funds from the trading wallet to the owner's home wallet (payout / panic
/ manual withdraw). The destination is always passed in by the caller from the
allowlisted config value — there is no address-entry UI anywhere that reaches
here, so no input can redirect funds.

Simulate-before-send is mandatory: we never broadcast a transaction the RPC
rejects in simulation.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from ..logging_setup import get_logger
from ..wallet import LAMPORTS_PER_SOL, is_valid_address

# Leave a hair of SOL for the transfer fee itself when sweeping "everything".
TX_FEE_BUFFER_LAMPORTS = 10_000  # ~0.00001 SOL


class TransferError(Exception):
    pass


@dataclass(slots=True)
class TransferResult:
    signature: str
    lamports: int

    @property
    def sol(self) -> float:
        return self.lamports / LAMPORTS_PER_SOL


def _build(helius, keypair: Keypair, to_pubkey: Pubkey, lamports: int) -> VersionedTransaction:
    bh_resp = helius._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
    blockhash = (bh_resp or {}).get("value", {}).get("blockhash")
    if not blockhash:
        raise TransferError("could not fetch a recent blockhash")
    ix = transfer(TransferParams(
        from_pubkey=keypair.pubkey(), to_pubkey=to_pubkey, lamports=lamports))
    msg = MessageV0.try_compile(keypair.pubkey(), [ix], [], Hash.from_string(blockhash))
    return VersionedTransaction(msg, [keypair])


def send_sol(helius, keypair: Keypair, to_address: str, lamports: int,
             *, simulate_only: bool = False) -> TransferResult:
    """Transfer `lamports` SOL from `keypair` to `to_address`.

    Validates the destination, simulates, and (unless simulate_only) sends. The
    caller is responsible for having sourced `to_address` from the allowlisted
    home-wallet config."""
    log = get_logger()
    if lamports <= 0:
        raise TransferError("amount must be positive")
    if not is_valid_address(to_address):
        raise TransferError("invalid destination address")
    to_pubkey = Pubkey.from_string(to_address)
    if to_pubkey == keypair.pubkey():
        raise TransferError("destination equals the source wallet")

    tx = _build(helius, keypair, to_pubkey, lamports)
    raw_b64 = base64.b64encode(bytes(tx)).decode()

    sim = helius._rpc("simulateTransaction",
                      [raw_b64, {"encoding": "base64", "commitment": "confirmed",
                                 "replaceRecentBlockhash": True}])
    err = (sim or {}).get("value", {}).get("err")
    if err is not None:
        raise TransferError(f"simulation failed: {err}")
    if simulate_only:
        return TransferResult(signature="(simulated)", lamports=lamports)

    sig = helius._rpc("sendTransaction",
                      [raw_b64, {"encoding": "base64", "skipPreflight": False,
                                 "maxRetries": 3, "preflightCommitment": "confirmed"}])
    if not isinstance(sig, str):
        raise TransferError(f"unexpected send result: {sig}")
    log.info("SOL transfer sent: %s lamports -> %s (sig %s)", lamports, to_address, sig)
    return TransferResult(signature=sig, lamports=lamports)
