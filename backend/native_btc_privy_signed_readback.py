"""Verify an already signed SegWit Bitcoin owner source transaction offline.

Given original owner PSBT and confirmed original UTXOs, parse the returned
witness transaction and reject *any* modification to version, outpoints,
sequences, vault/change/memo VOUTs, locktime, witness pubkey or sighash type.
Every BIP143 SIGHASH_ALL DER signature is verified against its exact input.
No Privy API call, policy grant, chain submission or order mutation occurs.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from .native_btc_segwit_sighash import (
    build_original_btc_segwit_v0_sighashes, _compact, _hash256,
)
from .native_btc_signed_wire import _CURVE_ORDER
from .native_privy_pubkey_binding import verify_native_public_key_owner


def inspect_original_btc_signed_transaction(
    original_psbt: object, *, original_utxos: object,
    signed_wire_base64: object,
) -> dict:
    """Read-only check of Bitcoin witness bytes against original PSBT."""
    context = build_original_btc_segwit_v0_sighashes(
        original_psbt, original_utxos=original_utxos,
    )
    if type(signed_wire_base64) is not str or len(signed_wire_base64) > 1000000:
        raise ValueError("Original Bitcoin signed witness unavailable")
    try:
        signed = base64.b64decode(signed_wire_base64, validate=True)
        unsigned = bytes.fromhex(original_psbt["unsigned_tx_hex"])
    except (ValueError, base64.binascii.Error, TypeError, KeyError):
        raise ValueError("Bitcoin signed witness encoding malformed") from None
    if (len(signed) < len(unsigned) + 2 + 75 * context["input_count"]
            or len(signed) > len(unsigned) + 100 * context["input_count"] + 2
            or signed[:4] != unsigned[:4]
            or signed[4:6] != b"\x00\x01"
            or signed[6:len(unsigned)-2] != unsigned[4:-4]
            or signed[-4:] != unsigned[-4:]):
        raise ValueError("Bitcoin original unsigned transaction was modified")
    witnesses = signed[len(unsigned)-2:-4]
    pos = 0
    signer_public_key: bytes | None = None
    signature_fingerprints = []
    for index, entry in enumerate(context["signature_digests"]):
        try:
            items, pos = _compact(witnesses, pos)
            if items != 2:
                raise ValueError("Original P2WPKH witness stack changed")
            signature_length, pos = _compact(witnesses, pos)
            if not 9 <= signature_length <= 74 or (
                    pos + signature_length > len(witnesses)):
                raise ValueError("Malformed Bitcoin source witness DER length")
            signature = witnesses[pos:pos + signature_length]
            pos += signature_length
            public_length, pos = _compact(witnesses, pos)
            if public_length != 33 or pos + 33 > len(witnesses):
                raise ValueError("Original Bitcoin signer pubkey missing")
            key = witnesses[pos:pos + 33]
            pos += 33
        except (IndexError, ValueError):
            raise ValueError("Original Bitcoin witness is truncated") from None
        if signature[-1:] != b"\x01":
            raise ValueError("Only BIP143 SIGHASH_ALL is authorized")
        try:
            r, s = utils.decode_dss_signature(signature[:-1])
        except ValueError:
            raise ValueError("Original witness uses malformed ECDSA DER") from None
        if (not 0 < r < _CURVE_ORDER
                or not 0 < s <= _CURVE_ORDER // 2
                or utils.encode_dss_signature(r, s) != signature[:-1]):
            raise ValueError("Original Bitcoin signature not canonical low-S")
        proof = verify_native_public_key_owner(
            asset="BTC", owner=context["source_wallet"],
            public_key=key.hex(),
        )
        if proof["original_public_key_matches_wallet"] is not True:
            raise ValueError("Bitcoin witness key differs from owner")
        if signer_public_key is not None and key != signer_public_key:
            raise ValueError("BTC inputs changed original owner key")
        signer_public_key = key
        digest = bytes.fromhex(entry["bip143_sighash_all_hex"])
        try:
            public = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256K1(), key,
            )
            public.verify(
                signature[:-1], digest,
                ec.ECDSA(utils.Prehashed(hashes.SHA256())),
            )
        except (InvalidSignature, ValueError):
            raise ValueError("Wrong original Bitcoin owner witness signature") from None
        if entry["input_index"] != index:
            raise ValueError("Bitcoin signature input order mismatch")
        signature_fingerprints.append(hashlib.sha256(
            digest + signature + key,
        ).hexdigest())
    if pos != len(witnesses):
        raise ValueError("Bitcoin signed witness contains unexpected data")
    if _hash256(unsigned)[::-1].hex() != context["original_unsigned_txid"]:
        raise ValueError("Original Bitcoin source transaction ID mismatch")
    return {
        "mode": "PRIVY_BTC_ORIGINAL_SIGNED_TX_READBACK_ONLY",
        "asset": "BTC", "side": "SELL",
        "source_wallet": context["source_wallet"],
        "original_unsigned_txid": context["original_unsigned_txid"],
        "original_psbt_sha256": context["original_psbt_sha256"],
        "witness_txid": _hash256(signed)[::-1].hex(),
        "signed_wire_sha256": hashlib.sha256(signed).hexdigest(),
        "verified_input_count": len(signature_fingerprints),
        "input_signature_fingerprints": signature_fingerprints,
        "original_owner_signatures_verified": True,
        "original_unsigned_outputs_unchanged": True,
        "original_owner_public_key_verified": True,
        "original_order_send_claim_verified": False,
        "privy_remote_owner_policy_verified": False,
        "broadcast_authorized": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
