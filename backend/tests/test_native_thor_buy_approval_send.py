"""Original native first-BUY owner allowance: exact, finite and non-retriable."""
import asyncio
from copy import deepcopy

import httpx
import pytest
from fastapi import HTTPException

from backend import native_thor_buy_approval_send as m
from backend.tests.test_native_thor_buy_approval_plan import plan,NOW
from backend.live_eth_quotes import STABLES


@pytest.mark.parametrize("asset,stable,allowance,mode",[
    ("BTC","USDC",0,"THOR_FINITE"),
    ("SOL","USDT",0,"THOR_FINITE"),
    ("SOL","USDT",10_000_000,"THOR_USDT_ZERO"),
    ("XRP","USDC",0,"THOR_FINITE"),
])
def test_original_first_buy_approval_targets_thor_router_only(
    asset,stable,allowance,mode,
):
    original=plan(asset=asset,stable=stable,allowance=allowance)
    result=m.bound_original_thor_approval(
        original,asset=asset,stable=stable,
        wallet_id="native_owner_approval_wallet",now=NOW)
    assert result["mode"]==mode
    assert result["amount_atomic"]==50_000_000
    assert result["token"]==STABLES[stable].lower()
    assert result["router"]==original["original_router"].lower()
    assert result["wallet"]==original["wallet_address"].lower()
    assert result["source_unsigned_tx"]["to"]==STABLES[stable]
    assert result["source_unsigned_tx"]["data"].startswith("0x095ea7b3")
    assert result["source_unsigned_tx"]["data"][-64:]==(
        "0"*64 if mode=="THOR_USDT_ZERO" else f"{50_000_000:064x}")
    assert result["privy_body"]["caip2"]=="eip155:1"
    assert result["privy_body"]["params"]["transaction"]=={
        "to":STABLES[stable],"data":result["source_unsigned_tx"]["data"],
        "value":"0",
    }
    assert result["approval_is_signed"] is False
    assert result["approval_sent"] is False


@pytest.mark.parametrize("change",[
    {"asset":"SOL"},{"stable":"USDT"},{"chain_id":56},
    {"original_router":"0x"+"f"*40},
    {"wallet_address":"0x"+"f"*40},
    {"official_stable_token_address":"0x"+"f"*40},
    {"approval_amount_atomic":"50000001"},
    {"source_balance_atomic":"1"},
    {"existing_router_allowance_atomic":"50000000"},
    {"source_eth_gas_balance_wei":"0"},
    {"can_submit":True},{"real_token_approval_sent":True},
    {"existing_usdt_allowance_requires_zero_reset":True},
    {"required_sequential_approval_receipts":2},
    {"exact_owner_approval_transaction":None},
    {"exact_owner_approval_reset_transaction":{"data":"0xdead"}},
    {"original_buy_unsigned_sha256":"bad"},
])
def test_native_allowance_source_cannot_be_mutated_into_privy_send(change):
    original=plan()
    corrupted={**deepcopy(original),**change}
    with pytest.raises(ValueError):
        m.bound_original_thor_approval(
            corrupted,asset="BTC",stable="USDC",
            wallet_id="native_owner_approval_wallet",now=NOW)


def test_thor_allowance_quote_expiry_and_invalid_privy_wallet_rejected():
    original=plan()
    with pytest.raises(ValueError):
        m.bound_original_thor_approval(
            original,asset="BTC",stable="USDC",
            wallet_id="native_owner_approval_wallet",now=NOW+150)
    with pytest.raises(ValueError):
        m.bound_original_thor_approval(
            original,asset="BTC",stable="USDC",
            wallet_id="../wrong_wallet",now=NOW)


def test_deterministic_per_signal_approval_stage_key_and_distinct_usdt_reset():
    finite=m.bound_original_thor_approval(
        plan(asset="SOL",stable="USDT",allowance=0),
        asset="SOL",stable="USDT",wallet_id="native_owner_approval_wallet",now=NOW)
    zero=m.bound_original_thor_approval(
        plan(asset="SOL",stable="USDT",allowance=1_000_000),
        asset="SOL",stable="USDT",wallet_id="native_owner_approval_wallet",now=NOW)
    args=dict(did="did:privy:test",asset="SOL",stable="USDT",revision=1)
    key=m._approval_key(**args,signal="strategy_abc",prepared=finite)
    assert key.startswith("appr_") and len(key)==63
    assert key==m._approval_key(**args,signal="strategy_abc",prepared=finite)
    assert key!=m._approval_key(**args,signal="strategy_abc",prepared=zero)
    assert key!=m._approval_key(**args,signal="strategy_xyz",prepared=finite)


def test_missing_user_signed_policy_never_reaches_privy_or_owner_network(monkeypatch):
    async def forbidden(*args,**kwargs):
        raise AssertionError("Owner grant missing: never read Privy wallet or send")
    monkeypatch.setattr(m,"read_original_thor_buy_allowance",forbidden)
    monkeypatch.setattr(m,"fetch_privy_user",forbidden)
    async def attempt():
        async with httpx.AsyncClient() as client:
            await m.submit_original_thor_approval_once(
                "did:privy:test",asset="BTC",stable="USDC",
                amount="50",signal_event_id="strategy_123",
                permission_audit={},client=client)
    with pytest.raises(HTTPException) as err:
        asyncio.run(attempt())
    assert err.value.status_code==409


def test_internal_owner_thor_approval_reuses_original_quote_not_another_venue(monkeypatch):
    from backend.tests.test_native_eth_thor_deposit_tx import quote
    source=quote(asset="BTC",stable="USDC",amount="50")
    calls=[]
    async def unnecessary(*args,**kwargs):
        raise AssertionError("A second original THORChain quote must not be fetched")
    async def verify(did,**kwargs):
        calls.append((did,kwargs))
        return {"owner_wallet_needs_erc20_approval":False}
    monkeypatch.setattr(m,"read_original_thor_buy_allowance",unnecessary)
    monkeypatch.setattr(m,"_read_original_thor_buy_allowance_from_quote",verify)
    async def run(q):
        async with httpx.AsyncClient() as client:
            return await m.submit_original_thor_approval_once(
                "did:privy:owner",asset="BTC",stable="USDC",
                amount="50",signal_event_id="strategy_123",
                permission_audit={
                    "did":"did:privy:owner","asset":"BTC","stable":"USDC",
                    "source_amount":"50","source_wallet":source["source_wallet"],
                    "budget_revision":1,"policy_sha256":"a"*64,
                    "owner_wallet_current":True,
                    "exact_remote_policy_verified":True,
                    "backend_quorum_current":True,
                    "owner_signed_budget_current":True,
                },client=client,original_quote=q)
    assert asyncio.run(run(source))["state"]=="ALREADY_APPROVED"
    assert len(calls)==1
    assert calls[0][1]["quote"] is source
    for bad in (
        {**source,"source_wallet":"0x"+"f"*40},
        {**source,"destination_asset":"ETH"},
        {**source,"mode":"INDICATIVE_PUBLIC_PRICE"},
        {**source,"amount":"100"},
    ):
        with pytest.raises(HTTPException) as err:
            asyncio.run(run(bad))
        assert err.value.status_code==409
    assert len(calls)==1
