"""Targeted offline checks for the actual manual XRP transfer encoding."""
from decimal import Decimal

import pytest
from fastapi import HTTPException
from xrpl.core.binarycodec import decode, encode
from cryptography.hazmat.primitives.asymmetric import ed25519
from xrpl.core.addresscodec import encode_classic_address
import hashlib

from backend.native_xrp_manual_send import (
    amount_to_drops, original_xrp_wallet, prepare_mainnet_payment,
    verified_signed_blob, verify_privy_xrp_remote_owner, _same_privy_user_id,
    _safe_privy_error_category,
)
from backend.tests.test_native_xrp_thor_payment_tx import OWNER, VAULT

DID = "did:privy:xrp_sender"


def sample():
    server = {"result": {"status": "success", "info": {
        "network_id": 0, "validated_ledger": {
            "seq": 110000, "age": 1, "reserve_base_xrp": 1,
            "reserve_inc_xrp": 0.2,
        },
    }}}
    fee = {"result": {"status": "success", "drops": {
        "open_ledger_fee": "12",
    }}}
    account = {"result": {"status": "success", "validated": True,
        "account_data": {
            "Account": OWNER, "Sequence": 22, "OwnerCount": 0,
            "Balance": "3000000",
        },
    }}
    return server, fee, account


def test_manual_one_xrp_uses_canonical_hex_payload():
    payment = prepare_mainnet_payment(
        OWNER, VAULT, amount_to_drops("1"), *sample())
    assert payment["Amount"] == "1000000"
    assert payment["Fee"] == "12"
    assert payment["LastLedgerSequence"] == 110020
    assert decode(encode(payment)) == payment
    assert payment["Account"] == OWNER


@pytest.mark.parametrize("bad", ["0", "-1", "1.0000001", "NaN", None, {}, ""])
def test_invalid_or_fractional_amount_not_sent(bad):
    with pytest.raises(HTTPException) as exc:
        amount_to_drops(bad)
    assert exc.value.status_code == 422


def test_reserve_and_fee_enforced():
    server, fee, account = sample()
    with pytest.raises(HTTPException) as exc:
        prepare_mainnet_payment(OWNER, VAULT, "2000000", server, fee, account)
    assert exc.value.status_code == 422
    server["result"]["info"]["network_id"] = 1
    with pytest.raises(HTTPException):
        prepare_mainnet_payment(OWNER, VAULT, "1000000", server, fee, account)


def test_rejects_wallet_impersonation():
    linked = {"id": DID, "linked_accounts": [{
        "type": "wallet", "chain_type": "xrpl",
        "wallet_client_type": "privy",
        "address": OWNER, "id": "wallet_123456789",
    }]}
    assert original_xrp_wallet(linked, DID, OWNER) == "wallet_123456789"
    with pytest.raises(HTTPException):
        original_xrp_wallet(linked, DID, VAULT)


def test_signed_blob_must_match_wallet_and_exact_payment():
    # Deterministic fixture: signing key produces a genuine XRPL public key.
    key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    from cryptography.hazmat.primitives import serialization
    public = bytes([0xED]) + key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_hex = public.hex().upper()
    digest = hashlib.new("ripemd160", hashlib.sha256(public).digest()).digest()
    address = encode_classic_address(digest)
    unsigned = prepare_mainnet_payment(address, VAULT, "1000000", *sample_for(address))
    unsigned["SigningPubKey"] = public_hex
    signing = {**unsigned, "SigningPubKey": public_hex, "TxnSignature": "AB" * 64}
    blob = encode(signing)
    decoded, txid = verified_signed_blob(
        {"data": {"signed_transaction": blob}}, unsigned)
    assert decoded == blob
    assert len(txid) == 64
    bad = dict(signing, Amount="1000001")
    with pytest.raises(HTTPException):
        verified_signed_blob(
            {"data": {"signed_transaction": encode(bad)}}, unsigned)


def sample_for(address):
    server, fee, account = sample()
    account["result"]["account_data"]["Account"] = address
    return server, fee, account


