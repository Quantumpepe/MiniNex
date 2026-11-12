"""Read-only wallet-record status for all MiniNex manual swap networks."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import manual_swap_wallet_readiness as readiness
from backend.manual_swap_routes import ALL_SWAP_ASSETS

DID = "did:privy:swap-wallet-check"
ETH = "0x" + "a"*40
BTC = "bc1q" + "a"*38
SOL = "1"*32


def linked(chain, address, client="privy"):
    return {"type": "wallet", "chain_type": chain, "address": address,
            "wallet_client_type": client}


def user(*records):
    return {"id": DID, "linked_accounts": list(records)}


def test_source_destination_are_independent_and_never_return_addresses():
    account = user(linked("ethereum", ETH), linked("bitcoin-segwit", BTC),
                   linked("solana", SOL))
    for from_asset, to_asset, source_status, target_status in (
        ("BTC", "USDC", "PRESENT", "PRESENT"),
        ("ETH", "LINK", "PRESENT", "PRESENT"),
        ("BNB", "AVAX", "PRESENT", "PRESENT"),
        ("SOL", "BTC", "PRESENT", "PRESENT"),
        ("SOL", "TRX", "PRESENT", "MISSING"),
        ("UNI", "ADA", "PRESENT", "MISSING"),
    ):
        result = readiness.inspect_manual_swap_wallet_records(
            account, DID, from_asset, to_asset)
        assert result["source"]["record_status"] == source_status
        assert result["destination"]["record_status"] == target_status
        assert result["source"]["asset"] == from_asset
        assert result["destination"]["asset"] == to_asset
        assert result["ada_in_progress"] is (from_asset == "ADA" or to_asset == "ADA")
        assert result["ownership_proof_verified"] is False
        assert result["native_signer_verified"] is False
        assert result["network_fees_verified"] is False
        assert result["refund_path_verified"] is False
        assert result["can_submit"] is False
        assert ETH not in str(result) and BTC not in str(result) and SOL not in str(result)
        assert "wallet_id" not in str(result) and "signing_payload" not in str(result)


def test_mismatch_duplicate_external_and_bad_records_fail_closed():
    internal = user(linked("ethereum", ETH))
    assert readiness.inspect_manual_swap_wallet_records(
        internal, DID, "ETH", "USDT")["source"]["record_status"] == "PRESENT"
    duplicate = user(linked("ethereum", ETH), linked("ethereum", ETH))
    assert readiness.inspect_manual_swap_wallet_records(
        duplicate, DID, "ETH", "USDT")["source"]["record_status"] == "AMBIGUOUS"
    external = user(linked("ethereum", ETH, "metamask"))
    assert readiness.inspect_manual_swap_wallet_records(
        external, DID, "ETH", "USDT")["source"]["record_status"] == "MISSING"
    wrong_chain = user(linked("solana", ETH))
    assert readiness.inspect_manual_swap_wallet_records(
        wrong_chain, DID, "ETH", "SOL")["source"]["record_status"] == "MISSING"
    malformed = user(linked("ethereum", "not-an-ethereum-wallet"))
    assert readiness.inspect_manual_swap_wallet_records(
        malformed, DID, "ETH", "USDT")["source"]["record_status"] == "INVALID_FORMAT"
    for records in (None, {}, "not-a-list"):
        with pytest.raises(HTTPException) as exc:
            readiness.inspect_manual_swap_wallet_records(
                {"id": DID, "linked_accounts": records}, DID, "BTC", "ETH")
        assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc:
        readiness.inspect_manual_swap_wallet_records(
            {"id": "did:privy:other", "linked_accounts": []}, DID, "BTC", "ETH")
    assert exc.value.status_code == 503


def test_entire_route_matrix_remains_quote_only_even_with_wallets():
    account = user(linked("ethereum", ETH), linked("bitcoin-segwit", BTC))
    for source in ALL_SWAP_ASSETS:
        for target in ALL_SWAP_ASSETS:
            if source == target:
                continue
            result = readiness.inspect_manual_swap_wallet_records(
                account, DID, source, target)
            assert result["mode"] == "MANUAL_SWAP_WALLET_READINESS_ONLY"
            assert result["linked_wallet_records_checked"] is True
            assert result["signing_authorized"] is False
            assert result["execution_ready"] is False
            assert result["can_submit"] is False


@pytest.fixture
def authorization(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "wallet-readiness-ci",
        "iat": now, "exp": now + 600,
    }, private, algorithm="ES256")
    return {"Authorization": "Bearer " + token}


def test_wallet_readiness_api_requires_auth_and_only_reads_privy(monkeypatch, authorization):
    seen = []
    async def server_user(did):
        seen.append(did)
        return user(linked("ethereum", ETH), linked("bitcoin-segwit", BTC))
    monkeypatch.setattr(readiness, "fetch_privy_user", server_user)
    client = TestClient(api.app)
    path = "/api/wallet/swap/wallet-readiness"
    params = {"source": "BTC", "target": "USDC"}
    assert client.get(path, params=params).status_code == 401
    assert client.get(path, params=params, headers={
        "Authorization": "Bearer fake"}).status_code == 401
    assert client.post(path, params=params, headers=authorization).status_code == 405
    response = client.get(path, params={**params, "address": "0x"+"f"*40},
                          headers=authorization)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json()["source"]["record_status"] == "PRESENT"
    assert response.json()["destination"]["record_status"] == "PRESENT"
    assert "0x"+"f"*40 not in response.text
    assert seen == [DID]
    for source, target in [("BTC", "BTC"), ("ADA", "WBTC"), ("bad", "ETH")]:
        assert client.get(path, params={"source": source, "target": target},
                          headers=authorization).status_code == 422
    assert seen == [DID]  # Invalid pairs make no Privy provider request.


def test_missing_privy_service_returns_503_without_downgrading_to_ready(monkeypatch):
    async def unavailable(_did):
        raise HTTPException(503, "Privy unavailable")
    monkeypatch.setattr(readiness, "fetch_privy_user", unavailable)
    with pytest.raises(HTTPException) as err:
        asyncio.run(readiness.get_manual_swap_wallet_readiness(DID, "ETH", "BTC"))
    assert err.value.status_code == 503
