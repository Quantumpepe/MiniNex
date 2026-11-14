"""Account-owned Router02 executable calldata export for Privy user signing.

Only an authenticated user can obtain calldata. NO backend send is possible.
"""
import asyncio
from copy import deepcopy
from fastapi import HTTPException
import pytest
from backend import app as api
from backend.eth_router02_live_quote import build_unsigned_router02_swap
from backend.eth_swaprouter02_preflight import SWAPROUTER02_MAINNET

DID="did:privy:manual-user-execution"
WALLET="0x"+"a"*40

def test_builder_is_wallet_restricted():
    tx=build_unsigned_router02_swap(wallet=WALLET,side="BUY",stable="USDC",
         amount_in=10000000,min_out=1000000000000000,fee=500)
    assert tx["from"]==WALLET
    assert tx["to"].lower()==SWAPROUTER02_MAINNET.lower()
    assert tx["chainId"]==1
    assert tx["value"]=="0"
    assert tx["data"].startswith("0x")
    assert len(tx["data"])==2+2*(4+7*32)

def test_user_transaction_endpoint_authenticates_and_returns_only_unsigned(monkeypatch):
    calls=[]
    monkeypatch.setattr(api,"verify_privy_bearer",
      lambda token: {"user_id":DID})
    async def quote(did,*,side,stable,amount,include_user_transaction=False):
        calls.append((did,side,stable,amount,include_user_transaction))
        return {"mode":"ETH_ROUTER02_POLICY_ABI_QUOTE_ONLY",
          "wallet_identity_verified":True,"unsigned_swap_validated":True,
          "transaction":{"from":WALLET,"to":SWAPROUTER02_MAINNET,"chainId":1,
            "value":"0x0","data":"0xabcdef"},
          "can_submit":False,"execution_enabled":False}
    monkeypatch.setattr(api,"make_policy_compatible_eth_quote",quote)
    request=api.EthLiveQuoteRequest(side="BUY",stable="USDC",amount="10")
    reply=asyncio.run(api.eth_router02_user_wallet_transaction(
        request,authorization="Bearer verified"))
    assert reply.status_code==200
    assert reply.headers["cache-control"]=="private, no-store"
    assert calls==[(DID,"BUY","USDC","10",True)]
    assert b'"execution_enabled":false' in reply.body

def test_missing_auth_blocks_transaction(monkeypatch):
    def no_auth(_):raise HTTPException(401,"Missing Privy session")
    monkeypatch.setattr(api,"verify_privy_bearer",no_auth)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.eth_router02_user_wallet_transaction(
            api.EthLiveQuoteRequest(side="SELL",stable="USDT",amount="0.5"),
            authorization=None))
    assert exc.value.status_code==401

def test_wrong_chain_never_exports_executable_transaction(monkeypatch):
    monkeypatch.setattr(api,"verify_privy_bearer",lambda token: {"user_id":DID})
    async def quote(*args,**kwargs):
        return {"wallet_identity_verified":True,"unsigned_swap_validated":True,
                "transaction":{"chainId":56}}
    monkeypatch.setattr(api,"make_policy_compatible_eth_quote",quote)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.eth_router02_user_wallet_transaction(
            api.EthLiveQuoteRequest(side="BUY",stable="USDC",amount="5"),
            authorization="Bearer valid"))
    assert exc.value.status_code==502
