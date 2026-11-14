"""Stage 19 ETH Privy policy previews: never grant or submit a transaction."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_privy_policy_plan as planner
from backend.eth_privy_user_policy_blueprint import inspect_eth_user_policy_blueprint

DID = "did:privy:stage19-verified-user"
WALLET = "0x" + "a" * 40
OTHER_WALLET = "0x" + "b" * 40
OWNER = "owner_quorum_ABC123456"
SIGNER = "backend_signer_quorum_ABC123456"
NOW = 1_900_000_000


def verified(wallet=WALLET, *, order="5000", revision=3, **changes):
    values = {
        "wallet_identity_verified": True,
        "wallet_owned_by_authenticated_user": True,
        "wallet_chain_verified": True,
        "wallet_additional_signers_reviewed": True,
        "unrecognized_existing_signers": False,
        "policy_owner_independent": True,
        "user_live_limits_verified": True,
        "live_limits_nonzero": True,
        "trading_paused": True,
        "verified_wallet_address": wallet,
        "live_limits_revision": revision,
        "live_capital_usd": "5000",
        "live_max_order_usd": order,
        "live_daily_turnover_usd": "15000",
    }
    values.update(changes)
    return values


def rates(eth="3000", usdc="1", usdt="1", age=0):
    return {
        "ethereum": {"usd": eth, "last_updated_at": NOW-age},
        "usd-coin": {"usd": usdc, "last_updated_at": NOW-age},
        "tether": {"usd": usdt, "last_updated_at": NOW-age},
    }


def candidate(wallet=WALLET, **changes):
    return planner.build_verified_eth_policy_candidate(
        evidence=verified(wallet=wallet, **changes),
        snapshot=rates(), owner=OWNER, executor=SIGNER, now=NOW,
    )


def test_independent_live_user_caps_not_100_usdc():
    plan = candidate()
    caps = plan["snapshot"]["caps"]
    assert caps["usdc_max_input_micro"] == "5000000000"
    assert caps["usdt_max_input_micro"] == "5000000000"
    assert caps["weth_max_input_wei"] == str((5*10**21)//3)
    assert caps["native_wrap_max_wei"] == caps["weth_max_input_wei"]
    assert plan["snapshot"]["wallet"] == WALLET
    assert plan["snapshot"]["limits_revision"] == 3
    assert plan["snapshot"]["max_order_usd"] == "5000"
    assert plan["snapshot"]["max_daily_turnover_usd"] == "15000"
    assert plan["policy"]["owner_id"] == OWNER
    assert len(plan["policy"]["rules"]) == 6
    for rule in plan["policy"]["rules"]:
        assert rule["method"] == "eth_sendTransaction"
        assert rule["action"] == "ALLOW"
        assert any(c["field"] == "chain_id" and c["value"] == "1"
                   for c in rule["conditions"])
    assert not any(k in plan for k in ("private_key","grant","send"))


def test_cross_user_wallet_produces_unique_policy_and_exact_recipient():
    a = candidate()
    b = candidate(wallet=OTHER_WALLET)
    assert a["snapshot"]["sha256"] != b["snapshot"]["sha256"]
    assert a["policy"]["name"] != b["policy"]["name"]
    for plan, wallet in ((a, WALLET), (b, OTHER_WALLET)):
        for rule in plan["policy"]["rules"][:4]:
            recipient = [c["value"] for c in rule["conditions"]
                         if c["field"] == "exactInputSingle.params.recipient"]
            assert recipient == [wallet]


def test_caps_reflect_verified_snapshot_depegs_without_fixed_100_usdc():
    caps = planner.derive_policy_atomic_caps(
        max_order_usd="2500.50", snapshot=rates(
            eth="2500", usdc="1.001", usdt="0.999"), now=NOW,
    )
    assert int(caps["usdc_max_input_micro"]) < 2_500_500_000
    assert int(caps["usdt_max_input_micro"]) > 2_500_500_000
    assert caps["weth_max_input_wei"] == str(1_000_200_000_000_000_000)


@pytest.mark.parametrize("change", [
    {"usdc":"0.98"}, {"usdc":"1.02"}, {"usdt":"0.97"},
    {"usdt":"1.03"}, {"eth":"0"}, {"eth":"NaN"},
    {"eth":"Infinity"}, {"eth":"-1"}, {"eth":"99999999"},
    {"age":91}, {"age":-7},
])
def test_unpegged_stale_future_or_invalid_prices_are_rejected(change):
    with pytest.raises(ValueError):
        planner.derive_policy_atomic_caps(
            max_order_usd="5000", snapshot=rates(**change), now=NOW,
        )


@pytest.mark.parametrize("bad_order", [
    "", "-1", "0", "NaN", "Infinity", "10000000", "0.0000001",
    None, True, 0,
])
def test_invalid_user_live_order_amount_rejected(bad_order):
    with pytest.raises(ValueError):
        planner.derive_policy_atomic_caps(
            max_order_usd=bad_order, snapshot=rates(), now=NOW,
        )


@pytest.mark.parametrize("bad", [
    {"wallet_identity_verified": False},
    {"wallet_owned_by_authenticated_user": False},
    {"wallet_chain_verified": False},
    {"unrecognized_existing_signers": True},
    {"policy_owner_independent": False},
    {"live_limits_nonzero": False},
    {"trading_paused": False},
    {"live_limits_revision": -1},
    {"live_capital_usd": "0"},
    {"live_max_order_usd": "7000"},
    {"live_daily_turnover_usd": "0"},
])
def test_unverified_wallet_owner_signer_paused_or_caps_fail_closed(bad):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        planner.build_verified_eth_policy_candidate(
            evidence=verified(**bad), snapshot=rates(),
            owner=OWNER, executor=SIGNER, now=NOW,
        )


def test_missing_or_same_quorum_ids_are_rejected():
    for owner, signer in ((OWNER, OWNER), ("", SIGNER), (OWNER, "")):
        with pytest.raises((ValueError, Exception)):
            planner.build_verified_eth_policy_candidate(
                evidence=verified(), snapshot=rates(), owner=owner,
                executor=signer, now=NOW,
            )


def test_prices_fetch_fixed_source_no_dynamic_url_and_size_bound(monkeypatch):
    observed = []
    original = httpx.AsyncClient
    def mock_client(*args, **kwargs):
        def handler(request):
            observed.append((str(request.url), request.method))
            assert request.url.path == "/api/v3/simple/price"
            assert request.url.host == "api.coingecko.com"
            assert "include_last_updated_at=true" in str(request.url)
            return httpx.Response(200, json=rates())
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(planner.httpx, "AsyncClient", mock_client)
    result = asyncio.run(planner.fetch_reference_prices())
    assert result == rates()
    assert observed and len(observed) == 1
    assert observed[0][1] == "GET"


def test_account_preflight_never_queries_prices_when_no_privy_identity(
    monkeypatch,
):
    async def unready(did):
        assert did == DID
        return verified(wallet_identity_verified=False)
    async def must_not_fetch():
        raise AssertionError("No third-party price fetch for unknown wallet")
    monkeypatch.setattr(planner, "get_eth_user_enrollment_preflight", unready)
    monkeypatch.setattr(planner, "fetch_reference_prices", must_not_fetch)
    result = asyncio.run(planner.get_eth_policy_candidate_preflight(DID))
    assert result["policy_candidate_valid"] is False
    assert result["can_grant_permission"] is False
    assert result["execution_enabled"] is False


def test_valid_candidate_diagnostic_does_not_create_or_expose_policy(
    monkeypatch,
):
    async def ready(did):
        assert did == DID
        return verified()
    async def prices():
        return rates()
    monkeypatch.setattr(planner, "get_eth_user_enrollment_preflight", ready)
    monkeypatch.setattr(planner, "fetch_reference_prices", prices)
    monkeypatch.setattr(planner, "_require_policy_roles", lambda: (OWNER,SIGNER))
    monkeypatch.setattr(planner.time, "time", lambda: NOW)
    result = asyncio.run(planner.get_eth_policy_candidate_preflight(DID))
    assert result["policy_candidate_valid"] is True
    assert len(result["policy_candidate_sha256"]) == 64
    assert result["policy_candidate_expiration_unix"] == NOW + 86400
    assert "policy" not in result and "rules" not in result
    assert result["policy_created_in_privy"] is False
    assert result["user_privy_delegation_verified"] is False
    assert result["remote_revocation_implemented"] is False
    assert result["can_grant_permission"] is False
    assert result["can_submit"] is False
    assert result["execution_enabled"] is False


@pytest.fixture
def auth(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "stage19-verified-session", "iat": now,
        "exp": now+600,
    }, private, algorithm="ES256")
    return {"Authorization": "Bearer "+token}


def test_only_verified_user_can_get_inert_policy_candidate_status(auth, monkeypatch):
    async def prepared(did):
        assert did == DID
        return {
            "mode": "ETH_USER_POLICY_CANDIDATE_PREVIEW_ONLY",
            "policy_candidate_valid": True,
            "can_grant_permission": False,
            "can_submit": False,
            "execution_enabled": False,
        }
    monkeypatch.setattr(api, "get_eth_policy_candidate_preflight", prepared)
    client = TestClient(api.app)
    path = "/api/live/eth/policy-candidate"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer fake"}).status_code == 401
    response = client.get(path, headers=auth)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json()["execution_enabled"] is False
    assert client.post(path, headers=auth).status_code == 405
    assert client.put(path, headers=auth).status_code == 405
    routes = {r.path for r in api.app.routes}
    assert "/api/live/eth/create-policy" not in routes
    assert "/api/live/eth/attach-signer" not in routes
    assert "/api/live/eth/execute" not in routes
