"""Original BTC/SOL Privy offline-sign RPC envelopes; never submitted.

Only validates already prepared original owner payloads. Actual sender must
independently prove current Privy user permission and journal claim first.
BTC: one raw secp256k1_sign digest per BIP143 input.
SOL: unsigned serialized transaction via signTransaction, never send.
XRP: cryptographic signing is implemented separately; Privy does not document
a native 'xrpl' wallet API, and no unverified RPC is fabricated here.
"""
from __future__ import annotations

import base64
import hashlib
import re

from .native_btc_segwit_sighash import build_original_btc_segwit_v0_sighashes
from .native_privy_pubkey_binding import verify_native_public_key_owner

_PRIVY_WALLET = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_NATIVE_INTENT = re.compile(r"^native_[0-9a-f]{64}$")


def _identity(*, wallet_id: object, intent_key: object) -> None:
    if (type(wallet_id) is not str
            or not _PRIVY_WALLET.fullmatch(wallet_id)
            or type(intent_key) is not str
            or not _NATIVE_INTENT.fullmatch(intent_key)):
        raise ValueError("Original native wallet/strategy order ID unavailable")


def preview_privy_btc_raw_sign_requests(
    psbt: object, *, original_utxos: object,
    owner_public_key_hex: object, wallet_id: str, intent_key: str,
) -> dict:
    """Return only raw BIP143 signature digests, not executed API requests."""
    _identity(wallet_id=wallet_id, intent_key=intent_key)
    context = build_original_btc_segwit_v0_sighashes(
        psbt, original_utxos=original_utxos,
    )
    proven = verify_native_public_key_owner(
        asset="BTC", owner=context["source_wallet"],
        public_key=owner_public_key_hex,
    )
    calls = []
    for entry in context["signature_digests"]:
        digest = entry["bip143_sighash_all_hex"]
        index = entry["input_index"]
        if (type(index) is not int or not 0 <= index < context["input_count"]
                or type(digest) is not str or len(digest) != 64):
            raise ValueError("Original Bitcoin BIP143 digest invalid")
        calls.append({
            "input_index": index,
            "prevout_txid": entry["prevout_txid"],
            "prevout_vout": entry["prevout_vout"],
            "method": "raw_sign",
            "params": {"hash": "0x" + digest},
            "idempotency_key": hashlib.sha256(
                (intent_key + ":BTC:" + str(index) + ":" + digest).encode(
                    "ascii")).hexdigest(),
        })
    if len({entry["input_index"] for entry in calls}) != len(calls):
        raise ValueError("Repeated original Bitcoin input signing position")
    return {
        "mode": "ORIGINAL_BTC_PRIVY_RPC_SIGNING_PREVIEW_ONLY",
        "asset": "BTC", "side": "SELL",
        "source_wallet": context["source_wallet"],
        "wallet_id": wallet_id, "original_intent_key": intent_key,
        "original_unsigned_txid": context["original_unsigned_txid"],
        "original_psbt_sha256": context["original_psbt_sha256"],
        "owner_public_key_sha256": proven["public_key_sha256"],
        "signature_request_count": len(calls),
        "unsigned_sign_requests": calls,
        "api_requests_performed": False,
        "real_privy_policy_verified": False,
        "original_order_claim_verified": False,
        "wallet_signed": False, "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }


def preview_privy_sol_sign_transaction_request(
    original_message: object, *, owner_public_key_hex: object,
    wallet_id: str, intent_key: str,
) -> dict:
    """Create serialized unsigned one-signer SOL tx for nonbroadcast sign RPC."""
    _identity(wallet_id=wallet_id, intent_key=intent_key)
    if (type(original_message) is not dict
            or original_message.get("mode") !=
                "ORIGINAL_SOL_THORCHAIN_UNSIGNED_MESSAGE"
            or original_message.get("asset") != "SOL"
            or original_message.get("side") != "SELL"
            or original_message.get("message_compiled") is not True
            or original_message.get("wallet_signed") is not False
            or original_message.get("privy_signer_grant_verified") is not False
            or original_message.get("ready_to_submit") is not False
            or original_message.get("source_transaction_sent") is not False
            or original_message.get("execution_enabled") is not False
            or original_message.get("can_submit") is not False):
        raise ValueError("Original Solana unsigned source manifest invalid")
    owner = original_message.get("source_wallet")
    proven = verify_native_public_key_owner(
        asset="SOL", owner=owner, public_key=owner_public_key_hex,
    )
    if original_message.get("required_signers") != [owner]:
        raise ValueError("Original Solana fee payer/signer authority changed")
    encoded = original_message.get("message_base64")
    if type(encoded) is not str:
        raise ValueError("Original Solana message bytes unavailable")
    try:
        message = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("Invalid original Solana message encoding") from None
    from .native_coin_balances import _decode_b58, _BASE58
    owner_bytes = _decode_b58(owner, _BASE58)
    if (len(message) < 135 or len(message) + 65 > 1232
            or hashlib.sha256(message).hexdigest() !=
                original_message.get("message_sha256")
            or message[:4] != bytes((1, 0, 2, 4))
            or message[4:36] != owner_bytes):
        raise ValueError("Original Solana message differs from wallet/checksum")
    empty_signed_tx = b"\x01" + bytes(64) + message
    return {
        "mode": "ORIGINAL_SOL_PRIVY_SIGN_TRANSACTION_PREVIEW_ONLY",
        "asset": "SOL", "side": "SELL",
        "source_wallet": owner, "wallet_id": wallet_id,
        "original_intent_key": intent_key,
        "original_message_sha256": original_message["message_sha256"],
        "owner_public_key_sha256": proven["public_key_sha256"],
        "method": "signTransaction",
        "params": {
            "transaction": base64.b64encode(empty_signed_tx).decode("ascii"),
            "encoding": "base64",
        },
        "idempotency_key": hashlib.sha256((
            intent_key + ":SOL:" + original_message["message_sha256"]
        ).encode("ascii")).hexdigest(),
        "signature_slot_empty": True,
        "api_requests_performed": False,
        "real_privy_policy_verified": False,
        "original_order_claim_verified": False,
        "wallet_signed": False, "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
