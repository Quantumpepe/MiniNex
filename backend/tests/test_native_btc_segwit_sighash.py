"""BIP143 preimages match the exact original unsigned PSBT and UTXOs."""
import base64
import copy
import hashlib
import struct

import pytest

from backend import native_btc_segwit_sighash as b
from backend.tests.test_native_btc_thor_psbt import _build, _inputs
from backend.tests.test_native_btc_thor_outputs import SOURCE_SCRIPT


def _digest(raw):
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


def test_original_owner_bip143_signing_input_exact_and_not_executable():
    psbt = _build()
    original = _inputs()
    result = b.build_original_btc_segwit_v0_sighashes(
        psbt, original_utxos=original)
    assert result["mode"] == "BTC_ORIGINAL_OWNER_SEGWIT_V0_SIGHASHES_ONLY"
    assert result["input_count"] == 1
    assert result["original_psbt_sha256"] == psbt["psbt_sha256"]
    assert result["sighash_type"] == 1
    assert len(result["signature_digests"][0]["bip143_sighash_all_hex"]) == 64
    for flag in (
        "signature_requests_performed", "privy_signer_policy_verified",
        "source_outpoint_lock_acquired", "signature_applied",
        "source_transaction_sent", "execution_enabled", "can_submit",
    ):
        assert result[flag] is False

    unsigned = bytes.fromhex(psbt["unsigned_tx_hex"])
    parsed = b._parse_original_unsigned_tx(unsigned)
    outpoint = bytes.fromhex(original[0]["txid"])[::-1] + struct.pack("<I", 2)
    seq = struct.pack("<I", 0xfffffffd)
    assert parsed["inputs"] == [(outpoint, seq)]
    assert len(parsed["outputs"]) == psbt["output_count"]
    script_code = bytes.fromhex("1976a914" + SOURCE_SCRIPT[4:] + "88ac")
    expected_preimage = (
        struct.pack("<I", 2) + _digest(outpoint) + _digest(seq)
        + outpoint + script_code + struct.pack("<Q", original[0]["value_sats"])
        + seq + _digest(b"".join(parsed["outputs"]))
        + struct.pack("<I", 0) + struct.pack("<I", 1)
    )
    assert result["signature_digests"][0]["bip143_sighash_all_hex"] == (
        _digest(expected_preimage).hex())


def test_each_witness_input_has_distinct_correct_bip143_digest():
    inputs = _inputs()
    inputs.append({"txid":"cd"*32,"vout":0,"value_sats":1000000,
                   "script_pubkey_hex":SOURCE_SCRIPT})
    psbt = _build(input_utxos=inputs)
    r = b.build_original_btc_segwit_v0_sighashes(
        psbt, original_utxos=inputs)
    assert r["input_count"] == 2
    assert len(r["signature_digests"]) == 2
    assert r["signature_digests"][0]["bip143_sighash_all_hex"] != (
        r["signature_digests"][1]["bip143_sighash_all_hex"])


@pytest.mark.parametrize("change", [
    lambda r: r[0].update(value_sats=100),
    lambda r: r[0].update(value_sats=5000000.0),
    lambda r: r[0].update(script_pubkey_hex="0014"+"33"*20),
    lambda r: r[0].update(txid="cd"*32),
    lambda r: r[0].update(vout=3),
    lambda r: r.append(dict(r[0])),
])
def test_bip143_never_signs_forged_input_prevout_value_or_script(change):
    plan = _build()
    utxos = _inputs()
    change(utxos)
    with pytest.raises(ValueError):
        b.build_original_btc_segwit_v0_sighashes(
            plan, original_utxos=utxos)


@pytest.mark.parametrize("change", [
    lambda p: p.update(wallet_signed=True),
    lambda p: p.update(can_submit=True),
    lambda p: p.update(ready_to_submit=True),
    lambda p: p.update(execution_enabled=True),
    lambda p: p.update(source_transaction_sent=True),
    lambda p: p.update(unsigned_txid="ff"*32),
    lambda p: p.update(unsigned_tx_hex=p["unsigned_tx_hex"][:-2]+"00"),
    lambda p: p.update(psbt_sha256="0"*64),
    lambda p: p.update(psbt_base64="!!!"),
    lambda p: p.update(source_wallet=p["inbound_vault"]),
])
def test_signature_digest_builder_rejects_untrusted_original_psbt(change):
    plan = _build()
    change(plan)
    with pytest.raises(ValueError):
        b.build_original_btc_segwit_v0_sighashes(
            plan, original_utxos=_inputs())


def test_rehashed_psbt_with_mutated_witness_utxo_still_rejected():
    plan = _build()
    envelope = bytearray(base64.b64decode(plan["psbt_base64"]))
    original_amount = struct.pack("<Q", 5000000)
    position = envelope.find(original_amount)
    assert position > 0
    envelope[position:position+8] = struct.pack("<Q", 4900000)
    plan["psbt_base64"] = base64.b64encode(envelope).decode("ascii")
    plan["psbt_sha256"] = hashlib.sha256(envelope).hexdigest()
    with pytest.raises(ValueError, match="witness UTXO"):
        b.build_original_btc_segwit_v0_sighashes(
            plan, original_utxos=_inputs())


def test_sighash_helper_never_holds_private_key_or_performs_privy_signature():
    import inspect
    src = inspect.getsource(b.build_original_btc_segwit_v0_sighashes)
    for forbidden in ("request.post", "httpx", "seed_phrase",
                      "private_key", "sendrawtransaction", "signDigest("):
        assert forbidden not in src
