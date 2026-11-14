"""Policy-compatible native ETH two-stage quote preparation: no signing."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_router02_live_quote as router
from backend.eth_swaprouter02_preflight import (
    SWAPROUTER02_MAINNET, WETH_MAINNET, inspect_swaprouter02_exact_input,
)
from backend.live_eth_quotes import STABLES

DID="did:privy:router02-wallet"
WALLET="0x"+"a"*40


def balances(*,eth="1.5",usdc="1500",usdt="1000",address=WALLET):
    return {
        "mode":"VERIFIED_READ_ONLY","wallet_ownership_verified":True,
        "wallets":{"ETH":address},
        "balances":{
            "ETH":{"status":"verified","balance":eth},
            "USDC":{"status":"verified","balance":usdc},
            "USDT":{"status":"verified","balance":usdt},
        },
    }


def encoded_result(amount: int, gas=180000):
    values=(amount,2**96,1,gas)
    return "0x"+"".join(f"{a:064x}" for a in values)


@pytest.fixture
def provider(monkeypatch):
    calls=[]
    state={"balances":balances(),"chain":"0x1","quotes":{
        500:encoded_result(2*10**17,180000),
        3000:encoded_result(19*10**16,130000),
    }}
    async def owner(did):
        assert did==DID
        return {"id":DID,"linked_accounts":[{
            "type":"wallet","chain_type":"ethereum",
            "wallet_client_type":"privy","address":WALLET,
        }]}
    async def get_balances(did):
        assert did==DID
        return state["balances"]
    async def fake_rpc(client,*,method,params,req_id):
        calls.append((method,req_id,params))
        if method=="eth_chainId":return state["chain"]
        if method=="eth_call":
            data=params[0]["data"]
            assert params[0]["to"]==router.QUOTER_V2_MAINNET
            assert data.startswith(router._QUOTER_SELECTOR)
            assert params[1]=="latest"
            value=state["quotes"][req_id]
            if isinstance(value,Exception):
                raise value
            return value
        raise AssertionError("unexpected RPC method")
    monkeypatch.setattr(router,"fetch_privy_user",owner)
    monkeypatch.setattr(router,"get_wallet_balances",get_balances)
    monkeypatch.setattr(router,"_rpc",fake_rpc)
    return state,calls


def test_router02_quote_selector_and_exact_calldata_format():
    tx=router.build_unsigned_router02_swap(
        wallet=WALLET,side="BUY",stable="USDC",
        amount_in=123456789,min_out=150000000000000000,fee=500)
    assert tx["chainId"]==1
    assert tx["value"]=="0"
    assert tx["to"]==SWAPROUTER02_MAINNET
    assert len(tx["data"])==2+(4+7*32)*2
    result=inspect_swaprouter02_exact_input(
        tx,wallet=WALLET,side="BUY",stable="USDC",
        input_atomic="123456789",
        minimum_output_atomic="150000000000000000")
    assert result["token_pair_verified"] is True
    assert result["recipient_verified"] is True
    with pytest.raises(ValueError):
        inspect_swaprouter02_exact_input(
            tx,wallet="0x"+"b"*40,side="BUY",stable="USDC",
            input_atomic="123456789",
            minimum_output_atomic="150000000000000000")


@pytest.mark.parametrize("side,stable",[
    ("BUY","USDC"),("SELL","USDC"),("BUY","USDT"),("SELL","USDT"),
])
def test_compatible_erc20_swap_call_does_not_sign(side,stable):
    amount_in=10**8 if side=="BUY" else 10**17
    min_out=10**17 if side=="BUY" else 10**8
    tx=router.build_unsigned_router02_swap(
        wallet=WALLET,side=side,stable=stable,
        amount_in=amount_in,min_out=min_out,fee=3000)
    assert tx["data"].startswith("0x04e45aaf")
    assert tx["to"]==SWAPROUTER02_MAINNET
    assert not any(field in tx for field in ("signature","nonce","private_key"))
    report=inspect_swaprouter02_exact_input(
        tx,wallet=WALLET,side=side,stable=stable,
        input_atomic=str(amount_in),
        minimum_output_atomic=str(min_out))
    assert report["router02_policy_live_authorized"] is False


@pytest.mark.parametrize("side,stable,amount",[
    ("BUY","USDC","100"),("BUY","USDT","100"),
    ("SELL","USDC","0.3"),("SELL","USDT","0.3"),
])
def test_real_abi_quote_both_directions_and_non_atomic_stages(
    provider,side,stable,amount,
):
    state,calls=provider
    result=asyncio.run(router.make_policy_compatible_eth_quote(
        DID,side=side,stable=stable,amount=amount))
    assert result["mode"]=="ETH_ROUTER02_POLICY_ABI_QUOTE_ONLY"
    assert result["asset"]=="ETH" and result["network"]=="ethereum-mainnet"
    assert result["quote_fee_tier"]==500
    assert result["route"]=="UNISWAP_V3_SINGLE_POOL"
    assert result["swap_call_abi"]=="exactInputSingle"
    assert result["router"]==SWAPROUTER02_MAINNET
    assert result["onchain_swap_quote"] is True
    assert result["unsigned_swap_validated"] is True
    assert result["minimum_swap_output_amount"]=="0.199" if side=="BUY" else result["minimum_swap_output_amount"]=="199000000000"
    assert result["native_eth_cycle_non_atomic"] is True
    names=[x["name"] for x in result["native_eth_cycle_stages"]]
    assert names==(
        ["SWAP_STABLE_TO_WETH","UNWRAP_RECEIVED_WETH"] if side=="BUY"
        else ["WRAP_NATIVE_ETH","SWAP_CONFIRMED_WETH_TO_STABLE"]
    )
    for name in ("can_submit","execution_enabled","wallet_signed",
                 "funds_transferred","privy_signer_authorized",
                 "privy_policy_attached"):
        assert result[name] is False
    assert "data" not in result and "private_key" not in result
    assert len(calls)==3
    assert {c[1] for c in calls}=={1,500,3000}


def test_router02_quote_does_not_assume_native_eth_equals_weth(provider):
    state,calls=provider
    result=asyncio.run(router.make_policy_compatible_eth_quote(
        DID,side="BUY",stable="USDC",amount="100"))
    assert result["swap_output_token"]=="WETH"
    assert result["native_wrap_unwrap_not_yet_settled"] is True
    assert result["native_eth_cycle_stages"][1]["amount_source"]=="INDEPENDENTLY_VERIFIED_WETH_TOKEN_DELTA"


@pytest.mark.parametrize("bad",[
    "", "0", "-2", "1e3", "0.0000001", "NaN", "1500.0000001",
])
def test_invalid_stable_amount_rejected_without_rpc(provider,bad):
    _,calls=provider
    with pytest.raises(HTTPException) as exc:
        asyncio.run(router.make_policy_compatible_eth_quote(
            DID,side="BUY",stable="USDC",amount=bad))
    assert exc.value.status_code==422 and not calls


@pytest.mark.parametrize("modify",[
    lambda s:s.update(chain="0x89"),
    lambda s:s["balances"].update(mode="UNVERIFIED"),
    lambda s:s["balances"]["wallets"].update(ETH="0x"+"b"*40),
    lambda s:s["balances"]["balances"]["ETH"].update(status="unavailable"),
    lambda s:s["balances"]["balances"]["USDC"].update(balance="5"),
    lambda s:s["quotes"].update({500:ValueError("unavailable"),
                                  3000:ValueError("unavailable")}),
])
def test_rpc_or_balance_discrepancy_fail_closed(provider,modify):
    state,_=provider
    modify(state)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(router.make_policy_compatible_eth_quote(
            DID,side="BUY",stable="USDC",amount="100"))
    assert exc.value.status_code in (409,502,503)


def test_nonviable_fee_can_fall_back_to_the_valid_pool(provider):
    state,_=provider
    state["quotes"][500]=ValueError("pool uninitialized")
    result=asyncio.run(router.make_policy_compatible_eth_quote(
        DID,side="BUY",stable="USDC",amount="100"))
    assert result["quote_fee_tier"]==3000


@pytest.mark.parametrize("bad",[
    "0x","0x"+"00"*127,"0x"+"00"*128,"junk",
    encoded_result(0),encoded_result(10**45),
    encoded_result(10**17,gas=0),
])
def test_malformed_onchain_abi_rejected(bad):
    with pytest.raises(ValueError):
        router._decode_quote(bad)


def test_rest_endpoint_stays_authenticated_and_cannot_accept_wallet_address(
    provider,monkeypatch,
):
    key=ec.generate_private_key(ec.SECP256R1())
    pub=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"v3-route","iat":now,"exp":now+600,
    },key,algorithm="ES256")
    client=TestClient(api.app)
    endpoint="/api/live/eth/router02-quote"
    body={"side":"BUY","stable":"USDC","amount":"100"}
    assert client.post(endpoint,json=body).status_code==401
    assert client.post(endpoint,headers={"Authorization":"Bearer "+token},
                       json={**body,"recipient":"0x"+"b"*40}).status_code==422
    ok=client.post(endpoint,headers={"Authorization":"Bearer "+token},json=body)
    assert ok.status_code==200
    assert ok.headers["cache-control"]=="private, no-store"
    assert ok.json()["can_submit"] is False
