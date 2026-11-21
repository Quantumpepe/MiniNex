"""Native ETH-stable router approval planning is exact and never signs."""
import asyncio

import pytest
from fastapi import HTTPException

from backend import native_thor_buy_approval_plan as m
from backend import app as api
from fastapi.testclient import TestClient
from backend.live_eth_quotes import STABLES
from backend.native_eth_thor_deposit_tx import encode_original_eth_native_buy_deposit
from backend.tests.test_native_eth_thor_deposit_tx import NOW,ETH,ROUTER,VAULT,quote

DID="did:privy:owner"
AMOUNT="50"
TOKEN=STABLES["USDC"]


def plan(*,asset="BTC",stable="USDC",allowance=0,balance=50_000_000,
         chain="0x1",eth=10**17):
    original=quote(asset=asset,stable=stable,amount=AMOUNT)
    unsigned=encode_original_eth_native_buy_deposit(
        original,asset=asset,stable=stable,amount=AMOUNT,now=NOW)
    return m.inspect_original_thor_buy_allowance(
        quote=original,unsigned=unsigned,asset=asset,
        stable=stable,amount=AMOUNT,now=NOW,chain_id=chain,
        token_allowance="0x"+f"{allowance:064x}",
        token_balance="0x"+f"{balance:064x}",
        eth_balance=hex(eth))


def test_real_native_buy_missing_allowance_returns_exact_finite_erc20_draft():
    ready=plan(allowance=0)
    assert ready["mode"]=="THORCHAIN_ORIGINAL_ERC20_APPROVAL_PREVIEW_ONLY"
    assert ready["owner_wallet_needs_erc20_approval"] is True
    assert ready["approval_amount_atomic"]=="50000000"
    expected={
        "from":ETH,"to":TOKEN,"chainId":1,"value":"0x0",
        "data":"0x095ea7b3"+ROUTER[2:].lower().rjust(64,"0")+
            f"{50_000_000:064x}",
    }
    assert ready["exact_owner_approval_transaction"]==expected
    assert ready["source_eth_gas_balance_wei"]==str(10**17)
    for flag in (
        "exact_approval_gas_verified","current_remote_privy_trade_policy_verified",
        "durable_approval_send_claimed","real_token_approval_sent",
        "actual_buy_source_sent","execution_enabled","can_submit",
    ):assert ready[flag] is False


def test_already_approved_original_thor_buy_needs_no_second_approval():
    result=plan(allowance=50_000_000)
    assert result["owner_wallet_needs_erc20_approval"] is False
    assert result["exact_owner_approval_transaction"] is None
    assert result["required_sequential_approval_receipts"]==0
    assert result["can_submit"] is False


def test_partial_usdt_allowance_requires_finalized_zero_reset_before_approve():
    result=plan(asset="SOL",stable="USDT",allowance=10_000_000)
    assert result["owner_wallet_needs_erc20_approval"] is True
    assert result["existing_usdt_allowance_requires_zero_reset"] is True
    assert result["required_sequential_approval_receipts"]==2
    reset=result["exact_owner_approval_reset_transaction"]
    approval=result["exact_owner_approval_transaction"]
    assert reset["from"]==approval["from"]
    assert reset["to"]==approval["to"]==STABLES["USDT"]
    assert reset["data"][:-64]==approval["data"][:-64]
    assert reset["data"][-64:]=="0"*64
    assert approval["data"][-64:]==f"{50_000_000:064x}"
    assert result["real_token_approval_sent"] is False
    assert result["execution_enabled"] is False


def test_zero_or_sufficient_usdt_allowance_does_not_need_reset():
    for existing in (0,50_000_000):
        result=plan(asset="SOL",stable="USDT",allowance=existing)
        assert result["existing_usdt_allowance_requires_zero_reset"] is False
        assert result["exact_owner_approval_reset_transaction"] is None
        assert result["required_sequential_approval_receipts"]==(1 if existing==0 else 0)


@pytest.mark.parametrize("change",[
    {"allowance":0,"balance":49_000_000},
    {"allowance":0,"eth":0},
    {"allowance":0,"chain":"0x38"},
])
def test_wrong_chain_missing_funds_or_no_gas_never_proposes_approval(change):
    with pytest.raises(ValueError):plan(**change)


