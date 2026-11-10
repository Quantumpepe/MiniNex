"""ETH LIVE preflight tests: independently owned wallet, genuine quote, no execution."""
import asyncio
from decimal import Decimal
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import live_eth_quotes as q

DID = "did:privy:eth-quote-test"
WALLET = "0x" + "a" * 40


def privy_record(did=DID):
    return {
        "id": did,
        "linked_accounts": [{
            "type": "wallet", "chain_type": "ethereum",
            "wallet_client_type": "privy", "address": WALLET,
        }],
    }


def balances(eth="1.25", usdc="500", usdt="100"):
    return {
        "wallets": {"ETH": WALLET},
        "balances": {
            "ETH": {"status": "verified", "balance": eth},
            "USDC": {"status": "verified", "balance": usdc},
            "USDT": {"status": "verified", "balance": usdt},
        },
    }


def mock_quote(payload):
    return {
        "routing": "CLASSIC",
        "quote": {
            "input": {"token": payload["tokenIn"], "amount": payload["amount"]},
            "output": {
                "token": payload["tokenOut"],
                "amount": "210000000000000000",
                "minimumAmount": "208950000000000000",
                "recipient": payload["swapper"],
            },
            "swapper": payload["swapper"],
        },
    }


@pytest.fixture
def headers(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", pem)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "eth-quote-test", "iat": now, "exp": now + 500,
    }, key, algorithm="ES256")
    return {"Authorization": "Bearer " + token}


def test_amounts_are_exact_and_never_rounded():
    assert q._quantity("100.000001", 6) == (Decimal("100.000001"), "100000001")
    assert q._quantity("0.1", 18)[1] == "100000000000000000"
    for raw in ("", "0", "1e3", "-1", "NaN", "0.0000001", " 1", "1.0e0", "01"):
        with pytest.raises(HTTPException) as error:
            q._quantity(raw, 6)
        assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        q._quantity(100, 6)


def test_uniswap_quote_rejects_other_networks_recipients_and_routes():
    sample = {
        "tokenIn": q.STABLES["USDC"],
        "tokenOut": q.ETH_NATIVE,
        "amount": "100000000",
        "swapper": WALLET,
    }
    valid = mock_quote(sample)
    assert q._read_quote(valid, token_in=sample["tokenIn"], token_out=sample["tokenOut"],
                         amount_units=sample["amount"], wallet=WALLET) == (
                             "210000000000000000", "208950000000000000")
    for key, value in (
        ("routing", "BRIDGE"),
        ("routing", "DUTCH_V3"),
    ):
        bad = {**valid, key: value}
        with pytest.raises(ValueError):
            q._read_quote(bad, token_in=sample["tokenIn"], token_out=sample["tokenOut"],
                          amount_units=sample["amount"], wallet=WALLET)
    for section, field, replacement in (
        ("input", "token", q.STABLES["USDT"]),
        ("input", "amount", "200000000"),
        ("output", "recipient", "0x" + "b" * 40),
        ("output", "minimumAmount", "999999999999999999"),
    ):
        bad = mock_quote(sample)
        bad["quote"][section][field] = replacement
        with pytest.raises(ValueError):
            q._read_quote(bad, token_in=sample["tokenIn"], token_out=sample["tokenOut"],
                          amount_units=sample["amount"], wallet=WALLET)


def test_quote_readiness_does_not_claim_order_authority(monkeypatch):
    monkeypatch.setenv("MININEX_UNISWAP_API_KEY", "secret-not-for-clients")
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET", "secret-not-for-clients")
    flags = q.quote_readiness()
    assert flags["quote_provider_configured"] is True
    assert flags["execution_enabled"] is False
    assert flags["automated_signing_enabled"] is False
    assert "secret-not-for-clients" not in str(flags)


def test_routes_require_auth_and_reject_client_wallet_override(headers):
    client = TestClient(api.app)
    body = {"side": "BUY", "stable": "USDC", "amount": "25.0"}
    assert client.get("/api/live/eth/readiness").status_code == 401
    assert client.post("/api/live/eth/quote", json=body).status_code == 401
    assert client.post("/api/live/eth/quote", headers=headers,
                       json={**body, "swapper": WALLET}).status_code == 422
    assert client.post("/api/live/eth/quote", headers=headers,
                       json={**body, "chain_id": 56}).status_code == 422
    assert client.post("/api/live/eth/quote", headers=headers,
                       json={**body, "side": "SHORT"}).status_code == 422
    data = client.get("/api/live/eth/readiness", headers=headers).json()
    assert data["execution_enabled"] is False