def test_nullable_privy_entity_still_matches_owner_linked_wallet():
    remote = {
        "id": "wallet_123456789", "address": OWNER, "chain_type": "xrpl",
        "entity": None, "archived_at": None,
    }
    verify_privy_xrp_remote_owner(remote,
        wallet_id="wallet_123456789", address=OWNER, did=DID)
    verify_privy_xrp_remote_owner({k:v for k,v in remote.items() if k!="entity"},
        wallet_id="wallet_123456789", address=OWNER, did=DID)
    verify_privy_xrp_remote_owner({
        **remote, "entity": {"type": "user", "id": DID},
    }, wallet_id="wallet_123456789", address=OWNER, did=DID)


@pytest.mark.parametrize("different", [
    {"id": "wallet_other"},
    {"address": VAULT},
    {"chain_type": "ethereum"},
    {"entity": {"type": "user", "id": "did:privy:other"}},
    {"entity": {"type": "organization", "id": DID}},
    {"archived_at": "2026-01-01T00:00:00Z"},
])
def test_remote_wallet_contradictions_rejected(different):
    remote = {
        "id": "wallet_123456789", "address": OWNER, "chain_type": "xrpl",
        "entity": None, "archived_at": None,
        **different,
    }
    with pytest.raises(HTTPException):
        verify_privy_xrp_remote_owner(remote,
            wallet_id="wallet_123456789", address=OWNER, did=DID)


@pytest.mark.parametrize("entity,expected", [
    ({"type":"organization","id":"organization_abc"}, "unexpected owner entity type"),
    ({"type":"user","id":"did:privy:someone_else"}, "different user ID"),
])
def test_owner_mismatch_has_distinct_diagnostic_without_revealing_ids(entity,expected):
    remote = {
        "id":"wallet_123456789", "address":OWNER, "chain_type":"xrpl",
        "archived_at":None, "entity":entity,
    }
    with pytest.raises(HTTPException) as exc:
        verify_privy_xrp_remote_owner(remote,
            wallet_id="wallet_123456789", address=OWNER, did=DID)
    assert exc.value.status_code == 409
    assert expected in exc.value.detail
    assert entity["id"] not in exc.value.detail


def test_privy_wallet_entity_id_can_be_exact_did_suffix():
    # Privy's wallet metadata can use the ID without the DID namespace.
    did="did:privy:cmw7yzn8q00a90gl8324o3ipy"
    user_id="cmw7yzn8q00a90gl8324o3ipy"
    remote={
        "id":"wallet_123456789", "address":OWNER, "chain_type":"xrpl",
        "archived_at":None,"entity":{"type":"user","id":user_id},
    }
    verify_privy_xrp_remote_owner(remote,
        wallet_id="wallet_123456789",address=OWNER,did=did)
    assert _same_privy_user_id(user_id,did)
    assert _same_privy_user_id(did,did)


@pytest.mark.parametrize("other_id",[
    "cmw7yzn8q00a90gl8324o3ip", 
    "cmw7yzn8q00a90gl8324o3ipy_extra",
    "did:privy:someone_else",
    "someone_else",None,"",
])
def test_prefix_free_privy_id_matching_is_exact(other_id):
    did="did:privy:cmw7yzn8q00a90gl8324o3ipy"
    assert not _same_privy_user_id(other_id,did)
    remote={
        "id":"wallet_123456789", "address":OWNER, "chain_type":"xrpl",
        "archived_at":None, "entity":{"type":"user","id":other_id},
    }
    with pytest.raises(HTTPException) as exc:
        verify_privy_xrp_remote_owner(remote,
            wallet_id="wallet_123456789",address=OWNER,did=did)
    assert exc.value.status_code==409


@pytest.mark.parametrize("payload, expected", [
    ({"error":{"message":"params.transaction: Expected string, received object"}}, "transaction_schema"),
    ({"error":{"message":"params.encoding: Invalid literal value"}}, "encoding_schema"),
    ({"error":{"message":"Unsupported method xrpl_signTransaction"}}, "unsupported_rpc_method"),
    ({"error":{"message":"Signer not configured"}}, "signer_configuration"),
    ({"error":{"message":"Wallet authorization required"}}, "wallet_authorization"),
    ({"error":{"message":"invalid chain_type xrpl"}}, "unsupported_chain"),
    ({"error":{"message":"Sensitive wallet identifier 1234567890"}}, "unclassified_privy_validation"),
    ({}, "missing_error_detail"),
])
def test_safe_privy_rejection_category_does_not_echo_remote_message(payload,expected):
    assert _safe_privy_error_category(payload)==expected
