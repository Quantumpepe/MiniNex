"""Real THORChain public liquidity quotes for all twelve MiniNex assets.

No paper prices, deposit addresses, memos, signatures, balances or transfers.
Providers may not have active ADA/UNI/other pools; in that case return 503.
"""
import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import manual_swap_live_quotes as mq
from backend.manual_swap_routes import ALL_SWAP_ASSETS

DID = "did:privy:native-real-quote"


def provider(out="12345678", slip=25, **other):
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "structuredContent": {
            "expected_amount_out": out,
            "slippage_bps": slip,
            "recommended_min_amount_in": "1000",
            **other,
        },
    }}


def test_all_twelve_native_assets_and_ethereum_stables_exactly_mapped():
    assert set(mq.THOR_ASSETS) == set(ALL_SWAP_ASSETS)
    assert len(mq.THOR_ASSETS) == 14
    expected = {
        "BTC": "BTC.BTC", "ETH": "ETH.ETH", "BNB": "BSC.BNB",
        "SOL": "SOL.SOL", "XRP": "XRP.XRP", "TRX": "TRON.TRX",
        "POL": "POL.POL", "AVAX": "AVAX.AVAX", "DOGE": "DOGE.DOGE",
        "ADA": "ADA.ADA",
    }
    for sym, ident in expected.items():
        assert mq.THOR_ASSETS[sym][0] == ident
    for sym in ("UNI", "LINK", "USDC", "USDT"):
        assert mq.THOR_ASSETS[sym][0].startswith("ETH."+sym+"-0X")
    for sym in ALL_SWAP_ASSETS:
        for dest in ALL_SWAP_ASSETS:
            if sym != dest:
                src, dst, units = mq.validate_live_quote_pair(sym,dest,"1")
                assert src==mq.THOR_ASSETS[sym][0]
                assert dst==mq.THOR_ASSETS[dest][0]
                assert units=="100000000"


@pytest.mark.parametrize("source,dest,amount", [
    ("ETH","ETH","1"), ("BTC","WBTC","1"), ("USDCx","ADA","1"),
    ("BTC","USDC","0"), ("DOGE","USDC","1e2"),
    ("ETH","SOL","0.000000001"),
    ("USDT","SOL","0.0000001"),
    ("ADA","USDC","0.0000001"),
    ("LINK","UNI","-10"),
    ("LINK","UNI","01"),
    ("LINK","UNI","100000000000"),
])
def test_invalid_precision_and_token_pairs_fail_before_any_provider_call(source,dest,amount):
    with pytest.raises(HTTPException) as err:
        mq.validate_live_quote_pair(source,dest,amount)
    assert err.value.status_code == 422


def test_single_real_provider_call_no_wallet_funding_memo_or_action(monkeypatch):
    seen=[]
    original=httpx.AsyncClient
    def mock_client(*args,**kwargs):
        assert kwargs.get("follow_redirects") is False
        def callback(request):
            assert str(request.url) == mq.BTC_QUOTE_URL
            body=json.loads(request.content)
            assert body["method"]=="tools/call"
            assert body["params"]["name"]=="get_swap_quote"
            args=body["params"]["arguments"]
            assert set(args)=={"from_asset","to_asset","amount"}
            assert args=={"from_asset":"SOL.SOL","to_asset":"XRP.XRP",
                          "amount":"150000000"}
            assert "destination" not in args
            assert "memo" not in str(body).lower()
            seen.append(body)
            return httpx.Response(200,json=provider())
        return original(*args,transport=httpx.MockTransport(callback),**kwargs)
    monkeypatch.setattr(mq.httpx,"AsyncClient",mock_client)
    out=asyncio.run(mq.get_native_swap_quote("SOL","XRP","1.5"))
    assert len(seen)==1
    assert out["source_network"]=="solana-mainnet"
    assert out["destination_network"]=="xrpl-mainnet"
    assert out["estimated_output_amount"]=="0.12345678"
    assert out["input_amount"]=="1.5"
    assert out["quoted_price_impact_bps"]==25
    assert out["pool_route_quoted"] is True
    assert out["execution_enabled"] is False
    assert out["can_submit"] is False
    assert out["transaction_built"] is False
    assert out["deposit_address_provided"] is False
    assert "memo" not in out and "inbound_address" not in out


@pytest.mark.parametrize("evil", [
    {"memo":"=:BTC.BTC:address"},
    {"inbound_address":"bc1qsuspect"},
    {"slippage_bps":1000},
    {"expected_amount_out":"0"},
    {"expected_amount_out":"NaN"},
    {"to_asset":"BTC.BTC"},
    {"from_asset":"BTC.BTC"},
    {"recommended_min_amount_in":"99999999999"},
])
def test_unavailable_or_malicious_provider_never_returns_executable_instructions(monkeypatch,evil):
    original=httpx.AsyncClient
    def client(*args,**kwargs):
        return original(*args,transport=httpx.MockTransport(
            lambda request:httpx.Response(200,json=provider(**evil))),**kwargs)
    monkeypatch.setattr(mq.httpx,"AsyncClient",client)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(mq.get_native_swap_quote("DOGE","ETH","2"))
    assert ex.value.status_code == 503


@pytest.fixture
def headers(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    public=private.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io", "aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"native-live-swap-quote",
        "iat":now,"exp":now+600,
    },private,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_quote_http_requires_real_privy_auth_for_real_provider_market_data(monkeypatch,headers):
    client=TestClient(api.app)
    path="/api/wallet/swap/live-quote"
    body={"source":"ADA","target":"ETH","amount":"5"}
    assert client.post(path,json=body).status_code==401
    assert client.get(path,headers=headers).status_code==405
    for bad in [
        {**body,"recipient":"0x"+"a"*40},
        {**body,"inbound_address":"bc1q"},
        {**body,"memo":"=:ETH"},
        {**body,"sender":"did:privy:user"},
    ]:
        assert client.post(path,headers=headers,json=bad).status_code==422
    async def mocked(src,dst,qty):
        assert (src,dst,qty)==("ADA","ETH","5")
        return {
            "mode":"NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY",
            "can_submit":False,"execution_enabled":False,
        }
    monkeypatch.setattr(api,"get_native_swap_quote",mocked)
    response=client.post(path,headers=headers,json=body)
    assert response.status_code==200
    assert response.headers["cache-control"]=="private, no-store"
    assert response.headers["x-content-type-options"]=="nosniff"
    assert response.json()["can_submit"] is False
    assert not any(x.path=="/api/wallet/swap/execute" for x in api.app.routes)