def test_missing_provider_fails_before_privy_queries(monkeypatch):
    monkeypatch.delenv("MININEX_UNISWAP_API_KEY", raising=False)
    with pytest.raises(HTTPException) as err:
        asyncio.run(q.quote_eth_mainnet(DID, side="BUY", stable="USDC", amount="20"))
    assert err.value.status_code == 503


def test_real_provider_quote_for_verified_privy_wallet_without_signing(monkeypatch):
    monkeypatch.setenv("MININEX_UNISWAP_API_KEY", "backend-private-api-key")
    calls = []
    async def user(did):
        calls.append(("privy", did))
        return privy_record(did)
    async def held(did):
        calls.append(("balances", did))
        return balances()
    monkeypatch.setattr(q, "fetch_privy_user", user)
    monkeypatch.setattr(q, "get_wallet_balances", held)
    real_client = httpx.AsyncClient
    def make_client(*args, **kwargs):
        def respond(request):
            assert str(request.url) == q.UNISWAP_QUOTE_URL
            assert request.headers["x-api-key"] == "backend-private-api-key"
            payload = __import__("json").loads(request.content)
            assert payload["tokenInChainId"] == payload["tokenOutChainId"] == 1
            assert payload["swapper"] == payload["recipient"] == WALLET
            assert payload["protocols"] == ["V3"]
            assert payload["permitAmount"] == "EXACT"
            assert payload["tokenIn"] == q.STABLES["USDC"]
            assert payload["tokenOut"] == q.ETH_NATIVE
            assert payload["amount"] == "100000000"
            calls.append(("quote", payload["amount"]))
            return httpx.Response(200, json=mock_quote(payload))
        return real_client(*args, transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(q.httpx, "AsyncClient", make_client)
    result = asyncio.run(q.quote_eth_mainnet(
        DID, side="BUY", stable="USDC", amount="100"))
    assert result["mode"] == "LIVE_QUOTE_ONLY"
    assert result["expected_output_amount"] == "0.21"
    assert result["minimum_output_amount"] == "0.20895"
    assert result["execution_enabled"] is False
    assert result["signing_authorized"] is False
    assert result["transaction_submitted"] is False
    assert calls == [("privy", DID), ("balances", DID), ("quote", "100000000")]
    assert "backend-private-api-key" not in str(result)


def test_insufficient_balance_and_gas_reserve_fail_closed(monkeypatch):
    monkeypatch.setenv("MININEX_UNISWAP_API_KEY", "backend-private-api-key")
    async def user(did):
        return privy_record(did)
    monkeypatch.setattr(q, "fetch_privy_user", user)
    async def held(did):
        return balances(eth="0.005", usdc="15")
    monkeypatch.setattr(q, "get_wallet_balances", held)
    for side, stable, amount in (
        ("BUY", "USDC", "20"),
        ("BUY", "USDC", "10"),
        ("SELL", "USDT", "0.001"),
    ):
        with pytest.raises(HTTPException) as err:
            asyncio.run(q.quote_eth_mainnet(DID, side=side, stable=stable, amount=amount))
        assert err.value.status_code == 409


def test_changed_wallet_and_nonfinite_balance_are_rejected(monkeypatch):
    monkeypatch.setenv("MININEX_UNISWAP_API_KEY", "backend-private-api-key")
    async def user(did):
        return privy_record(did)
    monkeypatch.setattr(q, "fetch_privy_user", user)
    async def mismatched(did):
        value = balances()
        value["wallets"]["ETH"] = "0x" + "b" * 40
        return value
    monkeypatch.setattr(q, "get_wallet_balances", mismatched)
    with pytest.raises(HTTPException) as err:
        asyncio.run(q.quote_eth_mainnet(DID, side="BUY", stable="USDC", amount="25"))
    assert err.value.status_code == 503
    async def nonfinite(did):
        return balances(eth="NaN")
    monkeypatch.setattr(q, "get_wallet_balances", nonfinite)
    with pytest.raises(HTTPException) as err:
        asyncio.run(q.quote_eth_mainnet(DID, side="BUY", stable="USDC", amount="25"))
    assert err.value.status_code == 503


def test_missing_ambiguous_or_unverified_wallet_never_quotes(monkeypatch):
    monkeypatch.setenv("MININEX_UNISWAP_API_KEY", "backend-private-api-key")
    async def user(did):
        return {"id": did, "linked_accounts": []}
    monkeypatch.setattr(q, "fetch_privy_user", user)
    with pytest.raises(HTTPException) as err:
        asyncio.run(q.quote_eth_mainnet(DID, side="BUY", stable="USDC", amount="10"))
    assert err.value.status_code == 409
