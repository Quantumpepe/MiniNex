"""Real wallet-led LIVE ETH strategist. No PAPER position or fake fill."""
from decimal import Decimal
import asyncio
import pytest
from backend import eth_live_strategy_tick as live

class Market:
    def __init__(self,trend="up"):
        self.trend=trend
        self.now=1000
    def snapshot(self,*,now):
        price=100.5 if self.trend=="up" else 99
        return {"markets":{"ETH":{"market_data_valid":True,"price_usdt":price}}}
    def history(self,asset,*,limit):
        assert asset=="ETH"
        return [{"timestamp":t,"price_usdt":
          (100+(t-810)/190*0.5 if self.trend=="up" else
           100-(t-810)/190)} for t in range(810,1001,5)]

def limits():return {"trading_paused":False,"planned_capital_usd":"200",
                      "max_order_usd":"100"}

def allowance(side="BUY"):
    return {"wallet_identity_verified":True,"side":side,"stable":"USDC",
      "router_allowance_sufficient":True,"source_funds_sufficient":True,
      "conservative_native_gas_reserve_met":True,
      "verified_source_balance":"1.5"}

def balance():
    return {"wallet_ownership_verified":True,"balances":{
        "USDC":{"status":"verified","balance":"80"}}}

def test_real_exchange_uptrend_generates_buy():
    assert live.verified_live_eth_momentum(Market("up"),now=1000)["action"]=="BUY"

def test_actual_downtrend_generates_sell():
    assert live.verified_live_eth_momentum(Market("down"),now=1000)["action"]=="SELL"

def test_live_buy_uses_wallet_cash_not_paper_balance():
    dec={"action":"BUY","market_price":"100.5"}
    assert live.live_trade_quantity(decision=dec,live_limits=limits(),
             balances=balance(),allowance=allowance())=="40.000000"
    bad=balance();bad["balances"]["USDC"]["status"]="simulated"
    assert live.live_trade_quantity(decision=dec,live_limits=limits(),
             balances=bad,allowance=allowance()) is None

def test_live_sell_uses_actual_weth_inventory():
    dec={"action":"SELL","market_price":"100"}
    assert live.live_trade_quantity(decision=dec,live_limits=limits(),
         balances=balance(),allowance=allowance("SELL"))=="1.000000000000000000"

def test_stop_and_no_token_allowance_cannot_trade():
    l=limits();l["trading_paused"]=True
    assert live.live_trade_quantity(decision={"action":"BUY"},
        live_limits=l,balances=balance(),allowance=allowance()) is None
    a=allowance();a["router_allowance_sufficient"]=False
    assert live.live_trade_quantity(decision={"action":"BUY"},
        live_limits=limits(),balances=balance(),allowance=a) is None

def test_real_tick_calls_executor_once(monkeypatch):
    actions=[]
    monkeypatch.setattr(live,"get_live_eth_limits",lambda did:limits())
    async def wallet(did):return balance()
    async def inspect(did,*,side,stable,amount):
        actions.append(("check",side,amount));return allowance(side)
    async def execute(did,signal,*,client):
        actions.append(("send",signal.side,signal.amount))
        return {"state":"SUBMITTED","tx_hash":"0x"+"a"*64}
    monkeypatch.setattr(live,"get_wallet_balances",wallet)
    monkeypatch.setattr(live,"inspect_router02_approval",inspect)
    monkeypatch.setattr(live,"execute_eth_auto_signal",execute)
    result=asyncio.run(live.execute_live_eth_strategy_tick(
        "did:privy:me",market=Market(),run_id="run_live_2026",
        stable="USDC",client=None,now=1000))
    assert result["state"]=="SUBMITTED"
    assert actions[-1]==("send","BUY","40.000000")
    assert len([x for x in actions if x[0]=="send"])==1

def test_live_tick_refuses_paper_market():
    class FakeMarket:
        def snapshot(self,*,now):return {"markets":{"ETH":{
            "market_data_valid":False,"price_usdt":200}}}
    result=asyncio.run(live.execute_live_eth_strategy_tick(
        "did:privy:me",market=FakeMarket(),run_id="run_live_2026",
        stable="USDC",client=None,now=1000))
    assert result["state"]=="WAIT"
