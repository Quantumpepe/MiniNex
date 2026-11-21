"""Validate an already-signed original SOL transaction before any broadcast.

The backend may eventually receive a raw signed transaction from Privy's
solana_signTransaction RPC. This module checks the serialized one-signer
transaction against MiniNex's exact original compiled transfer + refund
memo, verifies the original owner's Ed25519 signature, and does nothing else.
No Privy call, policy grant, sendTransaction, blockchain broadcast or fee.
"""
from __future__ import annotations

import base64
import hashlib

from .native_sol_signed_wire import verify_original_sol_detached_signature


def inspect_original_sol_signed_transaction(
    original_message: object, *, signed_transaction_base64: object,
) -> dict:
    """Compare signed Solana legacy bytes to original immutable source plan."""
    if (type(original_message) is not dict
            or original_message.get("mode") !=
                "ORIGINAL_SOL_THORCHAIN_UNSIGNED_MESSAGE"
            or original_message.get("asset") != "SOL"
            or original_message.get("wallet_signed") is not False
            or original_message.get("can_submit") is not False
            or original_message.get("execution_enabled") is not False
            or type(signed_transaction_base64) is not str
            or len(signed_transaction_base64) > 2000):
        raise ValueError("Original Solana signed transaction review not permitted")
    try:
        raw = base64.b64decode(signed_transaction_base64, validate=True)
        unsigned = base64.b64decode(
            original_message["message_base64"], validate=True)
    except (KeyError, TypeError, ValueError, base64.binascii.Error):
        raise ValueError("Original Solana signing response malformed") from None
    if (len(raw) != len(unsigned) + 65 or len(raw) > 1232
            or raw[:1] != b"\x01"
            or raw[65:] != unsigned
            or len(unsigned) < 135
            or raw[1:65] == bytes(64)):
        raise ValueError("Privy Solana response changed the original source bytes")
    signature = base64.b64encode(raw[1:65]).decode("ascii")
    proven = verify_original_sol_detached_signature(
        original_message, owner_signature_base64=signature,
    )
    if (proven["wire_sha256"] != hashlib.sha256(raw).hexdigest()
            or proven["wire_base64"] != signed_transaction_base64
            or proven["source_wallet"] != original_message["source_wallet"]
            or proven["detached_ed25519_signature_valid"] is not True):
        raise ValueError("Original Solana signature and wire no longer agree")
    return {
        "mode": "PRIVY_SOL_ORIGINAL_SIGNED_TX_READBACK_ONLY",
        "asset": "SOL", "side": "SELL",
        "source_wallet": original_message["source_wallet"],
        "original_message_sha256": original_message["message_sha256"],
        "signed_wire_sha256": proven["wire_sha256"],
        "source_amount_lamports": original_message["source_amount_lamports"],
        "original_owner_signature_verified": True,
        "original_refund_memo_and_vault_verified": True,
        "original_message_unchanged": True,
        "privy_remote_policy_independently_verified": False,
        "original_order_send_claim_verified": False,
        "broadcast_authorized": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
