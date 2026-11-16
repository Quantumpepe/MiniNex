"""Independent real ETH+WETH+stable balances, no synthetic profit/Paper."""
import asyncio
import time
import pytest
from fastapi import HTTPException
from backend import eth_live_wallet_equity as p

NOW=int(time.time())
OWNER="0x"+"ab"*20
OTHER="0x"+"cd"*20
def holdings():
    return {"wallet_ownership_verified":True,"wallets":{"ETH":OWNER},
      "balances":{
        "ETH":{"status":"verified","balance":"0.1"},
        "USDC":{"status":"verified","balance":"25"},
        "USDT":{"status":"verified","balance":"15"}}}
def weth():
    return {"wallet_identity_verified":True,"wallet_address":OWNER,"source_token":"WETH",
            "side":"SELL","verified_source_balance":"0.02"}
def prices():
    return {"ethereum":{"usd":2000,"last_updated_at":NOW},
            "usd-coin":{"usd":1,"last_updated_at":NOW},
            "tether":{"usd":1,"last_updated_at":NOW}}

def test_real_wallet_includes_native_weth_and_both_stables_once():
    value=p.calculate_eth_wallet_equity(
        holdings(),weth(),prices(),now=NOW)
    assert value["total_reference_value_usd"]=="280.000000"
    assert value["includes_paper"] is False
    assert value["includes_other_chains"] is False
    assert [row["asset"] for row in value["balances"]]==[
        "ETH","WETH","USDC","USDT"]
    assert value["realized_profit_usd"] is None
    assert value["performance_fee_usd"] is None

@pytest.mark.parametrize("asset",["ETH","USDC","USDT"])
def test_missing_confirmed_token_balance_cannot_be_invented(asset):
    b=holdings();b["balances"][asset]["status"]="unavailable"
    with pytest.raises(ValueError):
        p.calculate_eth_wallet_equity(b,weth(),prices(),now=NOW)

def test_unverified_weth_balance_is_not_zero_or_ignored():
    record=weth();record["wallet_identity_verified"]=False
    with pytest.raises(ValueError):
        p.calculate_eth_wallet_equity(holdings(),record,prices(),now=NOW)

def test_nonfinite_balance_cannot_enter_real_portfolio():
    b=holdings();b["balances"]["ETH"]["balance"]="NaN"
    with pytest.raises(ValueError):
        p.calculate_eth_wallet_equity(b,weth(),prices(),now=NOW)

def test_real_eth_equity_reads_actual_wallet_and_weth(monkeypatch):
    calls=[]
    async def fetch(did):calls.append("wallet");return holdings()
    async def inspect(did,*,side,stable,amount):
        assert side=="SELL" and stable=="USDC"
        calls.append("weth");return weth()
    async def market():calls.append("market");return prices()
    monkeypatch.setattr(p,"get_wallet_balances",fetch)
    monkeypatch.setattr(p,"inspect_router02_approval",inspect)
    monkeypatch.setattr(p,"fetch_reference_prices",market)
    result=asyncio.run(p.read_owner_eth_live_wallet_value("did:privy:owner"))
    assert result["real_wallet_balances_verified"] is True
    assert calls==["wallet","weth","market"]

@pytest.mark.parametrize("bad_owner",[OTHER,None,"not-an-address"])
def test_eth_wallet_equity_rejects_mismatched_weth_owner(bad_owner):
    other=weth()
    other["wallet_address"]=bad_owner
    with pytest.raises(ValueError,match="wallet identity mismatch"):
        p.calculate_eth_wallet_equity(holdings(),other,prices(),now=NOW)


def test_eth_wallet_identity_accepts_checksum_case_difference():
    b=holdings()
    b["wallets"]["ETH"]=OWNER.upper().replace("0X","0x")
    assert p.calculate_eth_wallet_equity(b,weth(),prices(),now=NOW)[
        "total_reference_value_usd"]=="280.000000"
