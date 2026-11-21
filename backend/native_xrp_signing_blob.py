"""Canonical XRPL single-signing payload for original Privy-owned XRP SELL.

Uses the official XRPL binarycodec to produce exact STX-prefixed signing
bytes for a genuine owner-bound, fully-canonical native Payment. Both Ed25519
and secp256k1 account public keys are bound to the original classic address.
No wallet signing, transaction submission, account secret or broadcast here.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re

from xrpl.core.addresscodec import encode_classic_address
from xrpl.core.binarycodec import encode_for_signing

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_SECP = (2, 3)
_STX = bytes.fromhex("53545800")


def _owner_pubkey(public_key_hex: object) -> tuple[bytes, str]:
    if (type(public_key_hex) is not str
            or len(public_key_hex) != 66
            or not _HEX.fullmatch(public_key_hex)):
        raise ValueError("Original XRPL owner public key unavailable")
    key = bytes.fromhex(public_key_hex)
    if key[0] == 0xed:
        algorithm = "ed25519"
    elif key[0] in _SECP:
        algorithm = "secp256k1"
    else:
        raise ValueError("Original XRPL public key curve unsupported")
    digest = hashlib.new("ripemd160", hashlib.sha256(key).digest()).digest()
    return key, encode_classic_address(digest)


def encode_original_xrp_privy_signing_blob(
    unsigned: object, *, original_owner_public_key_hex: object,
    now: int,
) -> dict:
    """Reject an unbound source/payment before preparing any raw signature."""
    if (type(unsigned) is not dict
            or unsigned.get("mode") != "ORIGINAL_XRP_THORCHAIN_UNSIGNED_PAYMENT"
            or unsigned.get("asset") != "XRP"
            or unsigned.get("side") != "SELL"
            or unsigned.get("original_quote_verified") is not True
            or unsigned.get("source_balance_and_reserve_checked") is not True
            or unsigned.get("fresh_privy_policy_verified") is not False
            or unsigned.get("wallet_signed") is not False
            or unsigned.get("ready_to_submit") is not False
            or unsigned.get("source_transaction_sent") is not False
            or unsigned.get("execution_enabled") is not False
            or unsigned.get("can_submit") is not False
            or type(now) is not int or now <= 0):
        raise ValueError("Original XRPL signing context invalid")
    key, address = _owner_pubkey(original_owner_public_key_hex)
    if address != unsigned.get("source_wallet"):
        raise ValueError("XRPL original owner public key does not match wallet")
    payment = unsigned.get("unsigned_payment")
    expected_fields = {
        "TransactionType", "Flags", "Account", "Destination", "Amount",
        "Fee", "Sequence", "LastLedgerSequence", "Memos",
    }
    if (type(payment) is not dict or set(payment) != expected_fields
            or payment.get("TransactionType") != "Payment"
            or payment.get("Flags") != 0x80000000
            or payment.get("Account") != address
            or payment.get("Destination") != unsigned.get("inbound_vault")
            or payment.get("Amount") != unsigned.get("source_amount_drops")
            or payment.get("Fee") != unsigned.get("network_fee_drops")
            or payment.get("Sequence") != unsigned.get("account_sequence")
            or type(payment.get("LastLedgerSequence")) is not int
            or type(unsigned.get("account_validated_ledger")) is not int
            or payment["LastLedgerSequence"] != unsigned["account_validated_ledger"] + 20
            or payment.get("Memos") is None):
        raise ValueError("Original XRPL payment terms changed")
    expiry = unsigned.get("quote_expires_at")
    reserve = unsigned.get("account_reserve_drops")
    if (type(expiry) is not str or type(reserve) is not str
            or not reserve.isascii() or not reserve.isdecimal()):
        raise ValueError("Original XRPL expiry or account reserve unavailable")
    try:
        end = datetime.fromisoformat(expiry)
    except ValueError:
        raise ValueError("Original XRP quote expiry malformed") from None
    if (end.tzinfo is None or not now < end.timestamp() <= now + 600):
        raise ValueError("Original XRP source quote expired")
    stable = "USDC"
    destination = unsigned.get("destination_wallet")
    digest = hashlib.sha256(json.dumps(
        [payment, stable, destination, int(end.timestamp()), int(reserve)],
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    if (unsigned.get("unsigned_payment_sha256") != digest
            or type(destination) is not str
            or not re.fullmatch(r"0x[a-fA-F0-9]{40}", destination)):
        raise ValueError("Original XRPL unsigned payment fingerprint changed")
    # Every signed field is from the immutable original Payment. The
    # signing public key is added only after matching its account address.
    canonical = {**payment, "SigningPubKey": key.hex().upper()}
    try:
        encoded = encode_for_signing(canonical)
        blob = bytes.fromhex(encoded)
    except Exception as error:
        # xrpl-py codec errors must not result in a guessed signing payload.
        raise ValueError("Canonical XRPL signing bytes unavailable") from error
    if not blob.startswith(_STX) or len(blob) < 100:
        raise ValueError("XRPL canonical single-sign prefix missing")
    return {
        "mode": "ORIGINAL_XRP_PRIVY_SIGNING_BLOB_READ_ONLY",
        "asset": "XRP", "side": "SELL",
        "source_wallet": address,
        "inbound_vault": unsigned["inbound_vault"],
        "destination_wallet": destination,
        "source_amount_drops": unsigned["source_amount_drops"],
        "network_fee_drops": unsigned["network_fee_drops"],
        "signer_algorithm": (
            "ed25519" if key[0] == 0xed else "secp256k1"),
        "original_public_key_hex": key.hex().upper(),
        "original_payment_sha256": digest,
        "signing_blob_hex": blob.hex().upper(),
        "signing_blob_sha512half_hex": hashlib.sha512(blob).digest()[:32].hex(),
        "signing_prefix_stx_verified": True,
        "owner_public_key_matches_wallet": True,
        "privy_remote_owner_policy_verified": False,
        "wallet_signed": False, "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
