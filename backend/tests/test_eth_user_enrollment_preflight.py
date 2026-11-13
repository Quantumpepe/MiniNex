"""Privy per-user ETH enrollment evidence is checked, never granted."""
import asyncio
from copy import deepcopy
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_user_enrollment_preflight as enrollment

DID = "did:privy:eth-enrollment-test"
OTHER_DID = "did:privy:some-other-user"
WALLET = "0x" + "a" * 40
WALLET_ID = "wallet_enrollment_sample_id"
OWNER_ID = "quorum_mininex_policy_owner"
SIGNER_ID = "quorum_mininex_backend_signer"


def user(*, did=DID, address=WALLET):
    return {
        "id": did,
        "linked_accounts": [{
            "id": WALLET_ID, "type": "wallet",
            "wallet_client_type": "privy",
            "chain_type": "ethereum",
            "address": address,
            "delegated": False,
        }],
    }


def wallet(*, did=DID, address=WALLET, signers=None):
    return {
        "id": WALLET_ID,
        "chain_type": "ethereum",
        "address": address,
        "owner": {"user_id": did},
        "additional_signers": [] if signers is None else signers,
    }


def limits(*, capital="5000", order="5000", daily="15000",
           revision=3, paused=True):
    return {
        "planned_capital_usd": capital,
        "max_order_usd": order,
        "max_daily_turnover_usd": daily,
        "revision": revision,
        "trading_paused": paused,
        "mode": "LIVE_LIMITS_PREPARATION_ONLY",
    }


def review(u=None, w=None, limit=None, owner=OWNER_ID, executor=SIGNER_ID):
    return enrollment.assess_eth_enrollment_evidence(
        did=DID,
        privy_user=user() if u is None else u,
        wallet_metadata=wallet() if w is None else w,
        live_limits=limits() if limit is None else limit,
        policy_owner_id=owner,
        executor_signer_id=executor,
    )


def test_user_owned_privy_wallet_and_five_thousand_live_capital_are_separate():
    report = review()
    assert report["verified_wallet_address"] == WALLET
    assert report["wallet_identity_verified"] is True
    assert report["wallet_owned_by_authenticated_user"] is True
    assert report["policy_owner_independent"] is True
    assert report["user_live_limits_verified"] is True
    assert report["live_capital_usd"] == "5000"
    assert report["live_max_order_usd"] == "5000"
    assert report["live_daily_turnover_usd"] == "15000"
    assert report["live_limits_nonzero"] is True
    assert report["live_limits_revision"] == 3
    assert report["trading_paused"] is True
    assert report["user_consent_signed_and_nonce_verified"] is False
    assert report["fresh_usd_to_atomic_token_caps_verified"] is False
    assert report["complete_privy_policy_generated"] is False
    assert report["policy_created"] is False
    assert report["additional_signer_enrolled"] is False
    assert report["can_grant_permission"] is False
    assert report["can_sign"] is False
    assert report["can_submit"] is False
    assert report["execution_enabled"] is False
    assert "rules" not in report
    assert "private_key" not in report


@pytest.mark.parametrize("bad_user,bad_wallet", [
    ({"id": OTHER_DID, "linked_accounts": user()["linked_accounts"]}, None),
    ({"id": DID, "linked_accounts": []}, None),
    ({"id": DID, "linked_accounts": user()["linked_accounts"] * 2}, None),
    (None, {"owner": {"user_id": DID}}),
    (None, {"owner": {"user_id": OTHER_DID}}),
    (None, {"address": "0x" + "b" * 40}),
    (None, {"chain_type": "solana"}),
    (None, {"id": "wallet_unrelated_id"}),
    (None, {"owner": None}),
])
def test_wrong_did_missing_or_duplicated_wallet_and_fake_metadata_are_blocked(
    bad_user, bad_wallet,
):
    supplied_wallet = wallet()
    if bad_wallet:
        supplied_wallet.update(bad_wallet)
    result = review(
        u=user() if bad_user is None else bad_user,
        w=supplied_wallet,
    )
    assert result["wallet_identity_verified"] is False
    assert result["verified_wallet_address"] is None
    assert result["can_grant_permission"] is False


@pytest.mark.parametrize("bad_owner,bad_signer", [
    (SIGNER_ID, SIGNER_ID),
    ("", SIGNER_ID),
    ("invalid owner id", SIGNER_ID),
    (OWNER_ID, OWNER_ID),
    (OWNER_ID, ""),
    (OWNER_ID, "not valid id"),
])
def test_missing_or_same_policy_owner_and_backend_executor_cannot_authorize(
    bad_owner, bad_signer,
):
    result = review(owner=bad_owner, executor=bad_signer)
    assert result["policy_owner_independent"] is False
    assert result["can_grant_permission"] is False


