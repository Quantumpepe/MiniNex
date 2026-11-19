"""Verify a detached Solana owner signature and assemble exact wire bytes.

This module does NOT sign, ask Privy to sign, broadcast, or grant authority.
A signature alone never proves that a wallet owner consented to automated
trading; the eventual sender must independently check the current Privy
policy, original order claim and live wallet identity before transmission.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .native_coin_balances import _BASE58, _decode_b58


def verify_original_sol_detached_signature(
    unsigned: object, *, owner_signature_base64: str,
) -> dict:
    """Verify exact 64-byte Ed25519 signature over original message bytes.

    Only accepts the immutable original compiler output, not an arbitrary
    user-supplied Solana message. Returns a locally verified wire envelope
    for offline inspection, NOT an instruction to send it.
    """
    if (type(unsigned) is not dict
            or unsigned.get("mode") != "ORIGINAL_SOL_THORCHAIN_UNSIGNED_MESSAGE"
            or unsigned.get("asset") != "SOL"
            or unsigned.get("side") != "SELL"
            or unsigned.get("message_compiled") is not True
            or unsigned.get("wallet_signed") is not False
            or unsigned.get("privy_signer_grant_verified") is not False
            or unsigned.get("ready_to_submit") is not False
            or unsigned.get("source_transaction_sent") is not False
            or unsigned.get("execution_enabled") is not False
            or unsigned.get("can_submit") is not False
            or type(owner_signature_base64) is not str
            or len(owner_signature_base64) > 100):
        raise ValueError("Untrusted Solana owner signature context")
    owner = unsigned.get("source_wallet")
    if (unsigned.get("required_signers") != [owner]
            or type(owner) is not str
            or unsigned.get("inbound_vault") == owner
            or type(unsigned.get("message_base64")) is not str
            or type(unsigned.get("message_sha256")) is not str):
        raise ValueError("Unsigned Solana owner identity unavailable")
    key = _decode_b58(owner, _BASE58)
    if type(key) is not bytes or len(key) != 32:
        raise ValueError("Invalid original Solana signer")
    try:
        message = base64.b64decode(unsigned["message_base64"], validate=True)
        signature = base64.b64decode(owner_signature_base64, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("Invalid Solana signature encoding") from None
    if (len(signature) != 64 or len(message) < 135
            or len(message) + 65 > 1232
            or hashlib.sha256(message).hexdigest() != unsigned["message_sha256"]
            or message[:4] != bytes((1, 0, 2, 4))
            or message[4:36] != key):
        raise ValueError("Solana signed message does not match original owner")
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(signature, message)
    except (InvalidSignature, ValueError):
        raise ValueError("Solana detached owner signature invalid") from None
    wire = bytes((1,)) + signature + message
    return {
        "mode": "ORIGINAL_SOL_SIGNATURE_LOCALLY_VERIFIED_ONLY",
        "asset": "SOL", "side": "SELL",
        "source_wallet": owner,
        "message_sha256": unsigned["message_sha256"],
        "wire_sha256": hashlib.sha256(wire).hexdigest(),
        "wire_base64": base64.b64encode(wire).decode("ascii"),
        "detached_ed25519_signature_valid": True,
        "privy_policy_independently_verified": False,
        "original_order_claim_verified": False,
        "broadcast_authorized": False,
        "source_transaction_sent": False,
        "execution_enabled": False,
        "can_submit": False,
    }
