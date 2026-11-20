"""Locally verify owner XRPL signatures and canonicalize original Payment.

Only original ED25519 (64-byte) or secp256k1 (r||s) detached signatures
are accepted. This is pure offline assembly: no Privy signature request,
delegation, ledger submission, trade, payout or fee occurs.
"""
from __future__ import annotations

import base64
import hashlib
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils
from xrpl.core.binarycodec import encode

from .native_xrp_signing_blob import encode_original_xrp_privy_signing_blob

_HEX=re.compile(r"^[0-9a-fA-F]+$")
_SECP_ORDER=0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_TXN_PREFIX=bytes.fromhex("54584e00")


def verify_original_xrp_detached_signature(
    unsigned:object, *, original_owner_public_key_hex:object,
    detached_signature_hex:object, now:int,
)->dict:
    """Validate signature over the exact original owner-bound STX payload."""
    signing=encode_original_xrp_privy_signing_blob(
        unsigned,original_owner_public_key_hex=original_owner_public_key_hex,
        now=now,
    )
    if (type(detached_signature_hex) is not str
            or len(detached_signature_hex) not in (128,130,140,142,144,146)
            or not _HEX.fullmatch(detached_signature_hex)):
        raise ValueError("Original XRPL signature encoding unavailable")
    original_pub=bytes.fromhex(signing["original_public_key_hex"])
    payload=bytes.fromhex(signing["signing_blob_hex"])
    signature=bytes.fromhex(detached_signature_hex)
    if signing["signer_algorithm"]=="ed25519":
        if len(signature)!=64:
            raise ValueError("Original XRP Ed25519 signature length invalid")
        try:
            ed25519.Ed25519PublicKey.from_public_bytes(
                original_pub[1:],
            ).verify(signature,payload)
        except (InvalidSignature,ValueError):
            raise ValueError("Original XRPL Ed25519 signer invalid") from None
        canonical_signature=signature
    else:
        # The original Privy raw signing API may provide compact r||s
        # or DER. Canonical DER+low-S is the actual XRP Ledger form.
        if len(signature)==65:
            if signature[64] not in (0,1,27,28):
                raise ValueError("Original XRP signature recovery tag invalid")
            signature=signature[:64]
        if len(signature)==64:
            r=int.from_bytes(signature[:32],"big")
            s=int.from_bytes(signature[32:],"big")
        else:
            try:
                r,s=utils.decode_dss_signature(signature)
            except ValueError:
                raise ValueError("Original XRP DER signature invalid") from None
        if not 0<r<_SECP_ORDER or not 0<s<_SECP_ORDER:
            raise ValueError("Original XRPL secp256k1 scalar invalid")
        try:
            public=ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256K1(),original_pub,
            )
            public.verify(
                utils.encode_dss_signature(r,s),
                hashlib.sha512(payload).digest()[:32],
                ec.ECDSA(utils.Prehashed(hashes.SHA256())),
            )
        except (InvalidSignature,ValueError):
            raise ValueError("Original XRPL secp256k1 signer invalid") from None
        canonical_signature=utils.encode_dss_signature(
            r,min(s,_SECP_ORDER-s),
        )
    payment={**unsigned["unsigned_payment"],
             "SigningPubKey":signing["original_public_key_hex"],
             "TxnSignature":canonical_signature.hex().upper()}
    try:
        wire_hex=encode(payment)
        wire=bytes.fromhex(wire_hex)
    except Exception as exc:
        raise ValueError("Original XRPL canonical signed blob invalid") from exc
    txn_id=hashlib.sha512(_TXN_PREFIX+wire).digest()[:32].hex().upper()
    return {
        "mode":"ORIGINAL_XRP_SIGNED_WIRE_LOCALLY_VERIFIED_ONLY",
        "asset":"XRP","side":"SELL",
        "source_wallet":signing["source_wallet"],
        "destination_wallet":signing["destination_wallet"],
        "inbound_vault":signing["inbound_vault"],
        "source_amount_drops":signing["source_amount_drops"],
        "original_unsigned_payment_sha256":signing["original_payment_sha256"],
        "signing_blob_sha512half_hex":signing["signing_blob_sha512half_hex"],
        "signed_wire_base64":base64.b64encode(wire).decode("ascii"),
        "signed_wire_sha256":hashlib.sha256(wire).hexdigest(),
        "signed_transaction_hash":txn_id,
        "original_owner_signature_verified":True,
        "privy_policy_independently_verified":False,
        "original_order_claim_verified":False,
        "broadcast_authorized":False,
        "source_transaction_sent":False,
        "execution_enabled":False,"can_submit":False,
    }
