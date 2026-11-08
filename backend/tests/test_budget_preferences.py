"""Budget APIs: identity-bound preferences with no wallet trading authority."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import budgets, storage


@pytest.fixture
def signed(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)

    def make(did="did:privy:alice"):
        now = int(time.time())
        return {"Authorization": "Bearer " + jwt.encode({
            "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
            "sub": did, "sid": "budget-session",
            "iat": now, "exp": now + 600
        }, private, algorithm="ES256")}
    return make


def sample(percent=0, revision=0):
    return {
        "mode": "PREFERENCES_ONLY", "trading_enabled": False,
        "signing_authorized": False,
        "budgets": {
            asset: {"percent": percent, "revision": revision, "authorized": False}
            for asset in ("BTC", "ETH")
        },
    }


def test_get_put_require_valid_privy():
    client = TestClient(api.app)
    for method, url, body in [
        ("GET", "/api/budgets", None),
        ("PUT", "/api/budgets/BTC", {"percent": 25, "expected_revision": 0}),
    ]:
        for headers in ({}, {"Authorization": "Bearer bad"}):
            response = client.request(method, url, headers=headers, json=body)
            assert response.status_code == 401


def test_persisted_settings_scoped_to_verified_did(monkeypatch, signed):
    seen = []

    def get(did):
        seen.append(("get", did))
        return sample()

    def save(did, asset, percent, expected_revision):
        seen.append(("put", did, asset, percent, expected_revision))
        return sample(percent, expected_revision + 1)

    monkeypatch.setattr(api, "get_budget_preferences", get)
    monkeypatch.setattr(api, "save_budget_preference", save)
    client = TestClient(api.app)
    a = client.get("/api/budgets", headers=signed("did:privy:alice"))
    b = client.put("/api/budgets/ETH", json={
        "percent": 35, "expected_revision": 2
    }, headers=signed("did:privy:bob"))
    assert a.status_code == b.status_code == 200
    assert a.headers["cache-control"] == "no-store"
    assert b.json()["signing_authorized"] is False
    assert seen == [("get", "did:privy:alice"), ("put", "did:privy:bob", "ETH", 35, 2)]


@pytest.mark.parametrize("payload", [
    {"percent": -1, "expected_revision": 0},
    {"percent": 101, "expected_revision": 0},
    {"percent": 50.5, "expected_revision": 0},
    {"percent": "50", "expected_revision": 0},
    {"percent": True, "expected_revision": 0},
    {"percent": 50, "expected_revision": -1},
    {"percent": 50, "expected_revision": "0"},
    {"percent": 50, "expected_revision": 0, "trading_enabled": True},
])
def test_invalid_preferences_rejected_without_write(monkeypatch, signed, payload):
    monkeypatch.setattr(api, "save_budget_preference",
                        lambda *args: pytest.fail("Must not write invalid settings"))
    response = TestClient(api.app).put(
        "/api/budgets/BTC", headers=signed(), json=payload
    )
    assert response.status_code == 422


def test_conflicting_revision_fails_closed(monkeypatch, signed):
    def conflict(*args):
        raise HTTPException(409, "Budget changed; refresh and try again")

    monkeypatch.setattr(api, "save_budget_preference", conflict)
    r = TestClient(api.app).put("/api/budgets/BTC",
        headers=signed(), json={"percent": 50, "expected_revision": 0})
    assert r.status_code == 409


def test_schema_separates_user_assets_and_never_grants_access():
    ddl = "\n".join(storage.SCHEMA)
    assert "mininex_budget_preferences" in ddl
    assert "PRIMARY KEY (privy_did,asset)" in ddl
    assert "DEFAULT 0 CHECK (percent BETWEEN 0 AND 100)" in ddl
    assert "ON CONFLICT (privy_did,asset) DO NOTHING" in storage._prepare_rows.__code__.co_consts
    assert not any(token in ddl.lower() for token in ("private_key", "signer_secret", "delegation_token"))
    assert budgets.validate_percent(0) == 0
    assert budgets.validate_percent(100) == 100
    for invalid in (False, "20", 1.2, -1, 101):
        with pytest.raises(ValueError):
            budgets.validate_percent(invalid)
