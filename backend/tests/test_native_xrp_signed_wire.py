"""Original XRP detached signatures validated before canonical wire assembly."""
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils
from xrpl.core.binarycodec import decode

from backend.native_xrp_signing_blob import encode_original_xrp_privy_signing_blob
from backend.native_xrp_signed_wire import verify_original_xrp_detached_signature
from backend.tests.test_native_xrp_signing_blob import original
from backend.tests.test_native_xrp_thor_payment_tx import NOW


def signed(algorithm="secp256k1"):
    unsigned, pub = original(algorithm)
    blob = encode_original_xrp_privy_signing_blob(
        unsigned, original_owner_public_key_hex=pub, now=NOW,
    )
    payload = bytes.fromhex(blob["signing_blob_hex"])
    if algorithm == "ed25519":
        key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(1,33)))
        signature = key.sign(payload)
    else:
        key = ec.derive_private_key(31,ec.SECP256K1())
        der = key.sign(
            hashlib.sha512(payload).digest()[:32],
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
        r,s = utils.decode_dss_signature(der)
        signature = r.to_bytes(32,"big") + s.to_bytes(32,"big")
    return unsigned, pub, signature.hex()


@pytest.mark.parametrize("algo",["secp256k1","ed25519"])
def test_original_native_xrp_source_wire_matches_only_owner_signature(algo):
    unsigned,pub,sig = signed(algo)
    result = verify_original_xrp_detached_signature(
        unsigned, original_owner_public_key_hex=pub,
        detached_signature_hex=sig, now=NOW,
    )
    assert result["mode"] == "ORIGINAL_XRP_SIGNED_WIRE_LOCALLY_VERIFIED_ONLY"
    assert result["source_wallet"] == unsigned["source_wallet"]
    assert result["inbound_vault"] == unsigned["inbound_vault"]
    assert result["original_owner_signature_verified"] is True
    assert len(result["signed_transaction_hash"]) == 64
    assert len(result["signing_blob_sha512half_hex"]) == 64
    wire = base64.b64decode(result["signed_wire_base64"],validate=True)
    tx = decode(wire.hex().upper())
    assert tx["Account"] == unsigned["source_wallet"]
    assert tx["Destination"] == unsigned["inbound_vault"]
    assert tx["Amount"] == unsigned["source_amount_drops"]
    assert tx["Fee"] == unsigned["network_fee_drops"]
    assert tx["Flags"] == 0x80000000
    assert tx["TxnSignature"]
    for flag in ("privy_policy_independently_verified",
                 "original_order_claim_verified","broadcast_authorized",
                 "source_transaction_sent","execution_enabled","can_submit"):
        assert result[flag] is False


@pytest.mark.parametrize("alg",["secp256k1","ed25519"])
def test_wrong_or_modified_xrp_original_signature_rejected(alg):
    unsigned,pub,sig = signed(alg)
    tampered = ("00" if sig[-2:]!="00" else "01")
    with pytest.raises(ValueError):
        verify_original_xrp_detached_signature(
            unsigned, original_owner_public_key_hex=pub,
            detached_signature_hex=sig[:-2]+tampered, now=NOW,
        )


@pytest.mark.parametrize("alg",["secp256k1","ed25519"])
def test_wrong_original_owner_pubkey_or_quote_expiry_rejected(alg):
    unsigned,pub,sig = signed(alg)
    with pytest.raises(ValueError):
        verify_original_xrp_detached_signature(
            unsigned, original_owner_public_key_hex="02"+"aa"*32,
            detached_signature_hex=sig, now=NOW,
        )
    with pytest.raises(ValueError):
        verify_original_xrp_detached_signature(
            unsigned, original_owner_public_key_hex=pub,
            detached_signature_hex=sig, now=NOW+200,
        )


def test_original_xrp_payment_terms_cannot_change_after_signing():
    unsigned,pub,sig = signed()
    unsigned["unsigned_payment"]["Amount"]="1"
    with pytest.raises(ValueError):
        verify_original_xrp_detached_signature(
            unsigned, original_owner_public_key_hex=pub,
            detached_signature_hex=sig, now=NOW,
        )


def test_native_xrp_signed_wire_has_no_broadcast_dependency():
    import inspect
    code=inspect.getsource(verify_original_xrp_detached_signature)
    for denied in ("sendrawtransaction","requests.post","PrivyClient",
                   "psycopg","private_key","submit("):
        assert denied not in code
