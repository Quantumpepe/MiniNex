"""Exact Solana legacy-message compiler for the owner-bound THORChain SELL.

Only creates unsigned bytes. It cannot sign, broadcast, create a Privy
delegation or permit execution. Live workers must separately prove the current
owner grant, use a fresh blockhash, and claim the original order once.
"""
from __future__ import annotations

import base64
import hashlib
import struct

from .native_coin_balances import _BASE58, _decode_b58
from .native_sol_thor_transfer_instructions import (
    build_unsigned_sol_thor_source_instructions,
    _SOL_SYSTEM_PROGRAM, _SOL_MEMO_PROGRAM,
)


def _shortvec(n: int) -> bytes:
    if type(n) is not int or not 0 <= n <= 65535:
        raise ValueError("Invalid Solana vector length")
    out = bytearray()
    while True:
        item = n & 127
        n >>= 7
        out.append(item | (128 if n else 0))
        if not n:
            return bytes(out)


def _key(address: str) -> bytes:
    key = _decode_b58(address, _BASE58)
    if type(key) is not bytes or len(key) != 32:
        raise ValueError("Invalid Solana message public key")
    return key


def compile_original_unsigned_sol_message(
    quote: object, *, amount: str, now: int,
    source_account: object, network_terms: object,
) -> dict:
    """Produce exact message bytes for one signer without submit capability."""
    plan = build_unsigned_sol_thor_source_instructions(
        quote, amount=amount, now=now, source_account=source_account,
        network_terms=network_terms,
    )
    owner, vault = plan["source_wallet"], plan["inbound_vault"]
    transfer, memo = plan["instructions"]
    if (transfer.get("program_id") != _SOL_SYSTEM_PROGRAM
            or memo.get("program_id") != _SOL_MEMO_PROGRAM
            or transfer.get("accounts") != [
                {"pubkey": owner, "signer": True, "writable": True},
                {"pubkey": vault, "signer": False, "writable": True}]
            or memo.get("accounts") != [
                {"pubkey": owner, "signer": True, "writable": False}]):
        raise ValueError("Solana source account flags changed")
    transfer_data = base64.b64decode(transfer["data_base64"], validate=True)
    memo_data = base64.b64decode(memo["data_base64"], validate=True)
    if (len(transfer_data) != 12
            or struct.unpack("<IQ", transfer_data) != (
                2, int(plan["source_amount_lamports"]))
            or memo_data != quote["memo"].encode("utf-8")):
        raise ValueError("Solana transfer or refund memo changed")
    # Header: one writable signer, zero readonly signers and two readonly
    # unsigned program IDs. Two writable accounts are owner + vault.
    account_keys = [owner, vault, _SOL_SYSTEM_PROGRAM, _SOL_MEMO_PROGRAM]
    message = (
        bytes((1, 0, 2)) + _shortvec(4)
        + b"".join(_key(address) for address in account_keys)
        + _key(plan["recent_blockhash"]) + _shortvec(2)
        + bytes((2,)) + _shortvec(2) + bytes((0, 1))
        + _shortvec(len(transfer_data)) + transfer_data
        + bytes((3,)) + _shortvec(1) + bytes((0,))
        + _shortvec(len(memo_data)) + memo_data
    )
    # 1-byte signature count plus exactly one Ed25519 64-byte signature.
    # This is a Solana packet-size constraint, not a trading amount limit.
    if len(message) + 65 > 1232:
        raise ValueError("Solana message exceeds transaction packet size")
    return {
        "mode": "ORIGINAL_SOL_THORCHAIN_UNSIGNED_MESSAGE",
        "asset": "SOL", "side": "SELL",
        "source_wallet": owner, "inbound_vault": vault,
        "destination_wallet": plan["destination_wallet"],
        "source_amount_lamports": plan["source_amount_lamports"],
        "refund_memo": quote["memo"],
        "recent_blockhash": plan["recent_blockhash"],
        "last_valid_block_height": plan["last_valid_block_height"],
        "quote_expires_at": plan["quote_expires_at"],
        "unsigned_instructions_sha256": plan["unsigned_instructions_sha256"],
        "message_base64": base64.b64encode(message).decode("ascii"),
        "message_sha256": hashlib.sha256(message).hexdigest(),
        "message_compiled": True,
        "required_signers": [owner],
        "privy_signer_grant_verified": False,
        "wallet_signed": False,
        "ready_to_submit": False, "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
