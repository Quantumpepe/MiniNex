"""Owner-only finite ERC20 token allowances for real ETH AUTO."""
import asyncio
import time
import pytest
from fastapi import HTTPException
from backend import eth_owner_auto_approval as m

NOW=int(time.time())
WALLET="0x"+"a"*40
def prices():
    return {"ethereum":{"usd":2000,"last_updated_at":NOW},
      "usd-coin":{"usd":1,"last_updated_at":NOW},
      "tether":{"usd":1,"last_updated_at":NOW}}
def limits():
    return {"trading_paused":True,"revision":3,
      "planned_capital_usd":"100","max_order_usd":"60",
      "max_daily_turnover_usd":"250"}

@pytest.mark.parametrize("token",["USDC","USDT","WETH"])
def test_approval_is_exact_router_bound_and_cap_is_session_capital(token):
    plan=m.build_owner_approval_plan(token=token,wallet=WALLET,
                                     limits=limits(),prices=prices(),now=NOW)
    assert plan["wallet_address"]==WALLET
    assert plan["approval_cap_usd"]=="100"
    assert plan["spender"]==m.SWAPROUTER02_MAINNET
    assert plan["token_contract"]==m.TOKENS[token]
    assert plan["backend_can_sign"] is False
    assert plan["requires_explicit_owner_privy_confirmation"] is True
    assert plan["expires_at_unix"]==NOW+120
    tx=plan["transaction"]
    assert tx["to"]==m.TOKENS[token] and tx["value"]=="0x0"
    assert tx["data"].startswith("0x095ea7b3")
    assert len(tx["data"])==2+8+64+64
    assert int(tx["data"][-64:],16)==int(plan["amount_atomic"])

def test_weth_price_converts_usd_to_exact_wei():
    plan=m.build_owner_approval_plan(token="WETH",wallet=WALLET,
                                     limits=limits(),prices=prices(),now=NOW)
    assert plan["amount_atomic"]=="50000000000000000"

@pytest.mark.parametrize("change",[
    {"trading_paused":False},{"planned_capital_usd":"0"},
    {"max_order_usd":"200"},{"max_daily_turnover_usd":"30"},
])
def test_nonpaused_or_unfunded_live_limits_do_not_grant_approval(change):
    values=limits();values.update(change)
    with pytest.raises(ValueError):
        m.build_owner_approval_plan(token="USDC",wallet=WALLET,
                                    limits=values,prices=prices(),now=NOW)

def test_only_authenticated_privy_wallet_used(monkeypatch):
    calls=[]
    async def user(did):
        calls.append(did)
        return {"id":did,"linked_accounts":[{
            "type":"wallet","chain_type":"ethereum",
            "wallet_client_type":"privy","address":WALLET}]}
    async def price():return prices()
    monkeypatch.setattr(m,"fetch_privy_user",user)
    monkeypatch.setattr(m,"fetch_reference_prices",price)
    monkeypatch.setattr(m,"get_live_eth_limits",lambda did:limits())
    result=asyncio.run(m.make_eth_owner_approval_plan(
        "did:privy:mine",token="USDC"))
    assert result["token"]=="USDC"
    assert calls==["did:privy:mine"]

def test_malformed_token_fails_before_any_wallet_lookup():
    with pytest.raises(HTTPException) as e:
        asyncio.run(m.make_eth_owner_approval_plan(
            "did:privy:mine",token="UNSAFE"))
    assert e.value.status_code==422
