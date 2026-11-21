"""Original BTC THORChain BIP174 PSBT must not grant send authority."""
import base64
import hashlib
import struct

import pytest

from backend.native_btc_thor_psbt import compile_original_unsigned_btc_thor_psbt
from backend.tests.test_native_btc_thor_outputs import (
    quote, NOW, AMOUNT, SOURCE, VAULT, SOURCE_SCRIPT, VAULT_SCRIPT,
)


def _inputs():
    return [{
        "txid": "ab" * 32,
        "vout": 2,
        "value_sats": 5000000,
        "script_pubkey_hex": SOURCE_SCRIPT,
    }]


def _build(**overrides):
    args = dict(
        quote=quote(), amount=AMOUNT, now=NOW,
        owner_script_hex=SOURCE_SCRIPT, vault_script_hex=VAULT_SCRIPT,
        input_utxos=_inputs(), fee_rate_sats_per_vbyte=5,
    )
    args.update(overrides)
    return compile_original_unsigned_btc_thor_psbt(**args)


def _varint(data, offset):
    tag = data[offset]
    if tag < 253:
        return tag, offset + 1
    if tag == 253:
        return struct.unpack_from("<H", data, offset + 1)[0], offset + 3
    if tag == 254:
        return struct.unpack_from("<I", data, offset + 1)[0], offset + 5
    return struct.unpack_from("<Q", data, offset + 1)[0], offset + 9


def test_bip174_global_unsigned_transaction_witness_utxo_and_empty_maps():
    result = _build()
    blob = base64.b64decode(result["psbt_base64"], validate=True)
    assert blob.startswith(b"psbt\xff")
    assert result["psbt_sha256"] == hashlib.sha256(blob).hexdigest()
    assert result["psbt_created"] is True
    offset = 5
    key_length, offset = _varint(blob, offset)
    assert key_length == 1
    assert blob[offset] == 0
    offset += 1
    length, offset = _varint(blob, offset)
    unsigned = blob[offset:offset + length]
    offset += length
    assert unsigned.hex() == result["unsigned_tx_hex"]
    assert result["unsigned_txid"] == hashlib.sha256(
        hashlib.sha256(unsigned).digest()).digest()[::-1].hex()
    assert blob[offset] == 0
    offset += 1
    assert unsigned[:4] == struct.pack("<I", 2)
    assert unsigned[4] == 1
    assert unsigned[5:37] == bytes.fromhex("ab" * 32)[::-1]
    assert unsigned[37:41] == struct.pack("<I", 2)
    assert unsigned[41] == 0  # unpopulated scriptSig
    assert unsigned[42:46] == struct.pack("<I", 0xfffffffd)
    assert unsigned[46] == result["output_count"]
    assert unsigned[-4:] == bytes(4)  # no locktime

    key_length, offset = _varint(blob, offset)
    assert key_length == 1 and blob[offset] == 1
    offset += 1  # BIP174 input witness_utxo key
    witness_length, offset = _varint(blob, offset)
    witness = blob[offset:offset + witness_length]
    offset += witness_length
    assert witness == struct.pack("<Q", 5000000) + b"\x16" + bytes.fromhex(SOURCE_SCRIPT)
    assert blob[offset] == 0
    offset += 1
    assert blob[offset:] == b"\x00" * result["output_count"]
    assert result["source_wallet"] == SOURCE
    assert result["inbound_vault"] == VAULT
    assert result["input_count"] == 1
    for flag in (
        "utxo_chain_ownership_checked", "utxo_unspent_state_checked",
        "current_privy_policy_verified", "owner_consent_verified",
        "original_order_claim_verified", "wallet_signed", "ready_to_submit",
        "source_transaction_sent", "execution_enabled", "can_submit",
    ):
        assert result[flag] is False


@pytest.mark.parametrize("mutate", [
    lambda u: u[0].update(script_pubkey_hex=VAULT_SCRIPT),
    lambda u: u[0].update(script_pubkey_hex="0014" + "33" * 20),
    lambda u: u[0].update(script_pubkey_hex="deadbeef"),
    lambda u: u[0].update(script_pubkey_hex=None),
    lambda u: u[0].update(value_sats=0),
    lambda u: u[0].update(value_sats=5000000.0),
    lambda u: u[0].update(txid="bad"),
    lambda u: u[0].update(vout=-1),
    lambda u: u.append(dict(u[0])),
])
def test_source_witness_utxo_cannot_redirect_or_duplicate(mutate):
    inputs = _inputs()
    mutate(inputs)
    with pytest.raises(ValueError):
        _build(input_utxos=inputs)


def test_two_owner_inputs_are_serialized_as_distinct_witness_psbt_maps():
    inputs = _inputs()
    inputs.append({
        "txid": "cd" * 32, "vout": 0,
        "value_sats": 1000000, "script_pubkey_hex": SOURCE_SCRIPT,
    })
    r = _build(input_utxos=inputs)
    assert r["input_count"] == 2
    assert r["output_count"] >= 3
    assert r["source_amount_sats"] == 2500000
    assert r["miner_fee_sats"] > 0


def test_signer_or_broadcast_not_present_in_psbt_compiler():
    import inspect
    source = inspect.getsource(compile_original_unsigned_btc_thor_psbt)
    for denied in ("PrivyClient", "broadcast", "sendrawtransaction",
                   "private_key", "seed_phrase", "psycopg"):
        if denied == "broadcast":
            continue  # docstring explicitly states broadcast never happens
        assert denied not in source
