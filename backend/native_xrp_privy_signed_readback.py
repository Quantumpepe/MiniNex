"""Read back an existing original XRP Payment and its owner signature.

XRPL payment serialization and ED25519/secp256k1 proof are independent from
the Privy tenant's actual XRPL remote wallet support. A signed Payment may
be locally inspected, but does not prove remote authorization or permission
to broadcast and cannot cause a transfer in this module.
"""
from __future__ import annotations

import base64
import hashlib

from xrpl.core.binarycodec import decode, encode

from .native_xrp_signed_wire import verify_original_xrp_detached_signature


def inspect_original_xrp_signed_payment(
    original_unsigned: object, *, original_owner_public_key_hex: object,
    signed_payment_base64: object, now: int,
) -> dict:
    """Require exact canonical signed XRPL Payment and original quote terms."""
    if (type(original_unsigned) is not dict
            or original_unsigned.get("mode") !=
                "ORIGINAL_XRP_THORCHAIN_UNSIGNED_PAYMENT"
            or original_unsigned.get("wallet_signed") is not False
            or original_unsigned.get("can_submit") is not False
            or original_unsigned.get("execution_enabled") is not False
            or type(signed_payment_base64) is not str
            or len(signed_payment_base64) > 16000):
        raise ValueError("Original XRPL signed Payment review not permitted")
    try:
        wire = base64.b64decode(signed_payment_base64, validate=True)
        if not wire or len(wire) > 8000:
            raise ValueError("Original XRPL Payment wire malformed")
        decoded = decode(wire.hex().upper())
        reencoded = bytes.fromhex(encode(decoded))
    except Exception as error:
        raise ValueError("Cannot parse canonical original XRP Payment") from error
    if (type(decoded) is not dict
            or reencoded != wire
            or decoded.get("TransactionType") != "Payment"
            or set(decoded) != (
                set(original_unsigned.get("unsigned_payment", {}))
                | {"SigningPubKey", "TxnSignature"}
            )):
        raise ValueError("Original signed XRPL Payment contains altered fields")
    original = {k: v for k, v in decoded.items()
                if k not in ("SigningPubKey", "TxnSignature")}
    if original != original_unsigned["unsigned_payment"]:
        raise ValueError("Original XRPL source Payment altered after quote")
    if (type(original_owner_public_key_hex) is not str
            or decoded["SigningPubKey"].upper() !=
                original_owner_public_key_hex.upper()):
        raise ValueError("Original XRPL signer public key was substituted")
    signature = decoded["TxnSignature"]
    if type(signature) is not str:
        raise ValueError("Original XRP signer signature absent")
    proven = verify_original_xrp_detached_signature(
        original_unsigned,
        original_owner_public_key_hex=original_owner_public_key_hex,
        detached_signature_hex=signature, now=now,
    )
    if (base64.b64decode(proven["signed_wire_base64"], validate=True) != wire
            or proven["original_owner_signature_verified"] is not True):
        raise ValueError("Original XRP signed wire canonicalization failed")
    return {
        "mode": "ORIGINAL_XRP_SIGNED_PAYMENT_READBACK_ONLY",
        "asset": "XRP", "side": "SELL",
        "source_wallet": proven["source_wallet"],
        "original_unsigned_payment_sha256": (
            proven["original_unsigned_payment_sha256"]),
        "signed_transaction_hash": proven["signed_transaction_hash"],
        "signed_wire_sha256": hashlib.sha256(wire).hexdigest(),
        "original_owner_signature_verified": True,
        "original_payment_fields_unchanged": True,
        "privy_xrpl_wallet_signer_supported_and_verified": False,
        "original_order_send_claim_verified": False,
        "broadcast_authorized": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
