"""Unified native readback reports real source evidence, never live rights."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import native_source_readiness as m
from backend import app as api

DID = "did:privy:source_review_owner"
NETWORKS = {
    "BTC": "bitcoin-mainnet",
    "SOL": "solana-mainnet",
    "XRP": "xrpl-mainnet",
}


def owner(asset):
    return {
        "asset": asset, "network": NETWORKS[asset],
        "original_wallet_link_verified": True,
        "original_wallet_remote_owner_verified": True,
        "original_public_key_matches_wallet": asset in ("BTC","SOL"),
        "privy_native_wallet_api_supported": asset in ("BTC","SOL"),
        "additional_signer_reference_present": False,
        "can_submit": False, "execution_enabled": False,
    }


def source(asset, side, stable, amount):
    units = m._exact_source_units(asset, side, amount)
    base = {
        "asset": asset, "side": side, "stable": stable,
        "source_wallet": "native-owned-source",
        "destination_wallet": "owned-destination",
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
        "wallet_signed": False,
        "memo": "do-not-expose",
        "inbound_vault": "do-not-expose",
        "signed_wire_base64": "do-not-expose",
        "psbt_base64": "do-not-expose",
    }
    if side == "BUY":
        base.update({
            "mode": "THORCHAIN_ETH_ORIGINAL_NATIVE_BUY_NETWORK_PREFLIGHT",
            "source_amount_atomic": units,
            "chain_id_verified": True,
            "original_router_allowance_verified": True,
            "source_funds_and_gas_verified": True,
            "pending_nonce_and_gas_estimate_verified": True,
        })
    elif asset == "BTC":
        base.update({
            "mode": "ORIGINAL_BTC_PSBT_OWNER_PREFLIGHT_READ_ONLY",
            "source_amount_sats": units,
            "unsigned_psbt": {"psbt_created": True},
            "current_esplora_unspent_reported": True,
            "bip143_signature_digest_preview": {
                "signature_requests_performed": False},
        })
    elif asset == "SOL":
        base.update({
            "mode": "ORIGINAL_SOL_SOURCE_UNSIGNED_MAINNET_PREFLIGHT",
            "source_amount_lamports": units,
            "solana_mainnet_genesis_verified": True,
            "current_owner_blockhash_and_balance_verified": True,
            "real_message_fee_verified": True,
        })
    else:
        base.update({
            "mode": "ORIGINAL_XRP_SOURCE_UNSIGNED_MAINNET_PREFLIGHT",
            "source_amount_drops": units,
            "xrpl_mainnet_id_verified": True,
            "source_account_and_fee_verified": True,
        })
    return base


def _install(monkeypatch, *, changed_owner=None, changed_source=None):
    calls = []

    async def status(did, *, asset):
        calls.append(("owner", asset, did))
        report = owner(asset)
        if changed_owner:
            changed_owner(report)
        return report

    async def execute(asset, did, side, stable, amount):
        calls.append(("source", asset, side, stable, amount))
        result = source(asset, side, stable, amount)
        if changed_source:
            changed_source(result)
        return result

    async def btc(did, *, stable, amount):
        return await execute("BTC", did, "SELL", stable, amount)

    async def sol(did, *, stable, amount):
        return await execute("SOL", did, "SELL", stable, amount)

    async def xrp(did, *, amount):
        return await execute("XRP", did, "SELL", "USDC", amount)

    async def eth(did, *, asset, stable, amount):
        return await execute(asset, did, "BUY", stable, amount)

    monkeypatch.setattr(m, "get_original_native_privy_signer_status", status)
    monkeypatch.setattr(m, "prepare_original_btc_unsigned_psbt", btc)
    monkeypatch.setattr(m, "prepare_original_sol_unsigned_owner_transfer", sol)
    monkeypatch.setattr(m, "prepare_original_xrp_unsigned_owner_payment", xrp)
    monkeypatch.setattr(m, "preflight_original_native_eth_buy", eth)
    return calls


@pytest.mark.parametrize("asset,side,stable", [
    ("BTC","BUY","USDC"),("BTC","SELL","USDC"),
    ("BTC","SELL","USDT"),("SOL","BUY","USDT"),
    ("SOL","SELL","USDC"),
])
def test_shared_native_source_preflight_selects_owner_and_exact_chain(
    monkeypatch, asset, side, stable,
):
    calls = _install(monkeypatch)
    result = asyncio.run(m.read_original_native_source_readiness(
        DID, asset=asset, side=side, stable=stable, amount="1.25",
    ))
    assert result["asset"] == asset and result["side"] == side
    assert result["stable"] == stable
    assert result["original_source_preflight_verified"] is True
    assert result["original_native_owner_wallet_link_verified"] is True
    assert result["original_native_owner_public_key_matches_wallet"] is True
    assert result["documented_privy_native_wallet_chain_type_supported"] is True
    assert result["can_submit"] is False
    assert result["source_network"] == (
        "ethereum-mainnet" if side == "BUY" else NETWORKS[asset])
    assert result["destination_network"] == (
        NETWORKS[asset] if side == "BUY" else "ethereum-mainnet")
    assert calls == [
        ("owner",asset,DID),("source",asset,side,stable,"1.25"),
    ]
    for flag in (
        "exact_native_privy_policy_contents_verified",
        "user_signed_coin_budget_ledger_verified",
        "durable_original_order_claim_verified", "source_chain_broadcast_verified",
        "crosschain_swap_filled", "destination_settlement_finalized",
        "performance_fee_assessed", "wallet_signed",
        "execution_enabled", "can_submit",
    ):
        assert result[flag] is False
    for forbidden in (
        "memo", "inbound_vault", "unsigned_psbt", "signed_wire_base64",
        "psbt_base64", "original_compiled_message", "signature_digests",
    ):
        assert forbidden not in result


@pytest.mark.parametrize("asset,side,stable", [
    ("ADA","SELL","USDC"), ("TRX","BUY","USDT"),
    ("BTC","HOLD","USDC"), ("XRP","SELL","USDT"),
    ("SOL","BUY","BTC"), ("BTC","SELL","USDC!"),
])
def test_wrong_coin_or_stable_never_calls_sources(monkeypatch,asset,side,stable):
    calls = _install(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(m.read_original_native_source_readiness(
            DID,asset=asset,side=side,stable=stable,amount="1",
        ))
    assert exc.value.status_code == 422
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("original_wallet_link_verified", False),
    ("original_wallet_remote_owner_verified", False),
    ("original_public_key_matches_wallet", False),
    ("privy_native_wallet_api_supported", False),
    ("asset", "XRP"), ("can_submit", True), ("execution_enabled", True),
])
def test_wallet_must_be_verified_and_never_claim_execution(monkeypatch,field,value):
    calls = _install(monkeypatch,changed_owner=lambda x:x.update({field:value}))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(m.read_original_native_source_readiness(
            DID,asset="BTC",side="SELL",stable="USDC",amount="0.1"))
    assert exc.value.status_code == 409
    assert not any(call[0] == "source" for call in calls)


@pytest.mark.parametrize("field,value", [
    ("can_submit",True),("wallet_signed",True),
    ("execution_enabled",True),("source_transaction_sent",True),
    ("asset","SOL"),("current_esplora_unspent_reported",False),
])
def test_untrusted_btc_source_cannot_be_reported_as_ready(monkeypatch,field,value):
    _install(monkeypatch,changed_source=lambda x:x.update({field:value}))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(m.read_original_native_source_readiness(
            DID,asset="BTC",side="SELL",stable="USDC",amount="0.1"))
    assert exc.value.status_code == 409


def test_authenticated_private_readiness_api_is_read_only(monkeypatch):
    _install(monkeypatch)

    def verify(token):
        if token != "Bearer local_native_test":
            raise HTTPException(401, "Unauthorized")
        return {"user_id": DID}

    monkeypatch.setattr(api, "verify_privy_bearer", verify)
    client = TestClient(api.app)
    url = "/api/live/native/source-readiness/SOL"
    data = {"side":"SELL","stable":"USDC","amount":"3"}
    assert client.post(url,json=data).status_code == 401
    invalid = client.post(url,json=data,headers={
        "Authorization":"Bearer wrong"})
    assert invalid.status_code == 401
    response = client.post(url,json=data,headers={
        "Authorization":"Bearer local_native_test"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["original_source_preflight_verified"] is True
    assert response.json()["can_submit"] is False
    assert client.get(url,headers={
        "Authorization":"Bearer local_native_test"}).status_code == 405
    assert client.post(url,json={**data,"recipient":"external-wallet"},headers={
        "Authorization":"Bearer local_native_test"}).status_code == 422


@pytest.mark.parametrize("amount,side,asset", [
    ("0","SELL","BTC"),("-1","SELL","BTC"),
    ("1.000000001","SELL","BTC"),
    ("1.0000000001","SELL","SOL"),
    ("1.0000001","SELL","XRP"),
    ("0.0000001","BUY","SOL"),
    ("NaN","SELL","XRP"),
    ("1e5","BUY","BTC"),
])
def test_native_readiness_never_rounds_or_accepts_fake_input(
    monkeypatch,amount,side,asset,
):
    calls=_install(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(m.read_original_native_source_readiness(
            DID,asset=asset,side=side,stable="USDC",amount=amount))
    assert exc.value.status_code == 422
    assert calls == []


@pytest.mark.parametrize("asset,side,field", [
    ("BTC","BUY","source_amount_atomic"),
    ("BTC","SELL","source_amount_sats"),
    ("SOL","SELL","source_amount_lamports"),
    ("XRP","SELL","source_amount_drops"),
    ("XRP","BUY","source_amount_atomic"),
])
def test_original_route_must_match_user_exact_atomic_amount(
    monkeypatch,asset,side,field,
):
    _install(monkeypatch,changed_source=lambda r:r.update({field:"1"}))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(m.read_original_native_source_readiness(
            DID,asset=asset,side=side,stable="USDC",amount="1.25"))
    assert exc.value.status_code == 409


def test_original_native_wallet_network_not_substitutable(monkeypatch):
    _install(monkeypatch,changed_owner=lambda r:r.update(network="ethereum-mainnet"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(m.read_original_native_source_readiness(
            DID,asset="SOL",side="SELL",stable="USDC",amount="1"))
    assert exc.value.status_code == 409


def test_xrp_readiness_requires_real_xrpl_signer_support_not_raw_math(monkeypatch):
    calls = _install(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(m.read_original_native_source_readiness(
            DID, asset="XRP", side="SELL", stable="USDC", amount="2"))
    assert exc.value.status_code == 409
    assert calls == [("owner", "XRP", DID)]
