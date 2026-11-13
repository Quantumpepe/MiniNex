"""ETH automated execution readiness cannot authorize signing or funds.

All checks operate on simulated server-side evidence. No Privy network,
PostgreSQL, onchain order, signing or wallet grant is used in these tests.
"""
from __future__ import annotations

import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from backend import app as api, eth_auto_preflight as stage
from backend.live_orders import LIVE_RESERVATION_ACTIVATED


DID = "did:privy:eth-preflight-user"


def signer(*, verified=True, delegated=True):
    return {
        "wallet_delegated": delegated,
        "signer_verified": verified,
        "policy_reference_verified": verified,
        "execution_enabled": False,
    }


def policy(*, verified=True):
    return {
        "policy_hash_pinned": verified,
        "policy_digest_matches": verified,
        "policy_structure_checked": verified,
        "recipient_wallet_bound": verified,
        "execution_enabled": False,
    }


def limits(*, paused=True, order="100", daily="300", capital="500"):
    return {
        "planned_capital_usd": capital,
        "max_order_usd": order,
        "max_daily_turnover_usd": daily,
        "trading_paused": paused,
        "revision": 7,
        "privy_grant_verified": False,
    }


def test_privy_verified_and_configured_limits_still_cannot_enable_execution():
    report = stage.assess_eth_automatic_preflight(
        signer=signer(), policy=policy(), limits=limits(paused=False))
    assert report["asset"] == "ETH"
    checks = report["checks"]
    assert checks["privy_wallet_delegated"] is True
    assert checks["privy_additional_signer_verified"] is True
    assert checks["privy_restricted_policy_reference_verified"] is True
    assert checks["privy_policy_digest_pinned_and_matched"] is True
    assert checks["privy_policy_structure_checked"] is True
    assert checks["per_user_eth_limits_configured"] is True
    assert checks["live_eth_stop_released"] is True
    assert checks["policy_covers_current_router_call"] is False
    assert checks["separate_revocable_execution_consent_verified"] is False
    assert checks["atomic_order_reservations_enabled"] is False
    assert checks["live_receipt_reconciliation_proven"] is False
    assert report["preflight_passed"] is False
    assert report["can_execute"] is False
    assert report["can_reserve"] is False
    assert report["automated_signing_enabled"] is False
    assert report["live_trading_enabled"] is False
    assert stage.CURRENT_ROUTER_CALL != stage.STATIC_POLICY_CALL
    assert LIVE_RESERVATION_ACTIVATED is False
    assert "policy_covers_current_router_call" in report["blockers"]


@pytest.mark.parametrize("bad_limits", [
    limits(), limits(order="0", daily="0", capital="0"),
    limits(paused=False, order="501"),
    limits(paused=False, order="999999999999"),
    limits(paused=False, capital="-1"),
    limits(paused=False, order="NaN"),
    limits(paused=False, order=True),
    {"revision": 1},
])
def test_stopped_or_bad_limits_fail_closed(bad_limits):
    result = stage.assess_eth_automatic_preflight(
        signer=signer(), policy=policy(), limits=bad_limits)
    assert result["can_execute"] is False
    assert (
        result["checks"]["per_user_eth_limits_configured"] is False
        or result["checks"]["live_eth_stop_released"] is False
    )


def test_wrong_grant_or_policy_still_appears_as_blocker():
    for evidence in (
        (signer(verified=False), policy(), limits(paused=False)),
        (signer(delegated=False), policy(), limits(paused=False)),
        (signer(), policy(verified=False), limits(paused=False)),
    ):
        result = stage.assess_eth_automatic_preflight(
            signer=evidence[0], policy=evidence[1], limits=evidence[2])
        assert result["blockers"]
        assert result["can_execute"] is False
        assert result["preflight_passed"] is False
    for evidence in (None, [], 5):
        with pytest.raises(ValueError):
            stage.assess_eth_automatic_preflight(
                signer=evidence, policy=policy(), limits=limits())


def test_preflight_gathers_verified_server_side_sources_without_writes(monkeypatch):
    calls = []

    async def fetch_signer(did):
        calls.append(("signer", did))
        return signer()

    async def fetch_policy(did):
        calls.append(("policy", did))
        return policy()

    def fetch_limits(did):
        calls.append(("limits", did))
        return limits()

    monkeypatch.setattr(stage, "get_eth_signer_status", fetch_signer)
    monkeypatch.setattr(stage, "get_eth_policy_review", fetch_policy)
    monkeypatch.setattr(stage, "get_live_eth_limits", fetch_limits)

    result = asyncio.run(stage.get_eth_automatic_preflight(DID))
    assert calls == [
        ("signer", DID), ("policy", DID), ("limits", DID)
    ]
    assert result["risk_limit_revision"] == 7
    assert result["checks"]["live_eth_stop_released"] is False
    assert result["can_execute"] is False


@pytest.fixture
def auth(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", pub)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io",
        "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID,
        "sid": "eth-auto-preflight",
        "iat": now,
        "exp": now + 600,
    }, key, algorithm="ES256")
    return {"Authorization": "Bearer " + token}


def test_api_is_private_read_only_and_never_grants_permission(auth, monkeypatch):
    seen = []

    async def get(did):
        seen.append(did)
        return stage.assess_eth_automatic_preflight(
            signer=signer(), policy=policy(), limits=limits())

    monkeypatch.setattr(api, "get_eth_automatic_preflight", get)
    client = TestClient(api.app)
    assert client.get("/api/live/eth/auto-preflight").status_code == 401
    assert client.post("/api/live/eth/auto-preflight", headers=auth).status_code == 405
    assert client.get(
        "/api/live/eth/auto-preflight?user_id=did:privy:attacker",
        headers=auth).status_code == 200
    result = client.get("/api/live/eth/auto-preflight", headers=auth)
    assert result.status_code == 200
    assert seen == [DID, DID]
    assert result.headers["cache-control"] == "private, no-store"
    assert result.headers["x-content-type-options"] == "nosniff"
    body = result.json()
    assert body["mode"] == "ETH_AUTO_EXECUTION_READINESS_ONLY"
    assert body["can_execute"] is False
    assert body["can_reserve"] is False
    assert not any(route.path == "/api/live/eth/auto-execute"
                   for route in api.app.routes)
