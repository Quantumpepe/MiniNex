"""Cryptographically verify existing SegWit wire bytes, never send them."""
from __future__ import annotations

import base64
import copy

import pytest

from backend.native_btc_privy_signed_readback import (
    inspect_original_btc_signed_transaction,
)
from backend.native_btc_signed_wire import (
    verify_original_btc_detached_signatures,
)
from backend.tests.test_native_btc_signed_wire import valid


def signed(two=False):
    psbt, utxos, pub, signatures = valid(two=two)
    r = verify_original_btc_detached_signatures(
        psbt, original_utxos=utxos,
        original_owner_public_key_hex=pub,
        original_signatures_hex=signatures,
    )
    return psbt, utxos, base64.b64decode(r["signed_wire_base64"])


def inspect(psbt, utxos, raw):
    return inspect_original_btc_signed_transaction(
        psbt, original_utxos=utxos,
        signed_wire_base64=base64.b64encode(raw).decode("ascii"),
    )


@pytest.mark.parametrize("two", [False, True])
def test_original_btc_privy_signed_witness_matches_owner_and_all_vouts(two):
    psbt, utxos, signed_wire = signed(two=two)
    result = inspect(psbt, utxos, signed_wire)
    assert result["mode"] == "PRIVY_BTC_ORIGINAL_SIGNED_TX_READBACK_ONLY"
    assert result["source_wallet"] == psbt["source_wallet"]
    assert result["original_unsigned_txid"] == psbt["unsigned_txid"]
    assert result["original_psbt_sha256"] == psbt["psbt_sha256"]
    assert result["original_owner_signatures_verified"] is True
    assert result["original_unsigned_outputs_unchanged"] is True
    assert result["original_owner_public_key_verified"] is True
    assert result["verified_input_count"] == (2 if two else 1)
    assert result["witness_txid"] != result["original_unsigned_txid"]
    assert len(result["signed_wire_sha256"]) == 64
    for flag in (
        "original_order_send_claim_verified",
        "privy_remote_owner_policy_verified",
        "broadcast_authorized", "source_transaction_sent",
        "execution_enabled", "can_submit",
    ):
        assert result[flag] is False
    for forbidden in ("signed_wire_base64", "private_key", "memo"):
        assert forbidden not in result


@pytest.mark.parametrize("mutation", [
    lambda raw,pos: raw.__setitem__(0,raw[0]^1),
    lambda raw,pos: raw.__setitem__(4,1),
    lambda raw,pos: raw.__setitem__(5,0),
    lambda raw,pos: raw.__setitem__(9,raw[9]^1),
    lambda raw,pos: raw.__setitem__(-5,raw[-5]^1),
    lambda raw,pos: raw.__setitem__(-1,raw[-1]^1),
    lambda raw,pos: raw.__setitem__(pos,3),
    lambda raw,pos: raw.__setitem__(pos+1,0),
    lambda raw,pos: raw.__setitem__(pos+2,raw[pos+2]^1),
    lambda raw,pos: raw.__setitem__(
        pos+2+raw[pos+1]-1,2),
    lambda raw,pos: raw.__setitem__(
        pos+2+raw[pos+1]+1,1),
    lambda raw,pos: raw.insert(-4,0),
    lambda raw,pos: raw.pop(),
])
def test_tampered_witness_version_sighash_owner_or_payment_rejected(mutation):
    psbt, utxos, signed_wire = signed()
    raw = bytearray(signed_wire)
    witness_pos = len(bytes.fromhex(psbt["unsigned_tx_hex"])) - 2
    mutation(raw,witness_pos)
    with pytest.raises(ValueError):
        inspect(psbt,utxos,bytes(raw))


def test_previous_utxo_value_or_psbt_digest_cannot_be_replayed_with_signed_wire():
    psbt,utxos,signed_wire = signed()
    altered_utxos = copy.deepcopy(utxos)
    altered_utxos[0]["value_sats"] += 1
    with pytest.raises(ValueError):
        inspect(psbt, altered_utxos, signed_wire)
    altered_psbt = copy.deepcopy(psbt)
    altered_psbt["psbt_sha256"] = "f"*64
    with pytest.raises(ValueError):
        inspect(altered_psbt,utxos,signed_wire)


def test_sighash_all_signature_cannot_be_used_for_another_original_outpoint():
    psbt,utxos,signed_wire = signed(two=True)
    raw = bytearray(signed_wire)
    pos = len(bytes.fromhex(psbt["unsigned_tx_hex"])) - 2
    first_stack = 1+1+raw[pos+1]+1+33
    second_pos = pos+first_stack
    assert raw[pos] == raw[second_pos] == 2
    # Swap equal-size original witness stacks when possible, otherwise
    # change one stack's raw signature while retaining owner pubkey.
    first_signature = bytes(raw[pos+2:pos+2+raw[pos+1]])
    n = min(len(first_signature), raw[second_pos+1])
    raw[second_pos+2:second_pos+2+n] = first_signature[:n]
    with pytest.raises(ValueError):
        inspect(psbt,utxos,bytes(raw))


def test_no_remote_signing_or_send_action_in_btc_readback_function():
    import inspect as stdlib_inspect
    source = stdlib_inspect.getsource(inspect_original_btc_signed_transaction)
    for forbidden in (
        "requests.post", "httpx", "PrivyClient",
        "sendrawtransaction", "psycopg", "wallet_send",
    ):
        assert forbidden not in source
