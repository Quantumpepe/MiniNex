"""Actual native signal uses one user budget, one remote grant, one ETH source."""
import asyncio
from datetime import datetime,timezone

import httpx
import pytest

from backend import native_live_auto_tick as m

DID="did:privy:original_owner"
SHA="a"*64


def session(asset="SOL"):
    return {
        "privy_did":DID,"asset":asset,
        "stable":"USDC" if asset!="TRX" else "USDT",
        "status":"ACTIVE","mode":"AUTO","horizon":"15m",
        "started_by_owner_at":datetime(2026,11,21,tzinfo=timezone.utc),
        "budget_revision":5,"budget_usd":700,
        "budget_percent":100,
    }


def grant(row,amount):
    return {
        "audit_mode":"CURRENT_NATIVE_OWNER_SIGNED_GRANT",
        "did":row["privy_did"],"asset":row["asset"],
        "stable":row["stable"],"budget_revision":5,
        "source_amount":amount,"source_amount_atomic":"100000000",
        "source_wallet":"0x"+"a"*40,
        "signer_id":"mininex_current_native_signer",
        "policy_id":"mininex_original_native_policy",
        "owner_consent_sha256":SHA,"privy_grant_sha256":SHA,
        "policy_sha256":SHA,
        "owner_wallet_current":True,
        "exact_remote_policy_verified":True,
        "backend_quorum_current":True,
        "owner_signed_budget_current":True,
        "user_consent_current":True,
    }


@pytest.mark.parametrize("asset",["BTC","SOL","XRP","TRX","DOGE"])
def test_one_real_native_buy_signal_uses_owner_budget_and_grant(monkeypatch,asset):
    row=session(asset)
    called=[]
    def signal(market,*,asset,horizon,now):
        called.append("signal")
        return {"action":"BUY","reason":"LIVE_CONFIRMED_UPTREND"}
    async def money(did,**kwargs):
        called.append("actual_wallets")
        assert kwargs["budget_percent"]==(100 if asset=="BTC" else None)
        assert kwargs["budget_usd"]==(None if asset=="BTC" else 700)
        return "100"
    async def independent(did,**kwargs):
        called.append("privy_remote_grant")
        return grant(row,kwargs["amount"])
    async def send(did,**kwargs):
        called.append("durable_one_source")
        assert kwargs["permission_audit"]["audit_mode"]=="CURRENT_NATIVE_OWNER_SIGNED_GRANT"
        assert kwargs["stable"]==row["stable"]
        return {"state":"SUBMITTED"}
    monkeypatch.setattr(m,"verified_live_native_momentum",signal)
    monkeypatch.setattr(m,"verified_owner_native_buy_amount",money)
    monkeypatch.setattr(m,"coordinate_original_native_buy_once",send)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m.execute_owner_native_live_tick(
                row,market=object(),client=client,
                grant_auditor=independent,now=1800000000)
    assert asyncio.run(run())=={"state":"SUBMITTED"}
    assert called==["signal","actual_wallets",
                   "privy_remote_grant","durable_one_source"]


def test_no_remote_wallet_consent_never_sends(monkeypatch):
    row=session()
    called=[]
    monkeypatch.setattr(m,"verified_live_native_momentum",
                        lambda *a,**kw:{"action":"BUY"})
    async def funds(*a,**kw):return "100"
    async def auditor(did,**kwargs):
        return {**grant(row,kwargs["amount"]),
                "exact_remote_policy_verified":False}
    async def forbid(*a,**kw):
        raise AssertionError("No original owner Privy grant, no network send")
    monkeypatch.setattr(m,"verified_owner_native_buy_amount",funds)
    monkeypatch.setattr(m,"coordinate_original_native_buy_once",forbid)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m.execute_owner_native_live_tick(
                row,market=object(),client=client,
                grant_auditor=auditor,now=1800000000)
    with pytest.raises(ValueError):
        asyncio.run(run())


def test_native_sell_signal_waits_without_unsupported_wallet_signer(monkeypatch):
    row=session("TRX")
    def signal(*a,**kw):return {"action":"SELL"}
    async def forbid(*a,**kw):
        raise AssertionError("Do not sign native SELL without signer and settlement")
    monkeypatch.setattr(m,"verified_live_native_momentum",signal)
    monkeypatch.setattr(m,"verified_owner_native_buy_amount",forbid)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m.execute_owner_native_live_tick(
                row,market=object(),client=client,
                grant_auditor=forbid,now=1800000000)
    assert asyncio.run(run())["state"]=="WAIT"


def test_btc_uses_only_its_own_market_source():
    class Stub:
        def __init__(self,key):self.key=key
        def snapshot(self,*,now):
            return ({self.key:{"price_usdt":99}} if self.key!="BTC"
                    else {"markets":{"BTC":{"price_usdt":100}}})
        def history(self,asset,*,limit):
            return [{"timestamp":1,"price_usdt":5}]
    market=m.OriginalNativeMarket(Stub("BTC"),Stub("DOGE"))
    assert market.snapshot(now=1800000000)["markets"]["BTC"]["price_usdt"]==100
    assert market.snapshot(now=1800000000)["markets"]["DOGE"]["price_usdt"]==99
    assert market.history("BTC",limit=10)[0]["price_usdt"]==5


def test_inactive_or_unselected_native_wallet_never_looks_up_market():
    row={**session("DOGE"),"status":"PAUSED"}
    with pytest.raises(ValueError):
        m._original_owner_native_session(row)
    row={**session("TRX"),"stable":"USDC"}
    with pytest.raises(ValueError):
        m._original_owner_native_session(row)
    row={**session("SOL"),"budget_revision":0}
    with pytest.raises(ValueError):
        m._original_owner_native_session(row)


def test_grant_does_not_accept_frontend_true_or_policy_without_remote_audit():
    row=session("BTC")
    assert m._independent_native_owner_permission(
        grant(row,"100"),row=row,amount="100")["policy_sha256"]==SHA
    for false in (
        {"owner_wallet_current":False},
        {"source_amount":"101"},
        {"asset":"DOGE"},
        {"budget_revision":4},
        {"policy_sha256":"bad"},
        {"audit_mode":"FRONTEND_CONFIRMATION"},
    ):
        with pytest.raises(ValueError):
            m._independent_native_owner_permission(
                {**grant(row,"100"),**false},row=row,amount="100")