def test_modified_quote_memo_or_router_never_approves_an_unexpected_spender():
    original=quote(asset="BTC",stable="USDC",amount=AMOUNT)
    unsigned=encode_original_eth_native_buy_deposit(
        original,asset="BTC",stable="USDC",amount=AMOUNT,now=NOW)
    corrupted=dict(original,inbound_router="0x"+"f"*40)
    with pytest.raises(ValueError):
        m.inspect_original_thor_buy_allowance(
            quote=corrupted,unsigned=unsigned,asset="BTC",
            stable="USDC",amount=AMOUNT,now=NOW,chain_id="0x1",
            token_balance="0x"+f"{50_000_000:064x}",
            token_allowance="0x"+f"{0:064x}",eth_balance=hex(10**17))


def test_private_owner_allowance_readback_pins_same_ethereum_block(monkeypatch):
    orig=quote(asset="SOL",stable="USDT",amount="50")
    async def terms(did,**kwargs):
        assert did==DID
        assert kwargs=={"asset":"SOL","side":"BUY","stable":"USDT","amount":"50"}
        return orig
    seen=[]
    async def rpc(client,*,method,params,req_id):
        seen.append((method,params,req_id))
        if method=="eth_chainId":return "0x1"
        if method=="eth_blockNumber":return "0x123abc"
        if method=="eth_getBalance":return hex(10**17)
        if method=="eth_call":return "0x"+f"{100_000_000:064x}"
        raise AssertionError("Approval read must never send a transaction")
    monkeypatch.setattr(m,"get_private_native_thornode_terms",terms)
    monkeypatch.setattr(m,"_rpc",rpc)
    monkeypatch.setattr(m.time,"time",lambda:NOW)
    result=asyncio.run(m.read_original_thor_buy_allowance(
        DID,asset="SOL",stable="USDT",amount="50"))
    assert result["owner_wallet_needs_erc20_approval"] is False
    assert result["asset"]=="SOL"
    assert len(seen)==5
    assert all(row[1][-1]=="0x123abc" for row in seen[2:])
    assert not any("send" in row[0].lower() for row in seen)


def test_owner_authenticated_native_buy_approval_api_never_sends(monkeypatch):
    calls=[]
    def bearer(value):
        if value!="Bearer native_owner":
            raise HTTPException(401,"Unauthorized")
        return {"user_id":DID}
    async def inspect(did,*,asset,stable,amount):
        calls.append((did,asset,stable,amount))
        return {"mode":"THORCHAIN_ORIGINAL_ERC20_APPROVAL_PREVIEW_ONLY",
                "execution_enabled":False,"can_submit":False}
    monkeypatch.setattr(api,"verify_privy_bearer",bearer)
    monkeypatch.setattr(api,"read_original_thor_buy_allowance",inspect)
    client=TestClient(api.app)
    endpoint="/api/live/native/buy-approval/SOL"
    body={"side":"BUY","stable":"USDT","amount":"50"}
    assert client.post(endpoint,json=body).status_code==401
    assert client.post(endpoint,json=body,headers={"Authorization":"Bearer bad"}).status_code==401
    assert client.post(endpoint,json={**body,"side":"SELL"},headers={
        "Authorization":"Bearer native_owner"}).status_code==422
    assert client.post(endpoint,json={**body,"router":"0xdead"},headers={
        "Authorization":"Bearer native_owner"}).status_code==422
    response=client.post(endpoint,json=body,headers={"Authorization":"Bearer native_owner"})
    assert response.status_code==200
    assert response.headers["cache-control"]=="private, no-store"
    assert response.json()["can_submit"] is False
    assert client.get(endpoint,headers={"Authorization":"Bearer native_owner"}).status_code==405
    assert calls==[(DID,"SOL","USDT","50")]


def test_native_xrp_unavailable_usdt_pair_is_rejected_before_quote(monkeypatch):
    async def unexpected(*args,**kwargs):
        raise AssertionError("An unsupported XRP pair must not fetch a quote")
    monkeypatch.setattr(m,"get_private_native_thornode_terms",unexpected)
    with pytest.raises(HTTPException) as error:
        asyncio.run(m.read_original_thor_buy_allowance(
            DID,asset="XRP",stable="USDT",amount="1"))
    assert error.value.status_code==422
