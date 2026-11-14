"""Actual Ethereum WETH/stable ERC20 allowances before the Privy policy step."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_router02_allowance as allowed
from backend.eth_swaprouter02_preflight import SWAPROUTER02_MAINNET,WETH_MAINNET
from backend.live_eth_quotes import STABLES

DID="did:privy:router02-allowance-owner"
WALLET="0x"+"a"*40


def encoded(n):
    return "0x"+f"{n:064x}"


@pytest.fixture
def rpc(monkeypatch):
    state={
        "chain":"0x1","allowance":100_000_000,
        "source_balance":120_000_000,
        "native_balance":10**18,
    }
    calls=[]
    async def owner(did):
        assert did==DID
        return {"id":did,"linked_accounts":[{
            "type":"wallet","chain_type":"ethereum",
            "wallet_client_type":"privy","address":WALLET,
        }]}
    async def query(client,*,method,params,req_id):
        calls.append((method,params,req_id))
        if method=="eth_chainId":return state["chain"]
        if method=="eth_getBalance":
            assert params==[WALLET.lower(),"latest"]
            return hex(state["native_balance"])
        assert method=="eth_call"
        assert params[1]=="latest"
        call=params[0]
        assert call["to"] in (STABLES["USDC"],STABLES["USDT"],WETH_MAINNET)
        if req_id==2:
            assert call["data"].startswith("0xdd62ed3e")
            assert call["data"][10:74]==WALLET[2:].rjust(64,"0")
            assert call["data"][74:]==SWAPROUTER02_MAINNET[2:].lower().rjust(64,"0")
            return encoded(state["allowance"])
        assert req_id==3
        assert call["data"].startswith("0x70a08231")
        return encoded(state["source_balance"])
    monkeypatch.setattr(allowed,"fetch_privy_user",owner)
    monkeypatch.setattr(allowed,"_rpc",query)
    return state,calls


@pytest.mark.parametrize("stable",["USDC","USDT"])
def test_eth_buy_reads_real_stable_allowance_and_verifies_gas(rpc,stable):
    state,calls=rpc
    result=asyncio.run(allowed.inspect_router02_approval(
        DID,side="BUY",stable=stable,amount="100"))
    assert result["source_token"]==stable
    assert result["input_amount"]=="100"
    assert result["verified_source_balance"]=="120"
    assert result["existing_router_allowance"]=="100"
    assert result["router_allowance_sufficient"] is True
    assert result["source_funds_sufficient"] is True
    assert result["manual_token_approval_would_be_required"] is False
    assert result["conservative_native_gas_reserve_met"] is True
    assert result["wallet_identity_verified"] is True
    assert result["signed_erc20_approval_created"] is False
    assert result["allowance_not_spend_authority"] is True
    assert result["privy_grant_verified"] is False
    assert result["execution_enabled"] is False
    assert result["can_submit"] is False
    assert {n for n,p,rid in calls}=={"eth_chainId","eth_call","eth_getBalance"}


def test_weth_sell_never_confuses_native_eth_with_weth_balance(rpc):
    state,calls=rpc
    state.update(allowance=0,source_balance=10**17)
    result=asyncio.run(allowed.inspect_router02_approval(
        DID,side="SELL",stable="USDC",amount="0.1"))
    assert result["source_token"]=="WETH"
    assert result["verified_source_balance"]=="0.1"
    assert result["native_eth_gas_balance"]=="1"
    assert result["router_allowance_sufficient"] is False
    assert result["manual_token_approval_would_be_required"] is True
    assert result["native_eth_unwrap_or_wrap_settled"] is False


def test_insufficient_actual_token_balance_is_not_approval(rpc):
    state,_=rpc
    state.update(allowance=10**10,source_balance=1)
    result=asyncio.run(allowed.inspect_router02_approval(
        DID,side="BUY",stable="USDT",amount="100"))
    assert result["router_allowance_sufficient"] is True
    assert result["source_funds_sufficient"] is False


def test_100_percent_live_spend_does_not_infer_gas_reserve(rpc):
    state,_=rpc
    state.update(native_balance=1000000000000000)
    result=asyncio.run(allowed.inspect_router02_approval(
        DID,side="BUY",stable="USDC",amount="100"))
    assert result["conservative_native_gas_reserve_met"] is False
    assert result["can_submit"] is False


@pytest.mark.parametrize("modify",[
    lambda state:state.update(chain="0x38"),
    lambda state:state.update(native_balance="invalid"),
    lambda state:state.update(allowance=-1),
    lambda state:state.update(source_balance="NaN"),
])
def test_wrong_chain_or_malformed_provider_response_fails_closed(rpc,modify):
    state,_=rpc
    modify(state)
    with pytest.raises(HTTPException) as e:
        asyncio.run(allowed.inspect_router02_approval(
            DID,side="BUY",stable="USDC",amount="100"))
    assert e.value.status_code==502


@pytest.mark.parametrize("bad",[
    "", "-1", "0", "NaN", "1e2", "100.0000001", "0001", 1,
])
def test_untrusted_amount_is_rejected_before_any_rpc(rpc,bad):
    _,calls=rpc
    with pytest.raises(HTTPException) as e:
        asyncio.run(allowed.inspect_router02_approval(
            DID,side="BUY",stable="USDC",amount=bad))
    assert e.value.status_code==422
    assert not calls


def test_invalid_wallet_does_not_reach_rpc(rpc,monkeypatch):
    _,calls=rpc
    async def missing(did):return {"id":did,"linked_accounts":[]}
    monkeypatch.setattr(allowed,"fetch_privy_user",missing)
    with pytest.raises(HTTPException) as e:
        asyncio.run(allowed.inspect_router02_approval(
            DID,side="BUY",stable="USDC",amount="10"))
    assert e.value.status_code==503
    assert not calls


def test_canonical_rpc_uint256_rejects_bad_data():
    for bad in ("0x123","0x","nothex",0,True,None,
                "0x"+"00"*33):
        with pytest.raises(ValueError):
            allowed._decode_uint(bad)
    assert allowed._decode_uint("0x"+"00"*32)==0


def test_authenticated_allowance_endpoint_does_not_grant_or_send(rpc,monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    pub=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"eth-allowance","iat":now,"exp":now+600,
    },key,algorithm="ES256")
    headers={"Authorization":"Bearer "+token}
    client=TestClient(api.app)
    endpoint="/api/live/eth/router02-allowance"
    body={"side":"BUY","stable":"USDC","amount":"100"}
    assert client.post(endpoint,json=body).status_code==401
    assert client.post(endpoint,json={**body,"recipient":"0x"+"b"*40},
                       headers=headers).status_code==422
    result=client.post(endpoint,json=body,headers=headers)
    assert result.status_code==200
    assert result.headers["cache-control"]=="private, no-store"
    assert result.json()["signed_erc20_approval_created"] is False
    assert result.json()["can_submit"] is False
