"""LINK and UNI use a common owner-bound V3 Router02 path, never paper fills."""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from backend import eth_erc20_trade_quote as erc20
from backend.eth_swaprouter02_preflight import SWAPROUTER02_MAINNET
from backend.live_eth_quotes import STABLES
from backend.multicoin_evm_balances import _ETH_TOKENS

DID="did:privy:erc20"
WALLET="0x"+"ab"*20


@pytest.mark.parametrize("asset",["LINK","UNI"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_link_uni_use_shared_actual_ethereum_router(asset,side):
    tx=erc20.build_erc20_router02_swap(
        asset=asset,wallet=WALLET,side=side,stable="USDC",
        input_atomic=1000000,minimum_output_atomic=500,fee=3000,
    )
    assert tx["chainId"]==1 and tx["to"]==SWAPROUTER02_MAINNET
    assert tx["from"]==WALLET and tx["value"]=="0"
    words=[int(tx["data"][10+i*64:10+(i+1)*64],16) for i in range(7)]
    expected_in=STABLES["USDC"] if side=="BUY" else _ETH_TOKENS[asset]
    expected_out=_ETH_TOKENS[asset] if side=="BUY" else STABLES["USDC"]
    assert words[0]==int(expected_in,16)
    assert words[1]==int(expected_out,16)
    assert words[2]==3000
    assert words[3]==int(WALLET,16)
    assert words[4]==1000000 and words[5]==500 and words[6]==0


@pytest.mark.parametrize("asset,stable",[
    ("ADA","USDC"),("POL","USDC"),("LINK","UNKNOWN"),("UNI","DAI"),
])
def test_unsupported_token_pair_cannot_emit_router_calldata(asset,stable):
    with pytest.raises(ValueError):
        erc20.build_erc20_router02_swap(
            asset=asset,wallet=WALLET,side="BUY",stable=stable,
            input_atomic=100,minimum_output_atomic=50,fee=3000,
        )


@pytest.fixture
def mocked_owner_and_chain(monkeypatch):
    seen=[]
    async def owner(did):
        assert did==DID
        return {"id":DID}
    async def balance(did):
        assert did==DID
        return {
            "mode":"VERIFIED_READ_ONLY","wallet_ownership_verified":True,
            "wallets":{"ETH":WALLET},
            "balances":{
                "ETH":{"status":"verified","balance":"1"},
                "USDC":{"status":"verified","balance":"500"},
                "USDT":{"status":"verified","balance":"500"},
            },
        }
    async def rpc(client,*,method,params,req_id):
        seen.append((method,req_id))
        if method=="eth_chainId":
            return "0x1"
        if method=="eth_call":
            assert params[0]["data"].startswith("0x70a08231")
            return "0x"+format(2*10**18,"064x")
        raise AssertionError(method)
    async def v3(client,token_in,token_out,amount_in,fee):
        seen.append(("v3",fee))
        return (fee,2*10**18,100000) if fee==500 else (fee,10**18,90000)
    class Client:
        def __init__(self,**kwargs): assert kwargs["follow_redirects"] is False
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
    monkeypatch.setattr(erc20,"fetch_privy_user",owner)
    monkeypatch.setattr(erc20,"get_wallet_balances",balance)
    monkeypatch.setattr(erc20,"verified_addresses",lambda user,did:{"ETH":WALLET})
    monkeypatch.setattr(erc20,"_rpc",rpc)
    monkeypatch.setattr(erc20,"_single_v3_quote",v3)
    monkeypatch.setattr(erc20.httpx,"AsyncClient",Client)
    return seen


@pytest.mark.parametrize("asset",["LINK","UNI"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_owner_verified_link_uni_quote_can_build_unsigned_user_transaction(
    asset,side,mocked_owner_and_chain
):
    result=asyncio.run(erc20.quote_owner_erc20_trade(
        DID,asset=asset,side=side,stable="USDC",
        amount="5" if side=="BUY" else "0.25",
        include_user_transaction=True,
    ))
    assert result["asset"]==asset
    assert result["side"]==side
    assert result["mode"]=="SHARED_EVM_ERC20_ROUTER02_QUOTE"
    assert result["onchain_quote_verified"] is True
    assert result["wallet_identity_verified"] is True
    assert result["transaction"]["chainId"]==1
    assert result["transaction"]["to"]==SWAPROUTER02_MAINNET
    assert result["transaction"]["from"]==WALLET
    assert result["transaction"]["data"].startswith("0x04e45aaf")
    assert result["requires_separate_erc20_approval"] is True
    assert result["can_submit"] is False
    assert result["execution_enabled"] is False
    assert result["funds_transferred"] is False
    assert ("eth_chainId",1) in mocked_owner_and_chain
    if side=="SELL":
        assert ("eth_call",4) in mocked_owner_and_chain


def test_erc20_quote_never_accepts_provider_chain_mismatch(
    monkeypatch,mocked_owner_and_chain
):
    async def wrong(client,*,method,params,req_id):
        return "0x38" if method=="eth_chainId" else "0x"+"0"*64
    monkeypatch.setattr(erc20,"_rpc",wrong)
    with pytest.raises(HTTPException) as error:
        asyncio.run(erc20.quote_owner_erc20_trade(
            DID,asset="LINK",side="BUY",stable="USDC",amount="5"))
    assert error.value.status_code==502


def test_owner_erc20_endpoint_does_not_sign_or_broadcast():
    from backend.app import app
    route=next(r for r in app.routes
               if r.path=="/api/live/evm/erc20-user-transaction/{asset}")
    text=inspect.getsource(route.endpoint)
    assert "verify_privy_bearer" in text
    assert "quote_owner_erc20_trade" in text
    assert "broadcast_privy_eth_swap(" not in text
    assert "send_privy_native_weth9(" not in text
