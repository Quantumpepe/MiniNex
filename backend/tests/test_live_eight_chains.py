"""Twelve-coin roadmap, bounded routes, and disabled GO LIVE."""
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
    assert reg.ASSET_ORDER == ("BTC","ETH","BNB","SOL","XRP","TRX","POL","AVAX","LINK","DOGE","UNI","ADA")
    assert tuple(row.symbol for row in reg.REGISTRY)==reg.ASSET_ORDER
    assert len(reg.BY_SYMBOL)==len(reg.ASSET_ORDER)==12
    expected={
        "BTC":("bitcoin-mainnet","bitcoin-utxo",None),
        "ETH":("ethereum-mainnet","evm",1),
        "BNB":("bsc-mainnet","evm",56),
        "POL":("polygon-pos-mainnet","evm",137),
        "SOL":("solana-mainnet","solana",None),
        "XRP":("xrpl-mainnet","xrpl",None),
        "ADA":("cardano-mainnet","cardano-eutxo",None),
        "TRX":("tron-mainnet","tron",None),
        "AVAX":("avalanche-c-mainnet","evm",43114),
        "LINK":("ethereum-mainnet","evm-erc20",1),
        "DOGE":("dogecoin-mainnet","dogecoin-utxo",None),
        "UNI":("ethereum-mainnet","evm-erc20",1),
    }
    for symbol,(chain,route,chain_id) in expected.items():
        coin=reg.get_asset(symbol)
        assert (coin.blockchain,coin.execution_family,coin.chain_id)==(
            chain,route,chain_id)
        assert coin.symbol==symbol
    for symbol in ("MATIC","BTC-BEP20","WRAPPED_BTC","USDT","USDC",""):
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
    assert coins["AVAX"].chain_id==43114
    assert coins["LINK"].execution_family=="evm-erc20"
    assert coins["UNI"].execution_family=="evm-erc20"
    assert coins["DOGE"].settlement_route=="external-provider-required"


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


def test_dispatch_stays_locked_for_all_twelve_coins():
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


def test_all_twelve_coin_manual_swap_route_pairs_classified_without_execution():
    from backend.manual_swap_routes import (
        STABLE_ASSETS, ALL_SWAP_ASSETS, build_manual_swap_route,
    )
    assert STABLE_ASSETS == ("USDC", "USDT")
    assert ALL_SWAP_ASSETS == reg.ASSET_ORDER + STABLE_ASSETS
    assert len(ALL_SWAP_ASSETS) == 14
    assert sum(1 for source in ALL_SWAP_ASSETS for target in ALL_SWAP_ASSETS
               if source != target) == 182
    for source in ALL_SWAP_ASSETS:
        for target in ALL_SWAP_ASSETS:
            if source == target:
                with pytest.raises(HTTPException):
                    build_manual_swap_route(source, target)
                continue
            row = build_manual_swap_route(source, target)
            assert row["source"]["symbol"] == source
            assert row["destination"]["symbol"] == target
            expected = ("SAME_CHAIN" if row["source"]["network"] ==
                        row["destination"]["network"] else "CROSS_CHAIN")
            assert row["route_class"] == expected
            assert row["execution_ready"] is False
            assert row["signing_authorized"] is False
            assert row["transaction_built"] is False
            assert row["can_submit"] is False
            assert row["network_fee_verified"] is False
            assert row["guaranteed_minimum_received"] is False
            assert len(row["required_checks"]) >= 5
            assert row["quote_preview_implemented"] is True
            assert row["active_liquidity_not_guaranteed"] is True
            assert row["quote_endpoint"] in (
                "/api/live/eth/quote", "/api/live/btc/quote",
                "/api/wallet/swap/live-quote")
            if {source, target} not in (
                {"ETH", "USDC"}, {"ETH", "USDT"},
                {"BTC", "USDC"}, {"BTC", "USDT"},
            ):
                assert row["quote_asset"] == "MULTICHAIN"
                assert row["quote_endpoint"] == "/api/wallet/swap/live-quote"
    # ERC-20 USDC/USDT currently refer to ETH holdings, not native TRON,
    # XRPL, Bitcoin, Polygon, BSC or Cardano stablecoin balances.
    for symbol in ("TRX", "XRP", "BTC", "DOGE", "ADA", "BNB", "POL", "AVAX", "SOL"):
        for stable in STABLE_ASSETS:
            route = build_manual_swap_route(symbol, stable)
            assert route["route_class"] == "CROSS_CHAIN"
            assert route["destination"]["network"] == "ethereum-mainnet"
            assert "cross_chain_finality_and_refund_path" in route["required_checks"]
    for left, right in (("ETH", "LINK"), ("LINK", "UNI"), ("UNI", "USDT"),
                        ("USDC", "ETH")):
        route = build_manual_swap_route(left, right)
        assert route["route_class"] == "SAME_CHAIN"
        assert route["source"]["network"] == route["destination"]["network"]
    for a, b in (("DOGE", "ETH"), ("BTC", "SOL"), ("ADA", "POL"),
                 ("XRP", "TRX")):
        route = build_manual_swap_route(a, b)
        assert route["route_class"] == "CROSS_CHAIN"
    for bad in ("USDCx", "USDC.e", "WBTC", "BTC.BTC", "MATIC", "BEP20-BTC", ""):
        with pytest.raises(HTTPException):
            build_manual_swap_route(bad, "ETH")


def test_authenticated_manual_swap_route_api_has_no_execution_or_provider_calls(headers):
    client = TestClient(api.app)
    path = "/api/wallet/swap/route"
    assert client.get(path, params={"source": "BTC", "target": "USDC"}).status_code == 401
    response = client.get(path, headers=headers,
                          params={"source": "BTC", "target": "USDC"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    row = response.json()
    assert row["source"]["network"] == "bitcoin-mainnet"
    assert row["destination"]["network"] == "ethereum-mainnet"
    assert row["quote_endpoint"] == "/api/live/btc/quote"
    assert row["can_submit"] is False
    assert client.get(path, headers=headers,
                      params={"source": "LINK", "target": "UNI"}).status_code == 200
    for src, dst in (("BTC", "BTC"), ("USDCx", "ADA"), ("ETH", "WBTC"),
                     ("ETH", ""), ("", "ETH")):
        assert client.get(path, headers=headers,
            params={"source": src, "target": dst}).status_code == 422
    for bad_param in ({"source": "ETH"}, {"target": "BTC"}):
        assert client.get(path, headers=headers, params=bad_param).status_code == 422
    assert client.post(path, headers=headers,
                       json={"source": "BTC", "target": "USDC"}).status_code == 405
    assert not any(route.path == "/api/wallet/swap/send" for route in api.app.routes)
