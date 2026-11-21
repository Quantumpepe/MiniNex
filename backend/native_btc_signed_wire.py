"""Locally verify original owner ECDSA inputs and assemble SegWit wire bytes.

Privy uses raw secp256k1 signing for native Bitcoin. This pure function takes
already-existing detached signatures, recomputes BIP143 messages, verifies
each against the original P2WPKH owner pubkey, normalizes Bitcoin low-S
encoding and assembles one canonical witness transaction. It never requests
Privy to sign, grants a policy, claims an order, or broadcasts anything.
"""
from __future__ import annotations

import base64
import hashlib
import re
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from .native_btc_segwit_sighash import (
    build_original_btc_segwit_v0_sighashes, _hash256,
)
from .native_btc_thor_outputs import _mainnet_p2wpkh_script

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _compact(value: int) -> bytes:
    if type(value) is not int or not 0 <= value <= 0xffffffff:
        raise ValueError("Invalid Bitcoin witness vector length")
    if value < 253:
        return bytes((value,))
    if value <= 0xffff:
        return b"\xfd" + struct.pack("<H", value)
    return b"\xfe" + struct.pack("<I", value)


def verify_original_btc_detached_signatures(
    psbt: object, *, original_utxos: object,
    original_owner_public_key_hex: object,
    original_signatures_hex: object,
) -> dict:
    """Return locally verified witness tx bytes without allowing submission."""
    context = build_original_btc_segwit_v0_sighashes(
        psbt, original_utxos=original_utxos,
    )
    if (type(original_owner_public_key_hex) is not str
            or not _HEX.fullmatch(original_owner_public_key_hex)
            or len(original_owner_public_key_hex) != 66
            or type(original_signatures_hex) is not list
            or len(original_signatures_hex) != context["input_count"]):
        raise ValueError("Original Privy BTC owner pubkey/signatures missing")
    public_bytes = bytes.fromhex(original_owner_public_key_hex)
    if public_bytes[0] not in (2, 3):
        raise ValueError("Original Bitcoin public key not compressed")
    owner_script = _mainnet_p2wpkh_script(context["source_wallet"])
    witness_hash = hashlib.new(
        "ripemd160", hashlib.sha256(public_bytes).digest(),
    ).digest()
    if owner_script != bytes((0, 20)) + witness_hash:
        raise ValueError("Original Bitcoin owner pubkey does not match script")
    try:
        public = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256K1(), public_bytes,
        )
    except ValueError:
        raise ValueError("Original Privy Bitcoin public key invalid") from None
    witnesses = bytearray()
    signature_fingerprints = []
    for i, (text, entry) in enumerate(zip(
        original_signatures_hex, context["signature_digests"],
    )):
        if (type(text) is not str
                or len(text) != 128
                or not _HEX.fullmatch(text)
                or entry["input_index"] != i):
            raise ValueError("Bitcoin source input signature malformed")
        raw = bytes.fromhex(text)
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        if not (0 < r < _CURVE_ORDER and 0 < s < _CURVE_ORDER):
            raise ValueError("Bitcoin source input ECDSA scalar invalid")
        message = bytes.fromhex(entry["bip143_sighash_all_hex"])
        try:
            public.verify(
                utils.encode_dss_signature(r, s), message,
                ec.ECDSA(utils.Prehashed(hashes.SHA256())),
            )
        except (InvalidSignature, ValueError):
            raise ValueError("Detached original Bitcoin owner signature invalid") from None
        # BIP62 standardness requires low S; (r, n-s) remains ECDSA valid.
        low_s = min(s, _CURVE_ORDER - s)
        der = utils.encode_dss_signature(r, low_s) + b"\x01"
        witnesses.extend(_compact(2))
        witnesses.extend(_compact(len(der)))
        witnesses.extend(der)
        witnesses.extend(_compact(len(public_bytes)))
        witnesses.extend(public_bytes)
        signature_fingerprints.append(
            hashlib.sha256(message + der + public_bytes).hexdigest(),
        )

    original = bytes.fromhex(psbt["unsigned_tx_hex"])
    # A native SegWit v0 unsigned tx has no witness marker/flag. Input,
    # output and locktime bytes are unchanged when adding witnesses.
    wire = (
        original[:4] + b"\x00\x01" + original[4:-4]
        + witnesses + original[-4:]
    )
    if _hash256(original)[::-1].hex() != context["original_unsigned_txid"]:
        raise ValueError("Bitcoin source txid changed while assembling witnesses")
    return {
        "mode": "ORIGINAL_BTC_SEGWIT_WIRE_LOCALLY_VERIFIED_ONLY",
        "asset": "BTC", "side": "SELL",
        "source_wallet": context["source_wallet"],
        "original_unsigned_txid": context["original_unsigned_txid"],
        "witness_txid": _hash256(wire)[::-1].hex(),
        "signed_wire_base64": base64.b64encode(wire).decode("ascii"),
        "signed_wire_sha256": hashlib.sha256(wire).hexdigest(),
        "owner_pubkey_sha256": hashlib.sha256(public_bytes).hexdigest(),
        "original_psbt_sha256": context["original_psbt_sha256"],
        "verified_input_count": len(signature_fingerprints),
        "input_signature_fingerprints": signature_fingerprints,
        "owner_public_key_matches_original_wallet": True,
        "all_bip143_sighash_all_signatures_valid": True,
        "remote_privy_delegation_verified": False,
        "fresh_owner_consent_verified": False,
        "original_order_send_claim_verified": False,
        "broadcast_authorized": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
