"""Shared actual Uniswap V3 mainnet route tests for BNB/POL/AVAX."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from backend import native_evm_v3_quote as evm
from backend.live_asset_registry import get_asset

DID="did:privy:native-evm"
WALLET="0x"+"ab"*20


@pytest.mark.parametrize("asset,stable,chain,decimals",[
    ("BNB","USDT",56,18),
    ("POL","USDC",137,6),
    ("AVAX","USDC",43114,6),
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_three_mainnet_evm_assets_share_transaction_encoder(
    asset,stable,chain,decimals,side
):
    route=evm._route(asset,stable)
    assert route.chain_id==get_asset(asset).chain_id==chain
    assert route.stable_decimals==decimals
    tx=evm.build_native_v3_transaction(
        asset=asset,stable=stable,side=side,wallet=WALLET,
        amount_in=10**decimals, min_out=100, fee=3000,
    )
    assert tx["chainId"]==chain
    assert tx["from"]==WALLET and tx["to"]==route.router
    assert tx["data"].startswith("0x04e45aaf")
    words=[int(tx["data"][10+i*64:10+(i+1)*64],16) for i in range(7)]
    assert words[0]==int(route.stable_contract if side=="BUY" else route.wrapped,16)
    assert words[1]==int(route.wrapped if side=="BUY" else route.stable_contract,16)
    assert words[3]==int(WALLET,16)
    assert words[4]==10**decimals
    assert words[5]==100 and words[6]==0


@pytest.mark.parametrize("asset,stable",[
    ("BNB","USDC"),("POL","USDT"),("AVAX","USDT"),("ETH","USDC"),
])
def test_native_issuer_scope_is_not_confused_across_evm_chains(asset,stable):
    with pytest.raises(HTTPException):
        evm._route(asset,stable)


@pytest.fixture
def evm_chain(monkeypatch):
    calls=[]
    async def owner(did):
        assert did==DID
        return {"id":DID}
    monkeypatch.setattr(evm,"fetch_privy_user",owner)
    monkeypatch.setattr(evm,"verified_addresses",
                        lambda user,did:{"ETH":WALLET})
    async def rpc(client,url,method,params,rid):
        calls.append((method,rid))
        route=next(r for r in evm.CHAINS.values()
                   if evm._RPC[{"bsc-mainnet":"BNB",
                               "polygon-pos-mainnet":"POL",
                               "avalanche-c-mainnet":"AVAX"}[r.network]][2]==url)
        if method=="eth_chainId":
            return hex(route.chain_id)
        if method=="eth_getCode":
            return "0x60016000"
        if method=="eth_getBalance":
            return hex(10**18)
        if method=="eth_call":
            data=params[0]["data"]
            if data=="0x313ce567":
                return "0x"+format(route.stable_decimals,"064x")
            if data.startswith("0x70a08231"):
                return "0x"+format(1000*10**18,"064x")
            if data.startswith("0xc6a5026a") or rid in (500,3000):
                output=(2*10**18 if rid==500 else 10**18)
                return "0x"+"".join(format(v,"064x") for v in (
                    output,2**96,2,100000))
        raise AssertionError((method,rid))
    monkeypatch.setattr(evm,"_rpc",rpc)
    class Client:
        def __init__(self,**kwargs): assert kwargs["follow_redirects"] is False
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
    monkeypatch.setattr(evm.httpx,"AsyncClient",Client)
    return calls


@pytest.mark.parametrize("asset,stable",[
    ("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_native_evm_actual_chain_quote_and_wallet_unsigned_transaction(
    asset,stable,side,evm_chain
):
    result=asyncio.run(evm.quote_native_evm_trade(
        DID,asset=asset,stable=stable,side=side,
        amount="1",include_user_transaction=True,
    ))
    assert result["asset"]==asset
    assert result["chain_id"]==evm.CHAINS[asset].chain_id
    assert result["mode"]=="SHARED_NATIVE_EVM_V3_QUOTE"
    assert result["wallet_identity_verified"] is True
    assert result["wallet_funds_verified"] is True
    assert result["onchain_quote_verified"] is True
    assert result["transaction"]["to"]==evm.CHAINS[asset].router
    assert result["transaction"]["chainId"]==evm.CHAINS[asset].chain_id
    assert result["transaction"]["from"]==WALLET
    assert result["transaction"]["data"].startswith("0x04e45aaf")
    assert result["native_wrap_unwrap_not_yet_settled"] is True
    assert result["source_token_allowance_not_checked"] is True
    assert result["execution_enabled"] is False
    assert result["wallet_signed"] is False
    assert ("eth_chainId",1) in evm_chain
    assert ("eth_getBalance",7) in evm_chain
    assert ("eth_call",6) in evm_chain


def test_bad_evm_chain_id_prevents_quote(monkeypatch,evm_chain):
    original=evm._rpc
    async def wrong(client,url,method,params,rid):
        if method=="eth_chainId":
            return "0x1"
        return await original(client,url,method,params,rid)
    monkeypatch.setattr(evm,"_rpc",wrong)
    with pytest.raises(HTTPException) as err:
        asyncio.run(evm.quote_native_evm_trade(
            DID,asset="BNB",side="BUY",stable="USDT",amount="1"))
    assert err.value.status_code==502


def test_native_evm_endpoint_is_authenticated_and_does_not_broadcast():
    from backend.app import app
    route=next(r for r in app.routes
               if r.path=="/api/live/evm/native-user-transaction/{asset}")
    text=inspect.getsource(route.endpoint)
    assert "verify_privy_bearer" in text
    assert "quote_native_evm_trade" in text
    assert "broadcast_privy" not in text
