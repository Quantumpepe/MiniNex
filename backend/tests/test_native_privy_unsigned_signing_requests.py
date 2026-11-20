"""Original native Privy requests stay strictly offline and wallet-bound."""
import base64
import copy
import hashlib

import pytest

from backend.native_privy_unsigned_signing_requests import (
    preview_privy_btc_raw_sign_requests,
    preview_privy_sol_sign_transaction_request,
)
from backend.native_sol_compiled_message import compile_original_unsigned_sol_message
from backend.tests.test_native_btc_signed_wire import valid as btc_fixture
from backend.tests.test_native_sol_compiled_message import params as sol_fixture, OWNER

INTENT = "native_" + "a"*64
BTC_WALLET = "wallet_btc_owner_00001"
SOL_WALLET = "wallet_sol_owner_00001"


def _btc(two=False):
    psbt, inputs, pubkey, _ = btc_fixture(two=two)
    return preview_privy_btc_raw_sign_requests(
        psbt,original_utxos=inputs,owner_public_key_hex=pubkey,
        wallet_id=BTC_WALLET,intent_key=INTENT,
    )


def _sol():
    original = compile_original_unsigned_sol_message(**sol_fixture())
    return preview_privy_sol_sign_transaction_request(
        original,owner_public_key_hex=(bytes([7])*32).hex(),
        wallet_id=SOL_WALLET,intent_key=INTENT,
    )


def test_bitcoin_raw_privy_sign_calls_match_exact_original_sighashes():
    r = _btc()
    assert r["mode"] == "ORIGINAL_BTC_PRIVY_RPC_SIGNING_PREVIEW_ONLY"
    assert r["asset"] == "BTC"
    assert r["wallet_id"] == BTC_WALLET
    assert r["signature_request_count"] == 1
    item = r["unsigned_sign_requests"][0]
    assert item["method"] == "secp256k1_sign"
    assert set(item["params"]) == {"hash"}
    assert item["params"]["hash"].startswith("0x")
    assert len(item["params"]["hash"]) == 66
    assert len(item["idempotency_key"]) == 64
    assert r == _btc()
    for flag in ("api_requests_performed", "real_privy_policy_verified",
                 "original_order_claim_verified", "wallet_signed",
                 "source_transaction_sent", "execution_enabled", "can_submit"):
        assert r[flag] is False


def test_multi_input_bitcoin_signing_requests_are_distinct_and_ordered():
    r = _btc(two=True)
    assert r["signature_request_count"] == 2
    a, b = r["unsigned_sign_requests"]
    assert [a["input_index"], b["input_index"]] == [0,1]
    assert a["params"]["hash"] != b["params"]["hash"]
    assert a["idempotency_key"] != b["idempotency_key"]
    assert a["prevout_txid"] != b["prevout_txid"]


def test_sol_sign_only_rpc_serializes_blank_signature_slot_before_message():
    r = _sol()
    assert r["mode"] == "ORIGINAL_SOL_PRIVY_SIGN_TRANSACTION_PREVIEW_ONLY"
    assert r["method"] == "signTransaction"
    assert r["params"]["encoding"] == "base64"
    wire = base64.b64decode(r["params"]["transaction"], validate=True)
    compiled = compile_original_unsigned_sol_message(**sol_fixture())
    message = base64.b64decode(compiled["message_base64"], validate=True)
    assert wire == b"\x01" + bytes(64) + message
    assert hashlib.sha256(message).hexdigest() == r["original_message_sha256"]
    assert r["signature_slot_empty"] is True
    assert r["api_requests_performed"] is False
    assert r["wallet_signed"] is False
    assert r["can_submit"] is False
    assert r == _sol()


def test_wrong_sol_original_fee_payer_and_message_never_generate_sign_request():
    original = compile_original_unsigned_sol_message(**sol_fixture())
    bad = copy.deepcopy(original)
    bad["required_signers"] = ["external"]
    with pytest.raises(ValueError):
        preview_privy_sol_sign_transaction_request(
            bad,owner_public_key_hex=(bytes([7])*32).hex(),
            wallet_id=SOL_WALLET,intent_key=INTENT,
        )
    bad = copy.deepcopy(original)
    bad["message_sha256"] = "b"*64
    with pytest.raises(ValueError):
        preview_privy_sol_sign_transaction_request(
            bad,owner_public_key_hex=(bytes([7])*32).hex(),
            wallet_id=SOL_WALLET,intent_key=INTENT,
        )
    with pytest.raises(ValueError):
        preview_privy_sol_sign_transaction_request(
            original,owner_public_key_hex=(bytes([8])*32).hex(),
            wallet_id=SOL_WALLET,intent_key=INTENT,
        )


