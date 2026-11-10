"""Read-only Privy ETH additional signer and policy guardrail tests."""
import asyncio
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import privy_live_signer as verifier

DID = "did:privy:signer-check"
WALLET_ID = "wallet_eth_server_test_00001"
WALLET = "0x" + "a" * 40
SIGNER = "quorum_test_eth_00001"
POLICY = "policy_eth_restricted_00001"


def privy_user(*, did=DID, delegated=True, wallet_id=WALLET_ID, address=WALLET):
    return {"id": did, "linked_accounts": [{
        "type": "wallet",
        "chain_type": "ethereum",
        "wallet_client_type": "privy",
        "id": wallet_id, "address": address,
        "delegated": delegated,
    }]}


def privy_wallet(*, address=WALLET, owner=DID,
                 signer=SIGNER, policy=POLICY):
    return {
        "id": WALLET_ID,
        "address": address,
        "chain_type": "ethereum",
        "owner": {"user_id": owner},
        "additional_signers": [{
            "signer_id": signer, "override_policy_ids": [policy],
        }],
    }


@pytest.fixture
def ready_config(monkeypatch):
    monkeypatch.setenv("MININEX_PRIVY_ETH_SIGNER_ID", SIGNER)
    monkeypatch.setenv("MININEX_PRIVY_ETH_POLICY_ID", POLICY)
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET", "test-private-app-secret")


@pytest.fixture
def headers(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", pub)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "signer-check-session",
        "iat": now, "exp": now + 600,
    }, key, algorithm="ES256")
    return {"Authorization": "Bearer " + token}


def test_no_signer_configuration_never_queries_privy(monkeypatch):
    monkeypatch.delenv("MININEX_PRIVY_ETH_SIGNER_ID", raising=False)
    monkeypatch.delenv("MININEX_PRIVY_ETH_POLICY_ID", raising=False)
    assert verifier.signer_configuration() is None
    result = asyncio.run(verifier.get_eth_signer_status(DID))
    assert result["signer_policy_configured"] is False
    assert result["signer_verified"] is False
    assert result["execution_enabled"] is False


def test_ambiguous_or_untrusted_wallets_fail_closed():
    assert verifier._owned_eth_wallet(privy_user(), DID) == {
        "wallet_id": WALLET_ID, "address": WALLET, "delegated": True,
    }
    for data in (
        privy_user(did="did:privy:another"),
        privy_user(wallet_id="../../steal"),
        privy_user(wallet_id=None),
        privy_user(address="0x123"),
        {"id": DID, "linked_accounts": []},
        {"id": DID, "linked_accounts": None},
        {"id": DID, "linked_accounts": [*privy_user()["linked_accounts"],
                                       *privy_user()["linked_accounts"]]},
        {"id": DID, "linked_accounts": [
            {**privy_user()["linked_accounts"][0], "wallet_client_type": "metamask"}]},
    ):
        with pytest.raises(ValueError):
            verifier._owned_eth_wallet(data, DID)


def test_signer_must_be_exact_quorum_with_exact_override_policy(ready_config):
    config = verifier.signer_configuration()
    assert config == {"signer_id": SIGNER, "policy_id": POLICY}
    assert verifier._scoped_signer(
        privy_wallet(), did=DID, address=WALLET, config=config) is True
    bad = [
        privy_wallet(owner="did:privy:other"),
        privy_wallet(address="0x" + "b" * 40),
        {**privy_wallet(), "chain_type": "solana"},
        {**privy_wallet(), "owner": None},
        {**privy_wallet(), "additional_signers": []},
        {**privy_wallet(), "additional_signers": [{"signer_id": SIGNER}]},
        {**privy_wallet(), "additional_signers": [{
            "signer_id": SIGNER, "override_policy_ids": []}]},
        {**privy_wallet(), "additional_signers": [{
            "signer_id": SIGNER, "override_policy_ids": ["other"]}]},
        {**privy_wallet(), "additional_signers": [{
            "signer_id": SIGNER, "override_policy_ids": [POLICY, "extra"]}]},
        {**privy_wallet(), "additional_signers": [
            {"signer_id": SIGNER, "override_policy_ids": [POLICY]},
            {"signer_id": SIGNER, "override_policy_ids": [POLICY]},
        ]},
    ]
    assert all(not verifier._scoped_signer(
        item, did=DID, address=WALLET, config=config) for item in bad)


