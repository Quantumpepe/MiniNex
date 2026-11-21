"""Original BTC/SOL/XRP BUY binds owner quote and ETH source without auto-live."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import httpx
import pytest
from fastapi import HTTPException

from backend import native_eth_buy_privy_dispatch as m
from backend.tests.test_native_eth_native_buy_preflight import inspect,preview,NOW
from backend.tests.test_native_eth_thor_deposit_tx import quote


def ready(asset="BTC",stable="USDC",amount="50"):
    original=quote(asset=asset,stable=stable,amount=amount)
    source=inspect(
        unsigned=preview(asset=asset,stable=stable,amount=amount),
        original_quote=original,
    )
    order={
        "asset":asset,"side":"BUY","stable":stable,
        "chain":"ethereum-mainnet","status":"PREPARED",
        "source_wallet":original["source_wallet"],
        "destination_wallet":original["destination_wallet"],
        "input_amount":Decimal(amount),"quote_expires_at":original["quote_expires_at"],
        "immutable_quote_sha256":m._original_quote_digest(original),
        "payload_sha256":"a"*64,
    }
    return order,original,source


@pytest.mark.parametrize("asset,stable,amount",[
    ("BTC","USDC","50"),("SOL","USDT","0.01"),("XRP","USDC","50"),
])
def test_exact_native_buy_request_uses_original_thor_router(asset,stable,amount):
    order,original,source=ready(asset,stable,amount)
    assert source["network_checked_at_unix"]==NOW
    url,body,nonce,digest=m.bound_original_native_buy_rpc(
        order,original,source,wallet_id="native_test_wallet001",now=NOW)
    assert url=="https://api.privy.io/v1/wallets/native_test_wallet001/rpc"
    assert body["method"]=="eth_sendTransaction"
    assert body["caip2"]=="eip155:1"
    assert body["params"]["transaction"]=={
        "to":original["inbound_router"],"value":"0",
        "data":source["unsigned_transaction"]["data"],
    }
    assert nonce==42
    assert len(digest)==64
    assert digest==m.bound_original_native_buy_rpc(
        order,original,source,wallet_id="native_test_wallet001",now=NOW)[3]


@pytest.mark.parametrize("tamper",[
    "quote_memo","quote_router","wallet","destination","amount",
    "proof_router","proof_vault","proof_nonce","proof_gas",
    "proof_allowance","proof_balance","proof_digest","proof_stale",
    "proof_live_flag","proof_data","order_status","order_chain",
])
def test_native_buy_cannot_sign_changed_or_stale_source(tamper):
    order,original,source=ready()
    order,original,source=deepcopy(order),deepcopy(original),deepcopy(source)
    if tamper=="quote_memo":original["memo"]="SWAP:BTC.BTC:wrong/wrong"
    if tamper=="quote_router":original["inbound_router"]="0x"+"f"*40
    if tamper=="wallet":order["source_wallet"]="0x"+"f"*40
    if tamper=="destination":order["destination_wallet"]="wrong"
    if tamper=="amount":order["input_amount"]=Decimal("49")
    if tamper=="proof_router":source["router"]="0x"+"f"*40
    if tamper=="proof_vault":source["vault"]="0x"+"f"*40
    if tamper=="proof_nonce":source["unsigned_transaction"]["nonce"]="0x2b"
    if tamper=="proof_gas":source["unsigned_transaction"]["gas"]="0x1234"
    if tamper=="proof_allowance":source["source_allowance_atomic"]="1"
    if tamper=="proof_balance":source["verified_eth_balance_wei"]="1"
    if tamper=="proof_digest":source["unsigned_transaction_sha256"]="f"*64
    if tamper=="proof_stale":source["network_checked_at_unix"]=NOW-40
    if tamper=="proof_live_flag":source["can_submit"]=True
    if tamper=="proof_data":source["unsigned_transaction"]["data"]="0xdeadbeef"
    if tamper=="order_status":order["status"]="SENDING"
    if tamper=="order_chain":order["chain"]="bitcoin-mainnet"
    with pytest.raises(ValueError):
        m.bound_original_native_buy_rpc(
            order,original,source,wallet_id="native_test_wallet001",now=NOW)


def test_native_buy_xrp_usdt_and_expired_quote_rejected():
    order,original,source=ready("XRP","USDC")
    order["stable"]="USDT"
    with pytest.raises(ValueError):
        m.bound_original_native_buy_rpc(
            order,original,source,wallet_id="native_test_wallet001",now=NOW)
    order,original,source=ready()
    with pytest.raises(ValueError):
        m.bound_original_native_buy_rpc(
            order,original,source,wallet_id="native_test_wallet001",now=NOW+130)


def test_no_owner_policy_means_no_remote_wallet_or_send(monkeypatch):
    async def forbidden(*args,**kwargs):
        raise AssertionError("No Privy lookup without original native owner grant")
    monkeypatch.setattr(m,"get_original_native_privy_signer_status",forbidden)
    monkeypatch.setattr(m,"fetch_privy_user",forbidden)
    async def run():
        async with httpx.AsyncClient() as client:
            await m.submit_original_eth_native_buy_once(
                "did:privy:owner",asset="BTC",intent_key="native_"+"a"*64,
                quote={},preflight={},permission_audit={},client=client)
    with pytest.raises(HTTPException) as e:
        asyncio.run(run())
    assert e.value.status_code==409
