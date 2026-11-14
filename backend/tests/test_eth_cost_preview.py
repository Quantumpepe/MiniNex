"""Stages 57-64: only read-only ETH cost review; no transaction authorization."""
import asyncio
from datetime import datetime, timezone
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_cost_preview as cp

DID = "did:privy:eth-cost-preview"
WALLET = "0x" + "a" * 40


def evidence():
    now = int(time.time())
    plan = {
        "mode": "LIVE_ETH_SWAP_PLAN_ONLY", "asset": "ETH", "chain_id": 1,
        "side": "BUY", "input_asset": "USDC", "output_asset": "ETH",
        "input_amount": "100", "minimum_output_amount": "0.05",
        "state": "UNSIGNED_TX_REVIEW_REQUIRED",
        "execution_enabled": False, "automatic_signing_enabled": False,
        "can_submit": False, "safe_to_sign": False,
        "transaction_built": True, "erc20_approval_required": False,
        "permit_signature_required": False, "tx_outer_fields_verified": True,
        "router_command_pattern_verified": True, "router_commands_decoded": True,
        "router_command_contents_verified": True, "router_recipients_verified": True,
        "router_quote_bounds_verified": True,
        "max_gas_limit": 100_000, "max_fee_per_gas_gwei": "2",
    }
    user = {
        "id": DID, "linked_accounts": [{
            "type": "wallet", "chain_type": "ethereum",
            "wallet_client_type": "privy", "address": WALLET,
        }],
    }
    wallet = {
        "mode": "VERIFIED_READ_ONLY", "wallet_ownership_verified": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "wallets": {"ETH": WALLET},
        "balances": {
            "ETH": {"balance": "0.1", "status": "verified", "network": "ethereum-mainnet"},
            "USDC": {"balance": "200", "status": "verified", "network": "ethereum-mainnet"},
        },
    }
    prices = {
        "ethereum": {"usd": 2000, "last_updated_at": now},
        "usd-coin": {"usd": 1, "last_updated_at": now},
    }
    limits = {
        "asset": "ETH", "chain_id": 1, "revision": 1,
        "trading_paused": True, "planned_capital_usd": "200",
        "max_order_usd": "110", "max_daily_turnover_usd": "200",
    }
    ledger = {
        "mode": "ETH_LIVE_LEDGER_READ_ONLY", "asset": "ETH",
        "chain_id": 1, "execution_enabled": False,
        "paper_simulation_included": False, "limits_revision": 1,
        "trading_paused": True, "planned_capital_usd": "200",
        "max_order_usd": "110", "rolling_24h_limit_usd": "200",
        "pending_reserved_usd": "0", "confirmed_24h_usd": "0",
        "unresolved_order_count": 0, "reconciliation_required_count": 0,
        "older_than_30m_pending_count": 0,
    }
    return dict(
        did=DID, side="BUY", stable="USDC", amount="100", now=now,
        plan=plan, user=user, wallet=wallet,
        prices=prices, ledger=ledger, limits=limits,
    )


def review(data):
    return cp._review_verified_evidence(**data)


def test_server_evidence_provides_only_indicative_cost():
    result = review(evidence())
    assert result["mode"] == "ETH_LIVE_COST_PREVIEW_ONLY"
    assert result["max_possible_gas_eth"] == "0.0002"
    assert result["cost_estimate_available"] is True
    assert result["wallet_identity_checked_at_preview"] is True
    assert result["within_declared_cost_limits"] is False  # User stop remains ON
    assert "ETH_STOP_ACTIVE" in result["blockers"]
    assert "NO_ATOMIC_ORDER_RESERVATION" in result["operational_blockers"]
    assert result["same_transaction_budget_lock_held"] is False
    assert not any(result[k] for k in ("can_reserve", "can_sign", "can_submit", "live_trading_enabled"))


@pytest.mark.parametrize("field", [
    "tx_outer_fields_verified", "router_command_pattern_verified",
    "router_commands_decoded", "router_command_contents_verified",
    "router_recipients_verified", "router_quote_bounds_verified",
])
def test_incomplete_uniswap_command_review_fails_closed(field):
    data = evidence()
    data["plan"][field] = False
    with pytest.raises(ValueError):
        review(data)


@pytest.mark.parametrize("field,value", [
    ("chain_id", 137), ("side", "SELL"), ("input_amount", "99"),
    ("execution_enabled", True), ("can_submit", True),
    ("erc20_approval_required", True), ("state", "UNKNOWN"),
    ("max_fee_per_gas_gwei", "1.0000000001"),
    ("max_gas_limit", "100000"),
])
def test_invalid_plan_never_receives_cost_status(field, value):
    data = evidence()
    data["plan"][field] = value
    with pytest.raises(ValueError):
        review(data)


def test_wallet_must_be_same_independently_verified_privy_owner():
    data = evidence()
    data["wallet"]["wallets"]["ETH"] = "0x" + "b" * 40
    with pytest.raises(ValueError):
        review(data)


