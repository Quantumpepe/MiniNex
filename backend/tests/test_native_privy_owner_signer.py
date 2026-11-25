"""Native Privy owner + policy reference never equals autonomous trade authority."""
import asyncio
import copy

import pytest
from fastapi import HTTPException

from backend import native_privy_owner_signer as auth
from backend.tests.test_native_btc_thor_outputs import SOURCE
from backend.tests.test_native_xrp_thor_payment_tx import OWNER as XRP
from backend.tests.test_native_sol_compiled_message import b58
from backend.native_trx_user_signed_send import _tron_address_from_public_key
from eth_keys import keys

DID = "did:privy:native_readback_user"
SOL = b58(bytes([5])*32)
TRX = _tron_address_from_public_key(keys.PrivateKey(bytes([1])*32).public_key.to_bytes())
_WALLETS = {
    "BTC": (SOURCE, "bitcoin-segwit", "btc_wallet_owner_0001"),
    "SOL": (SOL, "solana", "sol_wallet_owner_0001"),
    "XRP": (XRP, "xrpl", "xrp_wallet_owner_0001"),
    "TRX": (TRX, "tron", "trx_wallet_owner_0001"),
}


def user(asset, *, delegated=True):
    address, network, wallet_id = _WALLETS[asset]
    return {
        "id": DID,
        "linked_accounts": [{
            "type": "wallet", "chain_type": network,
            "wallet_client_type": "privy", "id": wallet_id,
            "address": address, "delegated": delegated,
        }],
    }


def remote(asset):
    address, network, wallet_id = _WALLETS[asset]
    return {
        "id": wallet_id, "chain_type": network,
        "address": address,
        "entity": {"type": "user", "id": DID},
        "archived_at": None,
        "additional_signers": [{
            "signer_id": "mininex_executor_001",
            "override_policy_ids": ["native_owner_policy_001"],
        }],
    }


@pytest.mark.parametrize("asset", ["BTC", "SOL", "XRP", "TRX"])
def test_original_privy_native_wallet_and_remote_owner_ref_verified_only(asset):
    u = user(asset)
    original = auth._original_privy_native_wallet(u, DID, asset)
    assert original["wallet_id"] == _WALLETS[asset][2]
    status = auth.inspect_original_native_signer_metadata(
        did=DID, asset=asset, linked=u, remote=remote(asset),
    )
    assert status["original_wallet_link_verified"] is True
    assert status["original_wallet_remote_owner_verified"] is True
    assert status["additional_signer_reference_present"] is True
    assert status["signer_policy_reference_present"] is True
    for flag in (
        "exact_remote_policy_contents_verified",
        "per_user_owner_trade_consent_verified",
        "original_budget_revision_verified",
        "privy_transaction_signing_capability_verified",
        "original_order_claim_verified",
        "automated_signing_enabled",
        "live_trading_enabled", "execution_enabled", "can_submit",
    ):
        assert status[flag] is False
    assert "native_owner_policy_001" not in str(status)
    assert "mininex_executor_001" not in str(status)
    assert _WALLETS[asset][2] not in str(status)


@pytest.mark.parametrize("asset", ["BTC", "SOL", "XRP", "TRX"])
def test_native_wallet_must_have_exact_unique_privy_identity(asset):
    u = user(asset)
    assert auth._original_privy_native_wallet(u, DID, asset)
    cases = [
        {**u, "id": "did:privy:wrong"},
        {**u, "linked_accounts": []},
        {**u, "linked_accounts": u["linked_accounts"] * 2},
        {**u, "linked_accounts": [{**u["linked_accounts"][0],
             "wallet_client_type": "metamask"}]},
        {**u, "linked_accounts": [{**u["linked_accounts"][0],
             "id": "../../forbidden"}]},
        {**u, "linked_accounts": [{**u["linked_accounts"][0],
             "address": "invalid"}]},
    ]
    for bad in cases:
        with pytest.raises(ValueError):
            auth._original_privy_native_wallet(bad, DID, asset)


