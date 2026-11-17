"""Live EVM readiness audits independent Privy ownership, journal and quorum.

Read-only. Neither a stored grant nor a remote signer alone authorizes trades.
"""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from backend import shared_evm_live_readiness as a

DID="did:privy:sample-owner"
ADDR="0x"+"ab"*20
WALLET_ID="wallet_1234567"
CHAIN={"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1}


def user():
    return {"id":DID,"linked_accounts":[{
        "type":"wallet","chain_type":"ethereum",
        "wallet_client_type":"privy",
        "address":ADDR,"id":WALLET_ID,"delegated":True,
    }]}


def metadata():
    return {"id":WALLET_ID,"chain_type":"ethereum",
            "address":ADDR,"owner":{"user_id":DID},
            "additional_signers":[]}


def permission(asset):
    return {
        "asset":asset,"chain_id":CHAIN[asset],
        "stored_wallet_id":WALLET_ID,
        "stored_wallet_address":ADDR,
        "user_consent_recorded":True,
        "server_policy_digest_matched":True,
        "signer_grant_recorded":True,
        "policy_current":True,
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_all_five_evm_assets_independently_recheck_wallet_and_quorum(asset):
    r=a.assess_evm_auto_readiness(
        asset=asset,user=user(),wallet=metadata(),
        permission=permission(asset),
        quorum={"remote_quorum_public_key_verified":True},
    )
    assert r["asset"]==asset
    assert r["checks"]["owner_privy_wallet_verified"] is True
    assert r["checks"]["permission_record_matches_privy_owner_wallet"] is True
    assert r["checks"]["registered_backend_quorum_public_key_verified"] is True
    assert r["checks"]["current_remote_privy_policy_rules_verified"] is False
    assert r["checks"]["durable_per_coin_budget_reserved"] is False
    assert r["live_auto_execution_enabled"] is False
    assert r["can_broadcast"] is False


@pytest.mark.parametrize("mutation",[
    lambda p:p.update(stored_wallet_address="0x"+"cd"*20),
    lambda p:p.update(stored_wallet_id="wallet_other123"),
    lambda p:p.update(user_consent_recorded=False),
    lambda p:p.update(server_policy_digest_matched=False),
    lambda p:p.update(signer_grant_recorded=False),
    lambda p:p.update(policy_current=False),
])
def test_wrong_owner_or_permission_never_claims_ready(mutation):
    p=permission("BNB")
    mutation(p)
    r=a.assess_evm_auto_readiness(
        asset="BNB",permission=p,user=user(),wallet=metadata(),
        quorum={"remote_quorum_public_key_verified":True})
    assert r["blockers"]
    assert r["can_broadcast"] is False
    assert (r["checks"]["permission_record_matches_privy_owner_wallet"] is False
            or r["checks"]["per_asset_user_consent_recorded"] is False
            or r["checks"]["per_asset_policy_digest_matched_in_ledger"] is False
            or r["checks"]["per_asset_grant_recorded_and_not_expired"] is False)


def test_missing_privy_signer_quorum_is_reported_not_ignored():
    r=a.assess_evm_auto_readiness(
        asset="LINK",permission=permission("LINK"),
        user=user(),wallet=metadata(),quorum={})
    assert r["checks"]["registered_backend_quorum_public_key_verified"] is False


def test_on_demand_preflight_reads_server_evidence_only(monkeypatch):
    called=[]
    def record(did,*,asset):
        called.append(("permissions",did,asset))
        return permission(asset)
    async def privy_user(did):
        called.append(("user",did))
        return user()
    async def privy_wallet(wallet_id):
        called.append(("wallet",wallet_id))
        return metadata()
    async def quorum(asset):
        assert asset=="POL"
        called.append(("quorum",))
        return {"remote_quorum_public_key_verified":True}
    monkeypatch.setattr(a,"read_owner_evm_permission_status",record)
    monkeypatch.setattr(a,"fetch_privy_user",privy_user)
    monkeypatch.setattr(a,"fetch_privy_wallet_metadata",privy_wallet)
    monkeypatch.setattr(a,"check_privy_backend_quorum",quorum)
    r=asyncio.run(a.get_owner_evm_auto_readiness(DID,"POL"))
    assert r["asset"]=="POL"
    assert called==[
        ("permissions",DID,"POL"),("user",DID),
        ("wallet",WALLET_ID),("quorum",)]
    assert r["can_broadcast"] is False


def test_missing_wallet_never_calls_signing_or_sends(monkeypatch):
    async def missing_user(did):
        return {"id":did,"linked_accounts":[]}
    monkeypatch.setattr(a,"read_owner_evm_permission_status",
                        lambda did,*,asset:permission(asset))
    monkeypatch.setattr(a,"fetch_privy_user",missing_user)
    with pytest.raises(ValueError):
        asyncio.run(a.get_owner_evm_auto_readiness(DID,"BNB"))


def test_endpoint_requires_authenticated_owner_and_is_get_only():
    from backend.app import app
    route=next(route for route in app.routes
        if route.path=="/api/live/evm/auto-readiness/{asset}")
    src=inspect.getsource(route.endpoint)
    assert route.methods=={"GET"}
    assert "verify_privy_bearer" in src
    assert "get_owner_evm_auto_readiness" in src
    assert "send_privy" not in src


@pytest.mark.parametrize("asset",["ETH","DOGE","BTC"])
def test_unsupported_asset_cannot_reuse_eth_grant(asset):
    with pytest.raises(HTTPException) as err:
        asyncio.run(a.get_owner_evm_auto_readiness(DID,asset))
    assert err.value.status_code==404
