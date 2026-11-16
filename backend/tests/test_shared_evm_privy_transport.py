"""All six EVM assets use the very same real Privy transport used by ETH."""
import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from backend.shared_evm_swap_receipts import _pair
from backend.shared_evm_execution import EvmPair,unsigned_exact_input_single
from backend.shared_evm_privy_transport import (
    bound_privy_evm_swap_request,post_claimed_privy_evm_rpc,
)
from backend.eth_privy_auto_transport import bound_privy_rpc_request

WALLET="0x"+"ab"*20
WALLET_ID="wallet123456"
KEY="sig_abcdef01234567890123456789"
HASH="0x"+"ab"*32


@pytest.mark.parametrize("asset,stable,chain",[
    ("ETH","USDC",1),("LINK","USDC",1),("UNI","USDT",1),
    ("BNB","USDT",56),("POL","USDC",137),("AVAX","USDC",43114),
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_one_privy_network_rpc_for_every_evm_asset(asset,stable,chain,side):
    pair=_pair(asset,stable,side)
    tx=unsigned_exact_input_single(pair=EvmPair(
        asset,chain,pair["network"],pair["source_token"],
        pair["output_token"],pair["router"],500),
        wallet=WALLET,amount_in=1000000,min_out=900000)
    url,body=bound_privy_evm_swap_request(
        asset=asset,wallet_id=WALLET_ID,wallet=WALLET,
        tx=tx,side=side,stable=stable,input_atomic="1000000",
        minimum_output_atomic="900000")
    assert url=="https://api.privy.io/v1/wallets/"+WALLET_ID+"/rpc"
    assert body["caip2"]==f"eip155:{chain}"
    assert body["method"]=="eth_sendTransaction"
    assert body["params"]["transaction"]["to"]==pair["router"]
    assert body["params"]["transaction"]["data"]==tx["data"]
    assert body["params"]["transaction"]["value"]=="0"
    if asset=="ETH":
        assert (url,body)==bound_privy_rpc_request(
            wallet_id=WALLET_ID,wallet=WALLET,tx=tx,
            side=side,stable=stable,input_atomic="1000000",
            minimum_output_atomic="900000")


@pytest.mark.parametrize("asset,stable,incorrect_chain",[
    ("BNB","USDT",1),("POL","USDC",56),("AVAX","USDC",1),
    ("LINK","USDC",137),("UNI","USDT",56),
])
def test_cross_network_privy_send_blocked_before_signature(asset,stable,incorrect_chain):
    pair=_pair(asset,stable,"BUY")
    tx=unsigned_exact_input_single(
        pair=EvmPair(asset,pair["chain_id"],pair["network"],
                     pair["source_token"],pair["output_token"],
                     pair["router"],500),
        wallet=WALLET,amount_in=1000000,min_out=900000)
    tx["chainId"]=incorrect_chain
    with pytest.raises(ValueError):
        bound_privy_evm_swap_request(
            asset=asset,wallet_id=WALLET_ID,wallet=WALLET,
            tx=tx,side="BUY",stable=stable,input_atomic="1000000",
            minimum_output_atomic="900000")


def test_actual_one_shot_shared_transport_preserves_privy_header_and_hash():
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.headers["privy-idempotency-key"]==KEY
        assert request.headers["privy-authorization-signature"]=="s"*50
        assert json.loads(request.content)["caip2"]=="eip155:56"
        return httpx.Response(200,json={"data":{"hash":HASH}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await post_claimed_privy_evm_rpc(
                url="https://api.privy.io/v1/wallets/"+WALLET_ID+"/rpc",
                body={"method":"eth_sendTransaction","caip2":"eip155:56",
                      "params":{"transaction":{"to":"0x"+"ef"*20,
                                                "value":"0","data":"0x04"}}},
                app_secret="private_server_secret",
                authorization_signature="s"*50,idempotency_key=KEY,
                client=client)
    assert asyncio.run(run())==HASH
    assert len(calls)==1


def test_uncertain_shared_privy_response_must_not_retry():
    calls=[]
    def handle(req):
        calls.append(req)
        raise httpx.ReadTimeout("uncertain",request=req)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await post_claimed_privy_evm_rpc(
                url="https://api.privy.io/v1/wallets/"+WALLET_ID+"/rpc",
                body={"method":"eth_sendTransaction","caip2":"eip155:137",
                      "params":{"transaction":{"to":"0x"+"ef"*20,
                                                "value":"0","data":"0x04"}}},
                app_secret="private_server_secret",
                authorization_signature="s"*50,idempotency_key=KEY,
                client=client)
    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code==503
    assert len(calls)==1


@pytest.mark.parametrize("bad_url,bad_chain",[
    ("https://other.example/v1/wallets/wallet123456/rpc","eip155:1"),
    ("https://api.privy.io/v1/wallets/wallet123456/rpc","eip155:999"),
])
def test_transport_rejects_unexpected_host_or_chain_without_post(bad_url,bad_chain):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req:pytest.fail("Must not contact Privy"))) as client:
            return await post_claimed_privy_evm_rpc(
                url=bad_url,body={"method":"eth_sendTransaction",
                    "caip2":bad_chain,"params":{"transaction":{"to":"0x"+"ab"*20}}},
                app_secret="private_server_secret",
                authorization_signature="s"*50,idempotency_key=KEY,
                client=client)
    with pytest.raises(ValueError):
        asyncio.run(run())