@pytest.mark.parametrize("asset", ["BTC", "SOL", "XRP", "TRX"])
def test_wrong_wallet_owner_or_unrestricted_remote_signer_fails_closed(asset):
    u = user(asset)
    valid = remote(asset)
    variants = [
        {**valid, "id": "other_wallet"},
        {**valid, "address": "wrong_address"},
        {**valid, "chain_type": "ethereum"},
        {**valid, "entity": {"type":"user","id":"did:privy:wrong"}},
        {**valid, "entity": None},
        {**valid, "entity": {"type":"organization","id":DID}},
        {**valid, "archived_at": 1763660000000},
        {**valid, "owner": {"user_id": DID}, "entity": {"type": "user", "id":"did:privy:other"}},
        {**valid, "additional_signers": None},
        {**valid, "additional_signers": [{"signer_id": "native_executor_001"}]},
        {**valid, "additional_signers": [{
            "signer_id": "native_executor_001", "override_policy_ids": []}]},
        {**valid, "additional_signers": [{
            "signer_id": "native_executor_001",
            "override_policy_ids": ["p1", "p2"]}]},
        {**valid, "additional_signers": [
            valid["additional_signers"][0], valid["additional_signers"][0]]},
        {**valid, "additional_signers": [{"signer_id": "../../bad",
           "override_policy_ids": ["native_owner_policy_001"]}]},
    ]
    for wrong in variants:
        with pytest.raises(ValueError):
            auth.inspect_original_native_signer_metadata(
                did=DID, asset=asset, linked=u, remote=wrong,
            )


def test_unconfigured_native_signer_is_not_equivalent_to_privy_permission():
    result = auth.inspect_original_native_signer_metadata(
        did=DID, asset="SOL",
        linked=user("SOL"), remote={**remote("SOL"), "additional_signers": []},
    )
    assert result["original_wallet_remote_owner_verified"] is True
    assert result["additional_signer_reference_present"] is False
    assert result["exact_remote_policy_contents_verified"] is False
    assert result["automated_signing_enabled"] is False


def test_native_privy_owner_status_non_delegated_does_not_read_wallet(monkeypatch):
    async def current_user(did):
        assert did == DID
        return user("SOL", delegated=False)
    async def unexpected(_):
        pytest.fail("Do not imply a signer when original user never delegated")
    monkeypatch.setattr(auth, "fetch_privy_user", current_user)
    monkeypatch.setattr(auth, "fetch_privy_wallet_metadata", unexpected)
    result = asyncio.run(auth.get_original_native_privy_signer_status(
        DID, asset="SOL"))
    assert result["original_wallet_link_verified"] is True
    assert result["wallet_delegation_flag_reported"] is False
    assert result["original_wallet_remote_owner_verified"] is False
    assert result["can_submit"] is False


def test_native_privy_owner_status_checks_wallet_by_verified_id(monkeypatch):
    calls = []
    async def current_user(did):
        calls.append(("user", did))
        return user("XRP")
    async def current_wallet(wallet_id):
        calls.append(("wallet", wallet_id))
        return remote("XRP")
    monkeypatch.setattr(auth, "fetch_privy_user", current_user)
    monkeypatch.setattr(auth, "fetch_privy_wallet_metadata", current_wallet)
    result = asyncio.run(auth.get_original_native_privy_signer_status(
        DID, asset="XRP"))
    assert calls == [("user", DID), ("wallet", _WALLETS["XRP"][2])]
    assert result["signer_policy_reference_present"] is True
    assert result["execution_enabled"] is False


def test_native_privy_wrong_owner_or_failed_api_still_has_no_approval(monkeypatch):
    async def current_user(did):
        return user("BTC")
    async def mismatched(_id):
        return {**remote("BTC"), "entity": {"type":"user", "id":"did:privy:another"}}
    monkeypatch.setattr(auth, "fetch_privy_user", current_user)
    monkeypatch.setattr(auth, "fetch_privy_wallet_metadata", mismatched)
    result = asyncio.run(auth.get_original_native_privy_signer_status(
        DID, asset="BTC"))
    assert result["original_wallet_link_verified"] is True
    assert result["original_wallet_remote_owner_verified"] is False
    assert result["can_submit"] is False

    async def unavailable(_id):
        raise HTTPException(503, "Privy temporarily offline")
    monkeypatch.setattr(auth, "fetch_privy_wallet_metadata", unavailable)
    result = asyncio.run(auth.get_original_native_privy_signer_status(
        DID, asset="BTC"))
    assert result["original_wallet_remote_owner_verified"] is False
    assert result["live_trading_enabled"] is False


def test_native_privy_unknown_asset_rejected_without_lookup():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.get_original_native_privy_signer_status(
            DID, asset="ADA"))
    assert exc.value.status_code == 422


