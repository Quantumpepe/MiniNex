"""Regression coverage: independent LIVE limits never become signing authority."""
from decimal import Decimal
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import live_eth_controls as controls
from backend.storage import SCHEMA

DID = "did:privy:live-eth-guards"


@pytest.fixture
def headers(monkeypatch):
    secret = ec.generate_private_key(ec.SECP256R1())
    public = secret.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "live-eth-guards", "iat": now, "exp": now + 500,
    }, secret, algorithm="ES256")
    return {"Authorization": "Bearer " + token}


def test_live_amounts_and_caps_are_exact():
    result = controls.validate_limits("1500.10", "300.05", "600.00")
    assert result == (Decimal("1500.10"), Decimal("300.05"), Decimal("600.00"))
    assert controls.validate_limits("0", "0", "0") == (
        Decimal("0"), Decimal("0"), Decimal("0"))
    for bad in (
        ("100", "0", "10"),
        ("100", "150", "200"),
        ("100", "10", "5"),
        ("-1", "1", "1"),
        ("1e6", "1", "1"),
        ("NaN", "1", "1"),
        ("1.0000001", "1", "1"),
        ("10000000", "100", "100"),
        (True, "1", "1"),
    ):
        with pytest.raises(HTTPException) as err:
            controls.validate_limits(*bad)
        assert err.value.status_code == 422


def test_spend_guard_defaults_to_closed_even_with_saved_budget():
    fields = {
        "planned_capital_usd": Decimal("500"),
        "max_order_usd": Decimal("100"),
        "max_daily_turnover_usd": Decimal("250"),
        "trading_paused": True,
        "privy_grant_verified": False,
    }
    with pytest.raises(HTTPException) as err:
        controls.validate_spend_limits(
            budget=fields, order_usd="40", reserved_usd="0", spent_today_usd="0")
    assert err.value.status_code == 409
    fields["trading_paused"] = False
    with pytest.raises(HTTPException) as err:
        controls.validate_spend_limits(
            budget=fields, order_usd="40", reserved_usd="0", spent_today_usd="0")
    assert err.value.status_code == 409
    fields["privy_grant_verified"] = True
    assert controls.validate_spend_limits(
        budget=fields, order_usd="80", reserved_usd="20",
        spent_today_usd="150") == Decimal("80")
    for order, reserved, spent in (
        ("101", "0", "0"),
        ("70", "450", "0"),
        ("70", "0", "200"),
    ):
        with pytest.raises(HTTPException) as err:
            controls.validate_spend_limits(
                budget=fields, order_usd=order,
                reserved_usd=reserved, spent_today_usd=spent)
        assert err.value.status_code == 409
    with pytest.raises(HTTPException):
        controls.validate_spend_limits(
            budget=fields, order_usd="NaN", reserved_usd="0", spent_today_usd="0")


def test_idempotency_key_is_strict():
    assert controls.validate_order_key("mininexETH_1234567") == "mininexETH_1234567"
    for value in ("", "123", " has-spaces", "../system/orders", None, 22):
        with pytest.raises(HTTPException):
            controls.validate_order_key(value)


def test_schema_has_separate_paused_eth_limits_and_no_authorization_column():
    ddl = "\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_live_eth_limits" in ddl
    assert "trading_paused BOOLEAN NOT NULL DEFAULT TRUE" in ddl
    assert "max_order_usd <= planned_capital_usd" in ddl
    assert "max_order_usd <= max_daily_turnover_usd" in ddl
    assert "mininex_budget_preferences" in ddl
    assert "mininex_user_paper_sessions" in ddl
    assert "INSERT INTO mininex_live_eth_limits(privy_did)" in __import__(
        "inspect").getsource(__import__("backend.storage", fromlist=["_prepare_rows"])._prepare_rows)


def test_live_endpoints_are_owner_scoped_and_never_activate(headers, monkeypatch):
    seen = []
    def get(did):
        seen.append(("get", did))
        return controls._snapshot({
            "planned_capital_usd": Decimal("0"),
            "max_order_usd": Decimal("0"),
            "max_daily_turnover_usd": Decimal("0"),
            "revision": 0,
            "trading_paused": True,
        })
    def save(did, *, capital, order, daily, expected_revision):
        seen.append(("save", did, capital, order, daily, expected_revision))
        return controls._snapshot({
            "planned_capital_usd": Decimal(capital),
            "max_order_usd": Decimal(order),
            "max_daily_turnover_usd": Decimal(daily),
            "revision": expected_revision + 1,
            "trading_paused": True,
        })
    def stop(did):
        seen.append(("stop", did))
        return get(did)
    monkeypatch.setattr(api, "get_live_eth_limits", get)
    monkeypatch.setattr(api, "save_live_eth_limits", save)
    monkeypatch.setattr(api, "stop_live_eth", stop)
    client = TestClient(api.app)
    assert client.get("/api/live/eth/limits").status_code == 401
    assert client.post("/api/live/eth/stop").status_code == 401
    assert client.put("/api/live/eth/limits", json={
        "planned_capital_usd": "100", "max_order_usd": "20",
        "max_daily_turnover_usd": "100", "expected_revision": 0,
    }).status_code == 401
    r = client.get("/api/live/eth/limits", headers=headers)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    assert r.json()["live_trading_enabled"] is False
    assert r.json()["automated_signing_enabled"] is False
    assert r.json()["trading_paused"] is True

    body = {"planned_capital_usd": "100", "max_order_usd": "20",
            "max_daily_turnover_usd": "100", "expected_revision": 0}
    assert client.put("/api/live/eth/limits", headers=headers,
                      json={**body, "wallet_id": "evil"}).status_code == 422
    assert client.put("/api/live/eth/limits", headers=headers,
                      json={**body, "trading_paused": False}).status_code == 422
    assert client.put("/api/live/eth/limits", headers=headers,
                      json={**body, "privy_grant_verified": True}).status_code == 422
    updated = client.put("/api/live/eth/limits", headers=headers, json=body)
    assert updated.status_code == 200
    assert updated.json()["live_trading_enabled"] is False
    assert updated.json()["trading_paused"] is True
    stopped = client.post("/api/live/eth/stop", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["trading_paused"] is True
    assert all(entry[1] == DID for entry in seen)
    assert not any("/api/live/eth/start" == r.path for r in api.app.routes)
    assert not any("/api/live/eth/execute" == r.path for r in api.app.routes)
