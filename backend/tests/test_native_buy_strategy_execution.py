"""Internal native BUY orchestrator: one verified approval or source per event."""
import asyncio
import httpx
import pytest
from fastapi import HTTPException

from backend import native_buy_strategy_execution as m
from backend.tests.test_native_eth_thor_deposit_tx import quote

DID="did:privy:owner"
EVENT="signal_20261121"
RUN="strategy_20261121"


def user_audit(asset="BTC",stable="USDC",amount="50"):
    source=quote(asset=asset,stable=stable,amount=amount)["source_wallet"]
    return {
        "did":DID,"asset":asset,"stable":stable,
        "source_amount":amount,"source_amount_atomic":"50000000",
        "source_wallet":source,"budget_revision":3,
        "policy_sha256":"a"*64,"owner_consent_sha256":"b"*64,
        "privy_grant_sha256":"c"*64,
        "owner_wallet_current":True,
        "exact_remote_policy_verified":True,
        "backend_quorum_current":True,
        "owner_signed_budget_current":True,
        "signer_id":"native_owner_signer","policy_id":"native_restricted_policy",
    }


def run(audit,asset="BTC",stable="USDC",amount="50"):
    async def inner():
        async with httpx.AsyncClient() as client:
            return await m.coordinate_original_native_buy_once(
                DID,asset=asset,stable=stable,amount=amount,
                strategy_run_id=RUN,signal_event_id=EVENT,
                permission_audit=audit,client=client)
    return asyncio.run(inner())


def test_native_buy_missing_owner_consent_never_reads_quote_or_sends(monkeypatch):
    async def invalid(*args,**kwargs):
        raise AssertionError("No quote/wallet calls without user grant")
    monkeypatch.setattr(m,"get_private_native_thornode_terms",invalid)
    with pytest.raises(HTTPException) as err:
        run({})
    assert err.value.status_code==409


def test_missing_first_buy_allowance_claims_one_approval_not_native_source(monkeypatch):
    calls=[]
    q=quote(asset="SOL",stable="USDT",amount="50")
    audit=user_audit(asset="SOL",stable="USDT")
    async def terms(did,**kwargs):
        calls.append("quote")
        assert kwargs==dict(asset="SOL",side="BUY",stable="USDT",amount="50")
        return q
    async def read(did,**kwargs):
        calls.append("read-allowance")
        assert kwargs["quote"] is q
        return {"owner_wallet_needs_erc20_approval":True}
    async def approve(did,**kwargs):
        calls.append("one-approval")
        assert kwargs["permission_audit"] is audit
        return {"mode":"THOR_USDT_ZERO",
                "state":"SUBMITTED","tx_hash":"0x"+"d"*64}
    async def bad(*args,**kwargs):
        raise AssertionError("Never send source while approval is pending")
    monkeypatch.setattr(m,"get_private_native_thornode_terms",terms)
    monkeypatch.setattr(m,"_read_original_thor_buy_allowance_from_quote",read)
    monkeypatch.setattr(m,"submit_original_thor_approval_once",approve)
    monkeypatch.setattr(m,"_preflight_verified_original_eth_buy",bad)
    monkeypatch.setattr(m,"submit_original_eth_native_buy_once",bad)
    result=run(audit,asset="SOL",stable="USDT")
    assert calls==["quote","read-allowance","one-approval"]
    assert result["approval_state"]=="SUBMITTED"
    assert result["source_trade_sent"] is False
    assert result["native_destination_settled"] is False
    assert result["fee_assessed"] is False


def test_approved_original_buy_refreshes_quote_and_claims_one_source(monkeypatch):
    calls=[]
    q=quote()
    audit=user_audit()
    async def terms(did,**kwargs):
        calls.append("quote")
        return q
    async def read(did,**kwargs):
        calls.append("allowance")
        assert kwargs["quote"] is q
        return {"owner_wallet_needs_erc20_approval":False}
    async def preflight(did,**kwargs):
        calls.append("preflight")
        assert kwargs["original"] is q
        return {"current_network":True}
    def identity(**kwargs):
        calls.append("identity")
        assert kwargs["source_wallet"]==audit["source_wallet"]
        return {
            "privy_did":DID,"asset":"BTC","intent_key":"native_"+"e"*64,
            "payload_sha256":"f"*64,"budget_revision":3,
            "owner_consent_sha256":audit["owner_consent_sha256"],
            "privy_grant_sha256":audit["privy_grant_sha256"],
            "source_wallet":audit["source_wallet"],
        }
    def persist(identity):
        calls.append("persist")
        assert identity["payload_sha256"]=="f"*64
        return {"state":"PREPARED"}
    async def submit(did,**kwargs):
        calls.append("one-eth-source")
        assert kwargs["permission_audit"]["payload_sha256"]=="f"*64
        return {"source_tx_hash":"0x"+"d"*64,"journal_state":"SUBMITTED"}
    async def reject(*args,**kwargs):
        raise AssertionError("No approval after actual verified allowance")
    monkeypatch.setattr(m,"get_private_native_thornode_terms",terms)
    monkeypatch.setattr(m,"_read_original_thor_buy_allowance_from_quote",read)
    monkeypatch.setattr(m,"_preflight_verified_original_eth_buy",preflight)
    monkeypatch.setattr(m,"immutable_native_order_identity",identity)
    monkeypatch.setattr(m,"persist_internal_native_order",persist)
    monkeypatch.setattr(m,"submit_original_eth_native_buy_once",submit)
    monkeypatch.setattr(m,"submit_original_thor_approval_once",reject)
    result=run(audit)
    assert calls==["quote","allowance","preflight","identity","persist","one-eth-source"]
    assert result["source_trade_sent"] is True
    assert result["native_destination_settled"] is False
    assert result["fee_assessed"] is False


def test_existing_live_source_can_never_be_rebroadcast(monkeypatch):
    audit=user_audit()
    async def terms(*a,**kw):return quote()
    async def read(*a,**kw):return {"owner_wallet_needs_erc20_approval":False}
    async def preflight(*a,**kw):return {"current_network":True}
    def identity(**kw):
        return {"intent_key":"native_"+"d"*64,
                "payload_sha256":"f"*64}
    def persist(_):return {"state":"SUBMITTED"}
    async def forbidden(*args,**kwargs):
        raise AssertionError("Never re-send a previously claimed original source")
    monkeypatch.setattr(m,"get_private_native_thornode_terms",terms)
    monkeypatch.setattr(m,"_read_original_thor_buy_allowance_from_quote",read)
    monkeypatch.setattr(m,"_preflight_verified_original_eth_buy",preflight)
    monkeypatch.setattr(m,"immutable_native_order_identity",identity)
    monkeypatch.setattr(m,"persist_internal_native_order",persist)
    monkeypatch.setattr(m,"submit_original_eth_native_buy_once",forbidden)
    with pytest.raises(HTTPException) as error:
        run(audit)
    assert error.value.status_code==409
