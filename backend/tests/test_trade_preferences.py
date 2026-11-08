"""BTC/ETH trade preferences: authenticated, persistent and never authorization."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import storage, trade_preferences


@pytest.fixture
def signed(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)

    def make(did="did:privy:alice"):
        now = int(time.time())
        token = jwt.encode({
            "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
            "sub": did, "sid": "trade-preference-tests",
            "iat": now, "exp": now + 600,
        }, key, algorithm="ES256")
        return {"Authorization": "Bearer " + token}
    return make


def snapshot():
    return {
        "mode": "PREFERENCES_ONLY", "trading_enabled": False,
        "signing_authorized": False,
        "settings": {
            a: {"mode": "LONG", "stable": "USDC", "horizon": "15m",
                "revision": 0, "authorized": False}
            for a in ("BTC", "ETH")
        },
    }


def test_authentication_required():
    client = TestClient(api.app)
    for url, method, body in [
        ("/api/trade-preferences", "GET", None),
        ("/api/trade-preferences/BTC", "PUT", {
            "mode": "LONG", "stable": "USDC", "horizon": "15m",
            "expected_revision": 0,
        }),
    ]:
        for headers in ({}, {"Authorization": "Bearer invalid"}):
            response = client.request(method, url, headers=headers, json=body)
            assert response.status_code == 401


def test_own_identity_and_assets_only(monkeypatch, signed):
    seen = []

    def read(did):
        seen.append(("read", did))
        return snapshot()

    def save(did, asset, mode, stable, horizon, expected_revision):
        seen.append(("save", did, asset, mode, stable, horizon, expected_revision))
        return snapshot()

    monkeypatch.setattr(api, "get_trade_preferences", read)
    monkeypatch.setattr(api, "save_trade_preference", save)
    client = TestClient(api.app)
    first = client.get("/api/trade-preferences", headers=signed("did:privy:alice"))
    second = client.put("/api/trade-preferences/ETH", headers=signed("did:privy:bob"),
                        json={"mode": "SPOT_SHORT", "stable": "USDT",
                              "horizon": "1h", "expected_revision": 4})
    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert second.json()["signing_authorized"] is False
    assert seen == [
        ("read", "did:privy:alice"),
        ("save", "did:privy:bob", "ETH", "SPOT_SHORT", "USDT", "1h", 4),
    ]


@pytest.mark.parametrize("patch", [
    {"mode": "LEVERAGED_SHORT"}, {"stable": "DAI"}, {"horizon": "2h"},
    {"expected_revision": -1}, {"expected_revision": "0"},
    {"expected_revision": 1.5}, {"mode": 5},
    {"trading_enabled": True},
])
def test_invalid_settings_never_written(monkeypatch, signed, patch):
    monkeypatch.setattr(api, "save_trade_preference",
                        lambda *args: pytest.fail("invalid preferences were written"))
    body = {"mode": "LONG", "stable": "USDC", "horizon": "15m",
            "expected_revision": 0, **patch}
    response = TestClient(api.app).put("/api/trade-preferences/BTC",
                                       headers=signed(), json=body)
    assert response.status_code == 422


def test_revision_conflicts_fail_closed(monkeypatch, signed):
    def conflict(*args):
        raise HTTPException(409, "Revision mismatch")
    monkeypatch.setattr(api, "save_trade_preference", conflict)
    response = TestClient(api.app).put("/api/trade-preferences/BTC", headers=signed(),
        json={"mode": "LONG", "stable": "USDC", "horizon": "15m",
              "expected_revision": 0})
    assert response.status_code == 409


def test_sql_schema_and_validations():
    ddl = "\n".join(storage.SCHEMA)
    assert "mininex_trade_preferences" in ddl
    assert "PRIMARY KEY (privy_did,asset)" in ddl
    assert "CHECK (mode IN ('LONG','SPOT_SHORT'))" in ddl
    assert "CHECK (stable IN ('USDC','USDT'))" in ddl
    assert "CHECK (horizon IN ('5m','15m','1h','24h'))" in ddl
    assert not any(x in ddl.lower() for x in ("private_key", "delegation_token", "signer_secret"))
    for field, value, choices in [
        ("mode", "LEVERAGED", trade_preferences.MODES),
        ("stable", "DAI", trade_preferences.STABLES),
        ("horizon", "3h", trade_preferences.HORIZONS),
    ]:
        with pytest.raises(ValueError):
            trade_preferences.validate_choice(field, value, choices)
