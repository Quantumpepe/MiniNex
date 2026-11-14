"""Stage 20 Privy transport checks. Every HTTP request is local MockTransport."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os

import httpx
import pytest
from fastapi import HTTPException

from backend import app as api
from backend import eth_privy_policy_transport as transport
from backend import eth_privy_policy_plan as planner

DID = "did:privy:stage20-owner-verified"
WALLET = "0x" + "a" * 40
OWNER = "mininex_policy_owner_quorum"
EXECUTOR = "mininex_backend_signer_quorum"
NOW = 1_900_000_000
CAPS = {
    "usdc_max_input_micro": "5000000000",
    "usdt_max_input_micro": "5000000000",
    "weth_max_input_wei": "1666666666666666666",
    "native_wrap_max_wei": "1666666666666666666",
}


def valid_request(*, wallet=WALLET, revision=4):
    from backend.eth_privy_user_policy_blueprint import (
        build_eth_privy_user_policy_blueprint,
    )
    policy = build_eth_privy_user_policy_blueprint(
        wallet=wallet, policy_owner_id=OWNER,
        executor_signer_id=EXECUTOR,
        expires_at=NOW+86400, now=NOW, **CAPS,
    )
    data = transport._validate_candidate(
        policy, did=DID, wallet=wallet,
        policy_owner_id=OWNER, executor_signer_id=EXECUTOR,
        limits_revision=revision,
        expires_at=NOW+86400, now=NOW, **CAPS,
    )
    return data


def remote_body(request):
    policy = deepcopy(request["candidate"])
    policy["id"] = "policy_stage20_mock_001"
    policy["created_at"] = NOW*1000
    for index, rule in enumerate(policy["rules"]):
        rule["id"] = f"rule_stage20_{index+1:03d}"
    return policy


def test_request_is_owned_by_independent_quorum_and_wallet_revision():
    prepared = valid_request()
    assert len(prepared["idempotency_key"]) == 64
    assert prepared["candidate"]["owner_id"] == OWNER
    assert prepared["candidate_sha256"] == transport._digest(
        prepared["candidate"]
    )
    assert prepared["candidate"]["version"] == "1.0"
    assert all(
        rule["method"] == "eth_sendTransaction"
        for rule in prepared["candidate"]["rules"]
    )
    assert valid_request(revision=5)["idempotency_key"] != prepared["idempotency_key"]
    assert valid_request(wallet="0x"+"b"*40)["idempotency_key"] != prepared["idempotency_key"]
    assert valid_request() == prepared


@pytest.mark.parametrize("change", [
    lambda p: p["rules"].append({
        "name": "all", "method": "*", "action": "ALLOW", "conditions": [],
    }),
    lambda p: p.update(owner_id=EXECUTOR),
    lambda p: p.update(owner_id=None),
    lambda p: p["rules"][0]["conditions"].pop(0),
    lambda p: p["rules"][0]["conditions"].pop(-1),
    lambda p: p["rules"][0]["conditions"][1].update(value="0x"+"f"*40),
    lambda p: p["rules"][0]["conditions"][7].update(value="0x"+"f"*40),
    lambda p: p["rules"][0]["conditions"][-1].update(value="0"),
    lambda p: p["rules"][0].update(method="eth_signTypedData_v4"),
    lambda p: p["rules"][0].update(action="DENY"),
])
def test_unreviewed_privy_payload_cannot_assemble_a_request(change):
    request = valid_request()
    policy = deepcopy(request["candidate"])
    change(policy)
    with pytest.raises(ValueError):
        transport._validate_candidate(
            policy, did=DID, wallet=WALLET,
            policy_owner_id=OWNER, executor_signer_id=EXECUTOR,
            limits_revision=4, expires_at=NOW+86400, now=NOW, **CAPS,
        )


@pytest.mark.parametrize("change", [
    lambda p: p["rules"].append(deepcopy(p["rules"][0])),
    lambda p: p["rules"][0]["conditions"].pop(0),
    lambda p: p["rules"][0]["conditions"].append({
        "field_source": "ethereum_transaction", "field": "to",
        "operator": "in", "value": ["0x"+"f"*40],
    }),
    lambda p: p.update(owner_id=None),
    lambda p: p.update(owner_id=EXECUTOR),
    lambda p: p.update(chain_type="solana"),
    lambda p: p["rules"][0].update(action="DENY"),
    lambda p: p["rules"][0].update(method="*"),
    lambda p: p["rules"][0]["conditions"][-1].update(value="0"),
    lambda p: p["rules"][0].update(id=p["rules"][1]["id"]),
    lambda p: p["rules"][0].pop("id"),
    lambda p: p["rules"][0].update(unsafe_additional_field=True),
    lambda p: p.update(id="wrong!"),
])
def test_privy_readback_rejects_mutated_policy_after_creation(change):
    request = valid_request()
    result = remote_body(request)
    change(result)
    with pytest.raises(ValueError):
        transport.verify_remote_privy_policy(result, request["candidate"])


def test_exact_privy_readback_keeps_writes_and_wallet_signing_disabled():
    request = valid_request()
    result = transport.verify_remote_privy_policy(
        remote_body(request), request["candidate"],
    )
    assert result["policy_id"] == "policy_stage20_mock_001"
    assert result["remote_exact_match"] is True
    assert result["policy_sha256"] == request["candidate_sha256"]
    assert result["wallet_signer_attached"] is False
    assert result["user_delegation_verified"] is False
    assert result["can_sign"] is False
    assert result["can_submit"] is False
    assert result["execution_enabled"] is False


def test_privy_mock_post_get_uses_exact_owner_payload_and_idempotency(
    monkeypatch,
):
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET", "local-only-test-secret")
    prepared = valid_request()
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "api.privy.io"
        assert request.headers["privy-app-id"] == transport.APP_ID
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "POST":
            assert request.url.path == "/v1/policies"
            assert request.headers["privy-idempotency-key"] == prepared["idempotency_key"]
            assert json.loads(request.content) == prepared["candidate"]
            assert json.loads(request.content)["owner_id"] == OWNER
            return httpx.Response(200, json={"id": "policy_stage20_mock_001"})
        assert request.method == "GET"
        assert request.url.path == "/v1/policies/policy_stage20_mock_001"
        assert "privy-idempotency-key" not in request.headers
        return httpx.Response(200, json=remote_body(prepared))
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=5, follow_redirects=False,
        ) as client:
            return await transport._post_and_readback_unattached(
                validated=prepared, client=client,
            )
    result = asyncio.run(run())
    assert result["remote_exact_match"] is True
    assert [r.method for r in requests] == ["POST", "GET"]
    assert result["can_submit"] is False


def test_incorrect_remote_owner_fails_even_after_successful_post(monkeypatch):
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET", "local-only-test-secret")
    prepared = valid_request()
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(201, json={"id": "policy_stage20_mock_001"})
        wrong = remote_body(prepared)
        wrong["owner_id"] = EXECUTOR
        return httpx.Response(200, json=wrong)
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=5, follow_redirects=False,
        ) as client:
            return await transport._post_and_readback_unattached(
                validated=prepared, client=client,
            )
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 503
    assert calls == ["POST", "GET"]


def test_request_mismatch_denied_before_any_external_http(monkeypatch):
    prepared = valid_request()
    prepared["candidate"]["rules"][0]["action"] = "DENY"
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=5, follow_redirects=False,
        ) as client:
            return await transport._post_and_readback_unattached(
                validated=prepared, client=client,
            )
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert calls == []


def test_network_timeout_fails_closed_with_exactly_one_post(monkeypatch):
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET", "local-only-test-secret")
    prepared = valid_request()
    calls = []
    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("mocked timeout")
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=5, follow_redirects=False,
        ) as client:
            return await transport._post_and_readback_unattached(
                validated=prepared, client=client,
            )
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 503
    assert calls == ["POST"]


def test_missing_server_secret_does_not_send_post(monkeypatch):
    monkeypatch.delenv("MININEX_PRIVY_APP_SECRET", raising=False)
    prepared = valid_request()
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={})
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=5, follow_redirects=False,
        ) as client:
            return await transport._post_and_readback_unattached(
                validated=prepared, client=client,
            )
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 503
    assert calls == []


def test_real_provisioning_entrypoint_and_wallet_grants_are_still_disabled():
    assert transport._REMOTE_PROVISIONING_ACTIVATED is False
    with pytest.raises(HTTPException) as error:
        transport.provision_eth_privy_policy(
            did=DID, wallet=WALLET, signed_intent=True,
        )
    assert error.value.status_code == 503
    paths = {route.path for route in api.app.routes}
    for forbidden in (
        "/api/live/eth/create-policy",
        "/api/live/eth/grant",
        "/api/live/eth/attach-signer",
        "/api/live/eth/delegate",
        "/api/live/eth/execute",
    ):
        assert forbidden not in paths
