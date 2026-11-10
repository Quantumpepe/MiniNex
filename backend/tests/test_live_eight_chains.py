"""Eight native-coin roadmap, bounded stablecoin routes, and disabled GO LIVE."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import live_asset_registry as reg


@pytest.fixture
def headers(monkeypatch):
    signer=ec.generate_private_key(ec.SECP256R1())
    public=signer.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":"did:privy:seven-chain-test","sid":"seven-chain-ci",
        "iat":now,"exp":now+600,
    },signer,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_exact_coin_order_native_networks_and_immutable_registry():
    assert reg.ASSET_ORDER == ("BTC","ETH","BNB","POL","SOL","XRP","ADA","TRX")
    assert tuple(row.symbol for row in reg.REGISTRY)==reg.ASSET_ORDER
    assert len(reg.BY_SYMBOL)==len(reg.ASSET_ORDER)==8
    expected={
        "BTC":("bitcoin-mainnet","bitcoin-utxo",None),
        "ETH":("ethereum-mainnet","evm",1),
        "BNB":("bsc-mainnet","evm",56),
        "POL":("polygon-pos-mainnet","evm",137),
        "SOL":("solana-mainnet","solana",None),
        "XRP":("xrpl-mainnet","xrpl",None),
        "ADA":("cardano-mainnet","cardano-eutxo",None),
        "TRX":("tron-mainnet","tron",None),
    }
    for symbol,(chain,route,chain_id) in expected.items():
        coin=reg.get_asset(symbol)
        assert (coin.blockchain,coin.execution_family,coin.chain_id)==(
            chain,route,chain_id)
        assert coin.symbol==symbol
    for symbol in ("DOGE","MATIC","BTC-BEP20","WRAPPED_BTC","USDT","USDC",""):
        with pytest.raises(HTTPException) as err:
            reg.get_asset(symbol)
        assert err.value.status_code==404


def test_stablecoin_network_issuer_edges_do_not_get_mislabeled():
    coins=reg.BY_SYMBOL
    assert coins["BTC"].settlement_route=="external-provider-required"
    assert "USDC" in coins["ETH"].stablecoin_candidates
    assert "USDT" in coins["ETH"].stablecoin_candidates
    assert coins["XRP"].stablecoin_candidates==("USDC",)
    assert coins["ADA"].stablecoin_candidates==("USDCx",)
    assert "USDCx" != "USDC"
    assert coins["TRX"].stablecoin_candidates==("USDT",)
    assert "USDC" not in coins["TRX"].stablecoin_candidates
    assert coins["BNB"].chain_id==56
    assert "USDC" not in coins["BNB"].stablecoin_candidates
    assert coins["POL"].stablecoin_candidates==("USDC",)
    assert coins["POL"].chain_id==137
    assert coins["POL"].wallet_approach.startswith("Privy EVM")
    assert "bridged USDC.e" in coins["POL"].special_requirements


def test_no_env_var_can_enable_global_or_per_asset_trades(monkeypatch):
    for env in ("MININEX_GO_LIVE","ENABLE_LIVE_TRADING","MININEX_LIVE_TRX",
                "MININEX_PRIVY_ETH_POLICY_SHA256"):
        monkeypatch.setenv(env,"true")
    monkeypatch.setenv("MININEX_UNISWAP_API_KEY","test-private-api-key")
    report=reg.get_global_live_readiness()
    assert report["ordered_assets"]==list(reg.ASSET_ORDER)
    assert report["global_trading_enabled"] is False
    assert report["global_activation_blocked"] is True
    assert report["operator_go_live_available"] is False
    assert report["one_click_activation_enabled"] is False
    assert report["all_chains_ready"] is False
    assert report["assets"]["ETH"]["readonly_eth_quote_provider_configured"] is True
    assert all(not row["live_execution_ready"] for row in report["assets"].values())
    assert all(not row["allow_user_trading"] for row in report["assets"].values())
    assert all(not any(row["safety_gates"].values()) for row in report["assets"].values())
    assert "test-private-api-key" not in str(report)


def test_dispatch_stays_locked_for_all_eight_coins():
    for asset in reg.ASSET_ORDER:
        with pytest.raises(HTTPException) as error:
            reg.require_live_engine(asset)
        assert error.value.status_code==503


def test_live_status_endpoint_is_authenticated_read_only(headers):
    client=TestClient(api.app)
    assert client.get("/api/live/readiness").status_code==401
    assert client.get("/api/live/readiness",headers={
        "Authorization":"Bearer invalid"}).status_code==401
    response=client.get("/api/live/readiness",headers=headers)
    assert response.status_code==200
    assert response.headers["cache-control"]=="private, no-store"
    report=response.json()
    assert list(report["assets"])==list(reg.ASSET_ORDER)
    assert report["global_trading_enabled"] is False
    assert report["operator_go_live_available"] is False
    assert client.post("/api/live/readiness",headers=headers,json={}).status_code==405
    assert not any(route.path in (
        "/api/live/go-live","/api/live/activate","/api/live/execute",
        "/api/operator/go-live","/api/live/TRX/send")
        for route in api.app.routes)
