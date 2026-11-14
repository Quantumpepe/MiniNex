"""Privy real owner WETH9 wrap/unwrap transport, mocked before sending."""
import asyncio
import base64
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend.eth_privy_native_rpc import bound_weth9_rpc,send_privy_native_weth9
from backend.eth_swaprouter02_preflight import WETH_MAINNET

WALLET="0x"+"a"*40
KEY="step_abcdef01234567890123456789"
TX="0x"+"b"*64

@pytest.mark.parametrize("operation,value,data",[
 ("DEPOSIT","100000000000000000","0xd0e30db0"),
 ("WITHDRAW","0","0x2e1a7d4d"+f"{100000000000000000:064x}"),
])
def test_mainnet_native_conversion_payload(operation,value,data):
    url,body=bound_weth9_rpc(wallet_id="wallet123456",wallet=WALLET,
      operation=operation,amount_wei=10**17)
    assert url.endswith("/wallet123456/rpc")
    assert body["caip2"]=="eip155:1"
    assert body["params"]["transaction"]=={
      "to":WETH_MAINNET,"data":data,"value":value}

@pytest.mark.parametrize("operation,amount",[
 ("APPROVE",100),("DEPOSIT",0),("WITHDRAW",-1),
 ("WITHDRAW",10**45),
])
def test_only_exact_weth9_operation(operation,amount):
    with pytest.raises(ValueError):
        bound_weth9_rpc(wallet_id="wallet123456",wallet=WALLET,
            operation=operation,amount_wei=amount)

def test_privy_weth9_sign_and_send(monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    der=key.private_bytes(serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    monkeypatch.setenv("MININEX_PRIVY_ETH_AUTH_P256_PKCS8",
                       base64.b64encode(der).decode())
    requests=[]
    def handler(request):
        import json
        requests.append(json.loads(request.content))
        assert request.headers["privy-idempotency-key"]==KEY
        assert request.headers["privy-authorization-signature"]
        return httpx.Response(200,json={"data":{"hash":TX}})
    async def action():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as c:
            return await send_privy_native_weth9(
                wallet_id="wallet123456",wallet=WALLET,
                operation="DEPOSIT",amount_wei=10**17,
                intent_key=KEY,app_secret="secret_for_mock_only",
                client=c)
    assert asyncio.run(action())==TX
    assert len(requests)==1

def test_privy_weth9_timeout_is_not_retried(monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    der=key.private_bytes(serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    monkeypatch.setenv("MININEX_PRIVY_ETH_AUTH_P256_PKCS8",
                       base64.b64encode(der).decode())
    requests=[]
    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("unknown",request=request)
    async def action():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as c:
            await send_privy_native_weth9(
                wallet_id="wallet123456",wallet=WALLET,
                operation="WITHDRAW",amount_wei=10**17,
                intent_key=KEY,app_secret="secret_for_mock_only",
                client=c)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(action())
    assert len(requests)==1
