"""Real Privy ETH broadcast transport, mocked network only."""
import asyncio
import httpx
import pytest
from fastapi import HTTPException

from backend.eth_privy_auto_transport import (
    bound_privy_rpc_request,broadcast_privy_eth_swap,
)
from backend.eth_router02_live_quote import build_unsigned_router02_swap
from backend.eth_swaprouter02_preflight import SWAPROUTER02_MAINNET
WALLET="0x"+"a"*40
ID="privatewallet1234"
HASH="0x"+"d"*64

def call():
    return build_unsigned_router02_swap(
        wallet=WALLET,side="BUY",stable="USDC",
        amount_in=10_000_000,min_out=1_000_000_000_000_000,fee=500)

def kwargs(client):
    return dict(wallet_id=ID,wallet=WALLET,tx=call(),
        side="BUY",stable="USDC",input_atomic="10000000",
        minimum_output_atomic="1000000000000000",
        app_secret="server_privy_secret_only",
        authorization_signature="s"*50,
        idempotency_key="sig_abcdef01234567890123456789",
        client=client)

def test_exact_one_router02_rpc_payload():
    url,body=bound_privy_rpc_request(wallet_id=ID,wallet=WALLET,tx=call(),
        side="BUY",stable="USDC",input_atomic="10000000",
        minimum_output_atomic="1000000000000000")
    assert url.endswith("/wallets/"+ID+"/rpc")
    assert body["caip2"]=="eip155:1"
    assert body["method"]=="eth_sendTransaction"
    assert body["params"]["transaction"]["to"]==SWAPROUTER02_MAINNET
    assert body["params"]["transaction"]["value"]=="0"

def test_actual_privy_rpc_transport_returns_hash_only():
    seen=[]
    def handler(request):
        seen.append(request)
        assert request.headers["privy-authorization-signature"]=="s"*50
        assert request.headers["privy-idempotency-key"]=="sig_abcdef01234567890123456789"
        import json
        body=json.loads(request.content)
        assert body["method"]=="eth_sendTransaction"
        return httpx.Response(200,json={"data":{"hash":HASH}})
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)) as client:
            return await broadcast_privy_eth_swap(**kwargs(client))
    assert asyncio.run(run())==HASH
    assert len(seen)==1

@pytest.mark.parametrize("bad",["wallet","to","chainId","data","value"])
def test_untrusted_swap_cannot_be_broadcast(bad):
    tx=call()
    if bad=="wallet":
        wallet="0x"+"b"*40
    else:
        wallet=WALLET
        tx[bad]={"to":"0x"+"b"*40,"chainId":56,
                 "data":"0xdeadbeef","value":"1"}[bad]
    with pytest.raises(ValueError):
        bound_privy_rpc_request(wallet_id=ID,wallet=wallet,tx=tx,
           side="BUY",stable="USDC",input_atomic="10000000",
           minimum_output_atomic="1000000000000000")

def test_privy_timeout_never_retries():
    attempts=[]
    def handler(request):
        attempts.append(1)
        raise httpx.ReadTimeout("unknown",request=request)
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)) as client:
            await broadcast_privy_eth_swap(**kwargs(client))
    with pytest.raises(HTTPException) as e:
        asyncio.run(run())
    assert e.value.status_code==503
    assert len(attempts)==1