@pytest.mark.parametrize("bad_wallet,bad_key", [
    ("../../wrong", INTENT), (SOL_WALLET, "arbitrary"),
    (SOL_WALLET, "native_"+"z"*64),
])
def test_native_privy_request_identity_cannot_be_client_chosen(bad_wallet,bad_key):
    original = compile_original_unsigned_sol_message(**sol_fixture())
    with pytest.raises(ValueError):
        preview_privy_sol_sign_transaction_request(
            original,owner_public_key_hex=(bytes([7])*32).hex(),
            wallet_id=bad_wallet,intent_key=bad_key,
        )


def test_unverified_bitcoin_private_key_or_psbt_rejected():
    psbt, inputs, pubkey, _ = btc_fixture()
    changed = copy.deepcopy(psbt)
    changed["psbt_sha256"] = "0"*64
    with pytest.raises(ValueError):
        preview_privy_btc_raw_sign_requests(
            changed,original_utxos=inputs,owner_public_key_hex=pubkey,
            wallet_id=BTC_WALLET,intent_key=INTENT,
        )
    with pytest.raises(ValueError):
        preview_privy_btc_raw_sign_requests(
            psbt,original_utxos=inputs,
            owner_public_key_hex="02"+"aa"*32,
            wallet_id=BTC_WALLET,intent_key=INTENT,
        )


def test_sign_request_builders_have_no_http_or_live_privy_post():
    import inspect
    for fn in (preview_privy_btc_raw_sign_requests,
               preview_privy_sol_sign_transaction_request):
        source = inspect.getsource(fn)
        for forbidden in (
            "httpx", "requests.post", "sendrawtransaction",
            "sendAndSendTransaction", "signAndSend", "psycopg",
        ):
            assert forbidden not in source


def test_btc_privy_raw_sign_request_digest_roundtrips_through_detached_wire():
    from backend.native_btc_signed_wire import verify_original_btc_detached_signatures
    from backend.native_btc_segwit_sighash import build_original_btc_segwit_v0_sighashes
    psbt, inputs, public, detached_signatures = btc_fixture(two=True)
    requests = preview_privy_btc_raw_sign_requests(
        psbt, original_utxos=inputs,
        owner_public_key_hex=public,wallet_id=BTC_WALLET,intent_key=INTENT,
    )
    digest = build_original_btc_segwit_v0_sighashes(
        psbt,original_utxos=inputs,
    )
    for envelope, original in zip(
        requests["unsigned_sign_requests"],digest["signature_digests"]
    ):
        assert envelope["params"] == {
            "hash": "0x" + original["bip143_sighash_all_hex"]
        }
    result = verify_original_btc_detached_signatures(
        psbt,original_utxos=inputs,
        original_owner_public_key_hex=public,
        original_signatures_hex=detached_signatures,
    )
    assert result["verified_input_count"] == 2
    assert result["original_psbt_sha256"] == requests["original_psbt_sha256"]
    assert result["all_bip143_sighash_all_signatures_valid"] is True
    assert result["source_transaction_sent"] is False
    assert result["can_submit"] is False


def test_sol_offline_privy_sign_transaction_roundtrip_matches_original_memo():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    from backend.native_sol_signed_wire import verify_original_sol_detached_signature
    from backend.tests.test_native_sol_compiled_message import b58

    secret = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(1,33)))
    public = secret.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    owner = b58(public)
    original = sol_fixture()
    old_owner = original["quote"]["source_wallet"]
    original["quote"]["source_wallet"] = owner
    original["quote"]["memo"] = original["quote"]["memo"].replace(
        "/"+old_owner+":", "/"+owner+":",
    )
    original["source_account"]["owner"] = owner
    message = compile_original_unsigned_sol_message(**original)
    request = preview_privy_sol_sign_transaction_request(
        message,owner_public_key_hex=public.hex(),
        wallet_id=SOL_WALLET,intent_key=INTENT,
    )
    transaction = base64.b64decode(request["params"]["transaction"],validate=True)
    assert transaction[:65] == b"\x01"+bytes(64)
    signature = secret.sign(transaction[65:])
    proof = verify_original_sol_detached_signature(
        message,owner_signature_base64=base64.b64encode(signature).decode("ascii"),
    )
    assert proof["detached_ed25519_signature_valid"] is True
    assert base64.b64decode(proof["wire_base64"]) == (
        b"\x01" + signature + transaction[65:]
    )
    assert proof["source_wallet"] == request["source_wallet"]
    assert proof["source_transaction_sent"] is False
    assert proof["can_submit"] is False