def test_unverified_or_stale_explorer_balance_fails_closed():
    data = evidence()
    data["wallet"]["balances"]["ETH"]["status"] = "unavailable"
    with pytest.raises(ValueError):
        review(data)
    data = evidence()
    data["wallet"]["balances"]["USDC"]["balance"] = "0.0000001"
    with pytest.raises(ValueError):
        review(data)


def test_price_staleness_future_stamp_and_depeg_fail_closed():
    for offset in (-31, 3):
        data = evidence()
        data["prices"]["ethereum"]["last_updated_at"] = data["now"] + offset
        with pytest.raises(ValueError):
            review(data)
    data = evidence()
    data["prices"]["usd-coin"]["usd"] = 0.95
    with pytest.raises(ValueError):
        review(data)


def test_stale_limit_revision_and_unpriced_cross_asset_rejected():
    data = evidence()
    data["limits"]["revision"] = 2
    with pytest.raises(ValueError):
        review(data)
    data = evidence()
    data["ledger"]["asset"] = "BTC"
    with pytest.raises(ValueError):
        review(data)


def test_pending_eth_orders_use_only_eth_budget_and_block_operationally():
    data = evidence()
    data["ledger"]["pending_reserved_usd"] = "150"
    data["ledger"]["unresolved_order_count"] = 1
    data["ledger"]["reconciliation_required_count"] = 1
    result = review(data)
    assert "RESERVED_CAPITAL_OVER_LIMIT" in result["blockers"]
    assert "ETH_RECEIPT_RECONCILIATION_REQUIRED" in result["operational_blockers"]
    assert result["can_submit"] is False


def test_approval_only_never_estimates_unbuilt_gas(monkeypatch):
    plan = evidence()["plan"]
    plan.update({
        "state": "APPROVAL_OR_PERMIT_REQUIRED", "transaction_built": False,
    })
    async def fake_plan(*args, **kwargs):
        return plan
    async def forbidden(*args, **kwargs):
        raise AssertionError("Should not fetch a fee estimate for an unbuilt transaction")
    monkeypatch.setattr(cp, "make_eth_swap_plan", fake_plan)
    monkeypatch.setattr(cp, "fetch_reference_prices", forbidden)
    result = asyncio.run(cp.get_eth_readonly_cost_preview(
        DID, side="BUY", stable="USDC", amount="100"))
    assert result["cost_estimate_available"] is False
    assert result["can_submit"] is False
    assert result["state"] == "APPROVAL_OR_PERMIT_REQUIRED"


def test_readonly_preview_uses_server_owned_data_only(monkeypatch):
    data = evidence()
    async def fake_plan(*args, **kwargs):
        return data["plan"]
    async def fake_user(did):
        assert did == DID
        return data["user"]
    async def fake_wallet(did):
        assert did == DID
        return data["wallet"]
    async def fake_prices():
        return data["prices"]
    def fake_ledger(did):
        assert did == DID
        return data["ledger"]
    def fake_limits(did):
        assert did == DID
        return data["limits"]
    monkeypatch.setattr(cp, "make_eth_swap_plan", fake_plan)
    monkeypatch.setattr(cp, "fetch_privy_user", fake_user)
    monkeypatch.setattr(cp, "get_wallet_balances", fake_wallet)
    monkeypatch.setattr(cp, "fetch_reference_prices", fake_prices)
    monkeypatch.setattr(cp, "get_eth_live_ledger_audit", fake_ledger)
    monkeypatch.setattr(cp, "get_live_eth_limits", fake_limits)
    result = asyncio.run(cp.get_eth_readonly_cost_preview(
        DID, side="BUY", stable="USDC", amount="100"))
    assert result["cost_estimate_available"] is True
    assert result["live_trading_enabled"] is False


@pytest.fixture
def bearer(monkeypatch):
    secret = ec.generate_private_key(ec.SECP256R1())
    pub = secret.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", pub)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "eth-cost-preview", "iat": now, "exp": now + 600,
    }, secret, algorithm="ES256")
    return {"Authorization": "Bearer " + token}


def test_cost_preview_is_user_authenticated_and_unable_to_send(monkeypatch, bearer):
    async def preview(did, *, side, stable, amount):
        assert (did, side, stable, amount) == (DID, "BUY", "USDC", "100")
        return {"asset": "ETH", "can_submit": False, "live_trading_enabled": False}
    monkeypatch.setattr(api, "get_eth_readonly_cost_preview", preview)
    client = TestClient(api.app)
    payload = {"side": "BUY", "stable": "USDC", "amount": "100"}
    no_auth = client.post("/api/live/eth/cost-preview", json=payload)
    assert no_auth.status_code in (401, 403)
    valid = client.post("/api/live/eth/cost-preview", json=payload, headers=bearer)
    assert valid.status_code == 200
    assert valid.json()["can_submit"] is False
    assert valid.headers["cache-control"] == "private, no-store"
    assert client.post(
        "/api/live/eth/cost-preview", json={**payload, "wallet": WALLET},
        headers=bearer,
    ).status_code == 422