@pytest.mark.parametrize("limit", [
    limits(capital="0", order="0", daily="0"),
    limits(capital="5000", order="6000", daily="10000"),
    limits(capital="5000", order="0", daily="10000"),
    limits(capital="abc"),
    limits(revision=-1),
    limits(revision=True),
])
def test_zero_or_invalid_live_limits_never_enable_signing(limit):
    result = review(limit=limit)
    assert not (result["user_live_limits_verified"]
                and result["live_limits_nonzero"]) or result["trading_paused"]
    assert result["can_grant_permission"] is False
    assert result["execution_enabled"] is False


def test_even_unpaused_live_limits_are_never_grant_or_user_signed_consent():
    result = review(limit=limits(paused=False))
    assert result["trading_paused"] is False
    assert result["user_consent_signed_and_nonce_verified"] is False
    assert result["wallet_delegation_enabled"] is False
    assert result["can_grant_permission"] is False
    assert result["can_submit"] is False


def test_unexpected_additional_signers_are_flagged_not_assumed_safe():
    unexpected = wallet(signers=[{
        "signer_id": "attacker_signer",
        "override_policy_ids": [],
    }])
    result = review(w=unexpected)
    assert result["wallet_identity_verified"] is True
    assert result["wallet_additional_signers_reviewed"] is True
    assert result["unrecognized_existing_signers"] is True
    known_id_without_policy = wallet(signers=[{
        "signer_id": SIGNER_ID,
        "override_policy_ids": [],
    }])
    assert review(w=known_id_without_policy)["unrecognized_existing_signers"] is True
    with_policy = wallet(signers=[{
        "signer_id": SIGNER_ID,
        "override_policy_ids": ["policy_mininex_restricted"],
    }])
    assert review(w=with_policy)["unrecognized_existing_signers"] is True
    assert review(w=with_policy)["user_consent_signed_and_nonce_verified"] is False
    assert review(w=with_policy)["can_grant_permission"] is False


def test_async_preflight_only_uses_privy_server_ids_and_db(monkeypatch):
    observed = []
    async def privy_user(did):
        observed.append(("privy-user", did))
        return user()
    async def privy_wallet(wallet_id):
        observed.append(("privy-wallet", wallet_id))
        return wallet()
    def db_limits(did):
        observed.append(("db-limits", did))
        return limits()
    monkeypatch.setattr(enrollment, "fetch_privy_user", privy_user)
    monkeypatch.setattr(enrollment, "fetch_privy_wallet_metadata", privy_wallet)
    monkeypatch.setattr(enrollment, "get_live_eth_limits", db_limits)
    monkeypatch.setenv("MININEX_PRIVY_ETH_POLICY_OWNER_ID", OWNER_ID)
    monkeypatch.setenv("MININEX_PRIVY_ETH_SIGNER_ID", SIGNER_ID)
    result = asyncio.run(enrollment.get_eth_user_enrollment_preflight(DID))
    assert observed == [
        ("privy-user", DID),
        ("privy-wallet", WALLET_ID),
        ("db-limits", DID),
    ]
    assert result["wallet_identity_verified"] is True
    assert result["user_live_limits_verified"] is True
    assert result["can_grant_permission"] is False


def test_malformed_user_prevents_wallet_and_db_lookup(monkeypatch):
    async def privy_user(did):
        return {"id": did, "linked_accounts": []}
    async def never_wallet(wallet_id):
        raise AssertionError("Unverified wallet should not be fetched")
    def never_db(did):
        raise AssertionError("Unverified wallet should not reach DB lookup")
    monkeypatch.setattr(enrollment, "fetch_privy_user", privy_user)
    monkeypatch.setattr(enrollment, "fetch_privy_wallet_metadata", never_wallet)
    monkeypatch.setattr(enrollment, "get_live_eth_limits", never_db)
    result = asyncio.run(enrollment.get_eth_user_enrollment_preflight(DID))
    assert result["wallet_identity_verified"] is False
    assert result["execution_enabled"] is False


@pytest.fixture
def auth_headers(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    verification_key = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", verification_key)
    now = int(time.time())
    payload = {
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "enrollment-preview", "iat": now,
        "exp": now + 600,
    }
    token = jwt.encode(payload, key, algorithm="ES256")
    return {"Authorization": "Bearer " + token}


def test_authenticated_preflight_endpoint_is_read_only(auth_headers, monkeypatch):
    async def prepared(did):
        assert did == DID
        return review()
    monkeypatch.setattr(api, "get_eth_user_enrollment_preflight", prepared)
    client = TestClient(api.app)
    path = "/api/live/eth/enrollment-preflight"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    response = client.get(path, headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json()["wallet_identity_verified"] is True
    assert response.json()["can_grant_permission"] is False
    assert client.post(path, headers=auth_headers).status_code == 405
    assert client.put(path, headers=auth_headers).status_code == 405
    paths = {route.path for route in api.app.routes}
    assert "/api/live/eth/create-policy" not in paths
    assert "/api/live/eth/privy-enroll" not in paths
    assert "/api/live/eth/start-auto" not in paths