def test_native_authorization_readback_route_is_authenticated_and_read_only(monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app as api
    seen = []

    def auth_header(header):
        if header != "Bearer native_test":
            raise HTTPException(401, "Unauthorized")
        return {"user_id": DID}

    async def status(did, *, asset):
        seen.append((did, asset))
        return {
            "mode": "PRIVY_ORIGINAL_NATIVE_OWNER_READBACK_ONLY",
            "asset": asset, "live_trading_enabled": False,
            "execution_enabled": False, "can_submit": False,
        }
    monkeypatch.setattr(api, "verify_privy_bearer", auth_header)
    monkeypatch.setattr(
        api, "get_original_native_privy_signer_status", status)
    client = TestClient(api.app)
    for coin in ("BTC", "SOL", "XRP", "TRX"):
        path = "/api/live/native/authorization/" + coin
        assert client.get(path).status_code == 401
        assert client.get(path, headers={
            "Authorization": "Bearer invalid"}).status_code == 401
        response = client.get(path, headers={
            "Authorization": "Bearer native_test"})
        assert response.status_code == 200
        assert response.json()["asset"] == coin
        assert response.json()["can_submit"] is False
        assert response.headers["cache-control"] == "private, no-store"
        assert client.post(path, headers={
            "Authorization": "Bearer native_test"}).status_code == 405
    assert seen == [(DID, asset) for asset in ("BTC", "SOL", "XRP", "TRX")]
    assert client.get("/api/live/native/authorization/ADA",
        headers={"Authorization": "Bearer native_test"}).status_code == 422


def test_legacy_owner_user_id_cannot_replace_documented_entity(monkeypatch):
    """Old fixture shape must not yield a forged remote wallet ownership proof."""
    candidate = remote("BTC")
    candidate.pop("entity")
    candidate["owner"] = {"user_id": DID}
    with pytest.raises(ValueError, match="another owner"):
        auth.inspect_original_native_signer_metadata(
            did=DID,asset="BTC",linked=user("BTC"),remote=candidate,
        )


def test_current_privy_wallet_metadata_entity_shape_is_required():
    for asset in ("BTC","SOL"):
        meta = remote(asset)
        assert meta["entity"] == {"type":"user","id":DID}
        result = auth.inspect_original_native_signer_metadata(
            did=DID,asset=asset,linked=user(asset),remote=meta,
        )
        assert result["original_wallet_remote_owner_verified"] is True
        assert result["exact_remote_policy_contents_verified"] is False


def test_remote_native_pubkey_binding_is_independent_of_delegation_and_policy():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import hashlib
    from backend.tests.test_native_btc_thor_outputs import _reference_address

    key = ec.derive_private_key(4711, ec.SECP256K1()).public_key()
    pub = key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.CompressedPoint,
    )
    program = hashlib.new(
        "ripemd160", hashlib.sha256(pub).digest(),
    ).digest()
    address = _reference_address(program.hex())
    linked = user("BTC")
    linked["linked_accounts"][0]["address"] = address
    current = remote("BTC")
    current["address"] = address
    current["public_key"] = "0x" + pub.hex()
    r = auth.inspect_original_native_signer_metadata(
        did=DID,asset="BTC",linked=linked,remote=current)
    assert r["original_public_key_matches_wallet"] is True
    assert r["privy_native_wallet_api_supported"] is True
    assert r["exact_remote_policy_contents_verified"] is False
    assert r["can_submit"] is False

    current["public_key"] = "02" + "aa"*32
    with pytest.raises(ValueError):
        auth.inspect_original_native_signer_metadata(
            did=DID,asset="BTC",linked=linked,remote=current)


def test_missing_remote_pubkey_is_visible_as_unknown_not_accepted_as_verified():
    r = auth.inspect_original_native_signer_metadata(
        did=DID,asset="SOL",linked=user("SOL"),remote=remote("SOL"))
    assert r["original_public_key_matches_wallet"] is False
    assert r["privy_native_wallet_api_supported"] is True
    assert r["privy_transaction_signing_capability_verified"] is False


def test_xrp_mocked_chain_type_is_not_claimed_as_supported_by_privy_api():
    result = auth.inspect_original_native_signer_metadata(
        did=DID,asset="XRP",linked=user("XRP"),remote=remote("XRP"))
    assert result["privy_native_wallet_api_supported"] is False
    assert result["privy_transaction_signing_capability_verified"] is False
    assert result["live_trading_enabled"] is False