def test_server_observed_grant_and_policy_still_do_not_enable_trades(
        monkeypatch, ready_config):
    calls = []
    async def user(did):
        calls.append(("user", did))
        return privy_user()
    async def wallet(wallet_id):
        calls.append(("wallet", wallet_id))
        return privy_wallet()
    monkeypatch.setattr(verifier, "fetch_privy_user", user)
    monkeypatch.setattr(verifier, "fetch_privy_wallet_metadata", wallet)
    outcome = asyncio.run(verifier.get_eth_signer_status(DID))
    assert calls == [("user", DID), ("wallet", WALLET_ID)]
    assert outcome["wallet_delegated"] is True
    assert outcome["signer_verified"] is True
    assert outcome["policy_verified"] is True
    assert outcome["trading_approved"] is False
    assert outcome["automated_signing_enabled"] is False
    assert outcome["execution_enabled"] is False
    assert SIGNER not in str(outcome)
    assert POLICY not in str(outcome)


def test_user_not_delegated_or_wrong_policy_fails_closed(monkeypatch, ready_config):
    async def user(did):
        return privy_user(delegated=False)
    async def wallet(_id):
        pytest.fail("Must not fetch metadata for non-delegated wallet")
    monkeypatch.setattr(verifier, "fetch_privy_user", user)
    monkeypatch.setattr(verifier, "fetch_privy_wallet_metadata", wallet)
    status = asyncio.run(verifier.get_eth_signer_status(DID))
    assert status["wallet_delegated"] is False
    assert status["signer_verified"] is False

    async def delegated(did):
        return privy_user()
    async def wrong(_id):
        return privy_wallet(policy="unrestricted_policy")
    monkeypatch.setattr(verifier, "fetch_privy_user", delegated)
    monkeypatch.setattr(verifier, "fetch_privy_wallet_metadata", wrong)
    status = asyncio.run(verifier.get_eth_signer_status(DID))
    assert status["wallet_delegated"] is True
    assert status["policy_verified"] is False
    assert status["signer_verified"] is False


def test_server_wallet_lookup_never_follows_redirect_or_accepts_wrong_id(
        monkeypatch, ready_config):
    original_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        def response(request):
            assert str(request.url) == verifier._PRIVY_WALLET_API + WALLET_ID
            assert request.headers["privy-app-id"] == verifier.APP_ID
            assert kwargs.get("follow_redirects") is False
            return httpx.Response(200, json=privy_wallet())
        return original_client(*args, transport=httpx.MockTransport(response), **kwargs)

    monkeypatch.setattr(verifier.httpx, "AsyncClient", make_client)
    result = asyncio.run(verifier.fetch_privy_wallet_metadata(WALLET_ID))
    assert result["id"] == WALLET_ID

    with pytest.raises(HTTPException):
        asyncio.run(verifier.fetch_privy_wallet_metadata("../other"))

    def spoofed_client(*args, **kwargs):
        def response(_):
            return httpx.Response(200, json={**privy_wallet(), "id": "wallet_elsewhere"})
        return original_client(*args, transport=httpx.MockTransport(response), **kwargs)
    monkeypatch.setattr(verifier.httpx, "AsyncClient", spoofed_client)
    with pytest.raises(HTTPException) as error:
        asyncio.run(verifier.fetch_privy_wallet_metadata(WALLET_ID))
    assert error.value.status_code == 503


def test_readonly_status_route_requires_auth_and_never_grants_signing(
        monkeypatch, headers):
    seen = []
    async def status(did):
        seen.append(did)
        return {"signer_verified": False, "policy_verified": False,
                "automated_signing_enabled": False, "execution_enabled": False}
    monkeypatch.setattr(api, "get_eth_signer_status", status)
    client = TestClient(api.app)
    assert client.get("/api/live/eth/authorization").status_code == 401
    assert client.get("/api/live/eth/authorization",
                      headers={"Authorization": "Bearer invalid"}).status_code == 401
    response = client.get("/api/live/eth/authorization", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert seen == [DID]
    assert response.json()["automated_signing_enabled"] is False
    assert client.post("/api/live/eth/authorization", headers=headers).status_code == 405
    assert not any(route.path == "/api/live/eth/authorize" for route in api.app.routes)
