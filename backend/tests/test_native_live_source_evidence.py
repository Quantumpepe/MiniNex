"""Native source-budget readbacks: one native wallet and owner ETH funds."""
import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException

from backend import native_live_source_evidence as m

ASSETS=("BTC","SOL","XRP","TRX")
DID="did:privy:budget_test"


def oracle(now):
    return {m.COIN_IDS[x]:{"usd":(1.0 if x in ("USDC","USDT") else 100.0),
                            "last_updated_at":now-10}
            for x in (*ASSETS,"USDC","USDT")}


def test_reference_rates_require_recent_exact_coin_and_stable_snapshot():
    data=oracle(1_800_000_000)
    assert m._native_rate(data,"BTC",1_800_000_000)==Decimal(100)
    assert m._native_rate(data,"USDT",1_800_000_000)==Decimal(1)
    with pytest.raises(ValueError):
        m._native_rate(data,"BTC",1_800_000_400)
    data[m.COIN_IDS["USDT"]]["usd"]=0.85
    with pytest.raises(ValueError):
        m._native_rate(data,"USDT",1_800_000_000)


def test_cross_stable_unclosed_native_buys_are_not_ignored_after_setting_change():
    rows=[
        {"asset":"SOL","side":"BUY","stable":"USDC",
         "status":"SOURCE_FINALIZED","input_amount":Decimal("150")},
        {"asset":"SOL","side":"BUY","stable":"USDT",
         "status":"SENDING","input_amount":Decimal("25")},
    ]
    p={"USDC":Decimal("1.001"),"USDT":Decimal("0.999")}
    assert m._pending_usd(rows,"SOL",p)==Decimal("175.125")
    with pytest.raises(ValueError):
        m._pending_usd(rows+[{"asset":"XRP","side":"BUY",
             "stable":"USDT","status":"SENDING",
             "input_amount":Decimal("2")}],"TRX",p)


@pytest.mark.parametrize("asset,stable",[
    ("BTC","USDC"),("SOL","USDT"),("XRP","USDC"),
    ("TRX","USDT"),
])
def test_one_native_coin_source_uses_verified_both_wallets_and_pending(
    monkeypatch,asset,stable,
):
    calls=[]
    async def user(did):
        calls.append("owner")
        return {"id":did,"linked_accounts":[]}
    def native_wallets(owned,did):
        assert owned["id"]==did
        return {asset:"native-address"}
    async def wallet(did):
        calls.append("eth")
        return {"wallet_ownership_verified":True,
                "wallets":{"ETH":"0x"+"a"*40},
                "balances":{
                    stable:{"balance":"1000","status":"verified",
                            "network":"ethereum-mainnet"},
                    "BTC":{"balance":"1","status":"verified","network":"bitcoin-mainnet"}},
                **{"wallets":{"ETH":"0x"+"a"*40,"BTC":"native-address"}}}
    async def native(client,coin,address):
        calls.append("native")
        assert (coin,address)==(asset,"native-address")
        return "1"
    async def prices(asset_):
        calls.append("usd")
        assert asset_==asset
        return {asset_:Decimal(100),"USDC":Decimal(1),"USDT":Decimal(1)}
    def pending(did,coin):
        calls.append("pending")
        return [{"asset":asset,"side":"BUY","stable":stable,
                 "status":"SUBMITTED","input_amount":Decimal(50)}]
    monkeypatch.setattr(m,"fetch_privy_user",user)
    monkeypatch.setattr(m,"verified_native_addresses",native_wallets)
    monkeypatch.setattr(m,"_exact_owner_wallets",lambda *a:("native-address","0x"+"a"*40))
    monkeypatch.setattr(m,"read_pending_ethereum_funding",lambda *a,**kw:
        {"pending_wallet_stable_atomic":"0"})
    monkeypatch.setattr(m,"get_wallet_balances",wallet)
    monkeypatch.setattr(m,"_read_one",native)
    monkeypatch.setattr(m,"fresh_native_usd_prices",prices)
    monkeypatch.setattr(m,"_pending_owner_native_buys",pending)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m.verified_owner_native_buy_amount(
                DID,asset=asset,stable=stable,
                budget_percent=50 if asset=="BTC" else None,
                budget_usd=500 if asset!="BTC" else None,client=client)
    assert asyncio.run(run())==("425" if asset=="BTC" else "350")
    assert sorted(calls)==sorted(("owner","eth","usd","pending")+
                                 (() if asset=="BTC" else ("native",)))


def test_missing_native_wallet_prevents_any_network_trade(monkeypatch):
    async def user(did):return {"id":did,"linked_accounts":[]}
    def wallets(_,did):return {"TRX":None}
    async def forbidden(*a,**kw):
        raise AssertionError("No ETH funds/price lookup for missing owner wallet")
    monkeypatch.setattr(m,"fetch_privy_user",user)
    monkeypatch.setattr(m,"verified_native_addresses",wallets)
    monkeypatch.setattr(m,"get_wallet_balances",forbidden)
    async def run():
        async with httpx.AsyncClient() as client:
            await m.verified_owner_native_buy_amount(
                DID,asset="TRX",stable="USDC",budget_usd=1000,client=client)
    with pytest.raises(HTTPException) as err:
        asyncio.run(run())
    assert err.value.status_code==409


def test_native_sell_inventory_uses_native_atomic_credits_not_stable_input():
    rows=[dict(side="BUY",destination_received_atomic=200000000),
          dict(side="SELL",input_amount="0.75"),
          dict(side="BUY",destination_received_atomic=25000000)]
    position=m.finalized_native_position_atomic(rows,asset="BTC")
    assert position==150000000
    assert m.exact_native_sell_amount(asset="BTC",position_atomic=position,
                                      wallet_balance="1.25")=="1.25"
    assert m.exact_native_sell_amount(asset="BTC",position_atomic=0,
                                      wallet_balance="2") is None


def test_confirmed_native_fee_and_partial_refund_reduce_inventory():
    rows=[
        dict(side="BUY",status="DESTINATION_FINALIZED",destination_received_atomic=10000000),
        dict(side="SELL",status="DESTINATION_FINALIZED",input_amount="2",source_network_fee_atomic="10000"),
        dict(side="SELL",status="REFUNDED_VERIFIED",input_amount="3",destination_received_atomic="2900000",source_network_fee_atomic="20000"),
        dict(side="BUY",status="REFUNDED_VERIFIED",destination_received_atomic="100000000"),
    ]
    assert m.finalized_native_position_atomic(rows,asset="TRX")==7870000
