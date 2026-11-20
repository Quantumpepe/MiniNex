"""SegWit owner signatures are verified without any Privy/broadcast side effect."""
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from backend import native_btc_signed_wire as wire
from backend.native_btc_thor_psbt import compile_original_unsigned_btc_thor_psbt
from backend.native_btc_segwit_sighash import build_original_btc_segwit_v0_sighashes
from backend.tests.test_native_btc_thor_outputs import (
    quote, SOURCE, VAULT_SCRIPT, AMOUNT, NOW, _reference_address,
)


def valid(two=False):
    key = ec.derive_private_key(42, ec.SECP256K1())
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    h160 = hashlib.new("ripemd160", hashlib.sha256(pub).digest()).digest()
    owner_script = "0014" + h160.hex()
    owner = _reference_address(h160.hex())
    q = quote()
    q["source_wallet"] = owner
    q["memo"] = q["memo"].replace("/" + SOURCE + ":", "/" + owner + ":")
    inputs = [{
        "txid": "ab"*32, "vout": 2, "value_sats": 5000000,
        "script_pubkey_hex": owner_script,
    }]
    if two:
        inputs.append({
            "txid": "cd"*32, "vout": 0, "value_sats": 1000000,
            "script_pubkey_hex": owner_script,
        })
    psbt = compile_original_unsigned_btc_thor_psbt(
        q, amount=AMOUNT, now=NOW,
        owner_script_hex=owner_script, vault_script_hex=VAULT_SCRIPT,
        input_utxos=inputs, fee_rate_sats_per_vbyte=5,
    )
    digests = build_original_btc_segwit_v0_sighashes(
        psbt, original_utxos=inputs,
    )
    signatures = []
    for item in digests["signature_digests"]:
        signature = key.sign(
            bytes.fromhex(item["bip143_sighash_all_hex"]),
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
        r, s = utils.decode_dss_signature(signature)
        signatures.append(r.to_bytes(32,"big").hex() + s.to_bytes(32,"big").hex())
    return psbt, inputs, pub.hex(), signatures


def test_verifies_real_original_owner_segwit_signature_and_wire():
    psbt, inputs, pubkey, signatures = valid()
    result = wire.verify_original_btc_detached_signatures(
        psbt, original_utxos=inputs,
        original_owner_public_key_hex=pubkey,
        original_signatures_hex=signatures,
    )
    assert result["mode"] == "ORIGINAL_BTC_SEGWIT_WIRE_LOCALLY_VERIFIED_ONLY"
    assert result["all_bip143_sighash_all_signatures_valid"] is True
    assert result["owner_public_key_matches_original_wallet"] is True
    assert result["verified_input_count"] == 1
    raw = base64.b64decode(result["signed_wire_base64"],validate=True)
    assert raw[:4] == bytes.fromhex(psbt["unsigned_tx_hex"])[:4]
    assert raw[4:6] == b"\x00\x01"
    assert raw[-4:] == bytes(4)
    assert result["original_unsigned_txid"] == psbt["unsigned_txid"]
    assert result["witness_txid"] != result["original_unsigned_txid"]
    assert len(result["input_signature_fingerprints"][0]) == 64
    for key in ("remote_privy_delegation_verified",
                "fresh_owner_consent_verified",
                "original_order_send_claim_verified",
                "broadcast_authorized",
                "source_transaction_sent", "execution_enabled",
                "can_submit"):
        assert result[key] is False


def test_original_owner_two_separate_utxo_signatures_verify():
    psbt, inputs, pubkey, signatures = valid(two=True)
    result = wire.verify_original_btc_detached_signatures(
        psbt, original_utxos=inputs,
        original_owner_public_key_hex=pubkey,
        original_signatures_hex=signatures,
    )
    assert result["verified_input_count"] == 2
    assert result["input_signature_fingerprints"][0] != (
        result["input_signature_fingerprints"][1])


@pytest.mark.parametrize("mutation", [
    lambda sigs: sigs.__setitem__(0, "00"*64),
    lambda sigs: sigs.__setitem__(0, "ab"*64),
    lambda sigs: sigs.__setitem__(0, "ff"),
    lambda sigs: sigs.append(sigs[0]),
])
def test_invalid_signature_or_extra_signature_rejected(mutation):
    psbt, inputs, pubkey, signatures = valid()
    mutation(signatures)
    with pytest.raises(ValueError):
        wire.verify_original_btc_detached_signatures(
            psbt, original_utxos=inputs,
            original_owner_public_key_hex=pubkey,
            original_signatures_hex=signatures,
        )


def test_wrong_original_privy_public_key_rejected():
    psbt, inputs, pubkey, signatures = valid()
    other = ec.derive_private_key(7, ec.SECP256K1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint,
    )
    with pytest.raises(ValueError):
        wire.verify_original_btc_detached_signatures(
            psbt, original_utxos=inputs,
            original_owner_public_key_hex=other.hex(),
            original_signatures_hex=signatures,
        )


def test_modified_original_value_and_digest_cannot_be_signed():
    psbt, inputs, pubkey, signatures = valid()
    inputs[0]["value_sats"] = 4999999
    with pytest.raises(ValueError):
        wire.verify_original_btc_detached_signatures(
            psbt, original_utxos=inputs,
            original_owner_public_key_hex=pubkey,
            original_signatures_hex=signatures,
        )


def test_original_signer_is_mathematically_verified_only_not_privy_authorized():
    import inspect
    code = inspect.getsource(wire.verify_original_btc_detached_signatures)
    for action in ("sendrawtransaction", "httpx", "requests.post",
                   "privy_rpc", "psycopg", "private_key"):
        assert action not in code
