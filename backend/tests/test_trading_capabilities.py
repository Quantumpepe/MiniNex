"""Trading preparation must never grant execution authority."""
import time
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from backend.app import app


def test_routes_require_privy_identity():
    client = TestClient(app)
    for header in ({}, {"Authorization": "Bearer invalid"}):
        response = client.get("/api/trading/capabilities", headers=header)
        assert response.status_code == 401


def test_routes_are_separate_and_fail_closed(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", pem)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": "did:privy:routes-test", "sid": "routes-session",
        "iat": now, "exp": now + 600
    }, key, algorithm="ES256")
    response = TestClient(app).get("/api/trading/capabilities", headers={
        "Authorization": "Bearer " + token
    })
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["mode"] == "PREPARATION_ONLY"
    assert data["real_trading_enabled"] is False
    assert data["automated_signing_enabled"] is False
    assert data["deposits_enabled"] is False
    assert data["withdrawals_enabled"] is False
    assert set(data["routes"]) == {"BTC", "ETH"}
    assert data["routes"]["BTC"]["wallet_chain"] == "bitcoin"
    assert data["routes"]["ETH"]["wallet_chain"] == "ethereum"
    assert data["routes"]["BTC"]["cross_chain_settlement_required"] is True
    assert data["routes"]["ETH"]["cross_chain_settlement_required"] is False
    for asset in ("BTC", "ETH"):
        route = data["routes"][asset]
        assert route["settlement_options"] == ["USDC", "USDT"]
        assert route["ready"] is False
        assert route["authorization_configured"] is False
        assert route["execution_provider_configured"] is False
