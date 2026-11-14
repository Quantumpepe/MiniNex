"""Native BTC↔stable indicative quote tests: no signing, deposit memo or bridge send."""
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
from backend import live_btc_quotes as q

DID="did:privy:btc-live-quote"
BTC="bc1q"+"a"*38
ETH="0x"+"b"*40


def identity():
    return {"id":DID,"linked_accounts":[
        {"type":"wallet","chain_type":"bitcoin-segwit","address":BTC},
        {"type":"wallet","chain_type":"ethereum","wallet_client_type":"privy","address":ETH},
    ]}


def wallet_balances(*,btc="1.0",eth="0.5",usdc="1000",usdt="500"):
    return {"wallets":{"BTC":BTC,"ETH":ETH.lower()},
            "balances":{
              "BTC":{"status":"verified","balance":btc},
              "ETH":{"status":"verified","balance":eth},
              "USDC":{"status":"verified","balance":usdc},
              "USDT":{"status":"verified","balance":usdt},
            }}


def provider_quote(out="200000000",slippage=75):
    return {"jsonrpc":"2.0","id":1,"result":{
        "structuredContent":{
            "expected_amount_out":out,
            "slippage_bps":slippage,
            "recommended_min_amount_in":"100000",
        },
    }}


@pytest.fixture
def wallets(monkeypatch):
    async def user(did):
        assert did==DID
        return identity()
    async def balances(did):
        assert did==DID
        return wallet_balances()
    monkeypatch.setattr(q,"fetch_privy_user",user)
    monkeypatch.setattr(q,"get_wallet_balances",balances)


@pytest.fixture
def headers(monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    pub=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"btc-quote","iat":now,"exp":now+600,
    },key,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_exact_btc_satoshis_and_stablecoin_precision():
    assert q._amount_to_mcp_units("0.00000001",8)[1]=="1"
    assert q._amount_to_mcp_units("1",8)[1]=="100000000"
    assert q._amount_to_mcp_units("12.250001",6)[1]=="1225000100"
    for raw,decimals in [
        ("0",8), ("0.000000001",8), ("1e-2",8), ("01",8),
        ("-1",8), ("0.0000001",6), ("NaN",6), (1,8),
    ]:
        with pytest.raises(HTTPException) as err:
            q._amount_to_mcp_units(raw,decimals)
        assert err.value.status_code==422


def test_provider_quote_strictly_rejects_memos_inbound_addresses_and_bad_terms():
    from_asset="BTC.BTC"
    to_asset="ETH.USDC-0XABC"
    good=q._provider_quote(provider_quote(),from_asset=from_asset,
                           to_asset=to_asset,input_units="100000000")
    assert good["estimated_output_units_1e8"]=="200000000"
    assert good["minimum_received_guaranteed"] is False
    assert good["provider_expiry"] is None
    current=int(time.time())
    valid_expiring=provider_quote()
    valid_expiring["result"]["structuredContent"]["expiry"]=current+20
    result=q._provider_quote(valid_expiring,from_asset=from_asset,
                             to_asset=to_asset,input_units="100000000")
    assert result["provider_expiry"]==current+20
    # Allow CI wall-clock to tick before validation; +3601 may cross the boundary.
    for invalid_expiry in (current-1,current+7200,"invalid",True,0):
        invalid=provider_quote()
        invalid["result"]["structuredContent"]["expiry"]=invalid_expiry
        with pytest.raises(ValueError):
            q._provider_quote(invalid,from_asset=from_asset,
                              to_asset=to_asset,input_units="100000000")
    for changed in [
        {"expected_amount_out":"0"},
        {"expected_amount_out":"-1"},
        {"expected_amount_out":"NaN"},
        {"slippage_bps":600},
        {"slippage_bps":-1},
        {"recommended_min_amount_in":"200000000"},
        {"inbound_address":"bc1qnotadestination"},
        {"memo":"=:ETH.USDC:0x..."},
        {"to_asset":"ETH.ETH"},
        {"from_asset":"BTC.BTC2"},
        {"amount":"200000000"},
    ]:
        altered=provider_quote()
        altered["result"]["structuredContent"].update(changed)
        with pytest.raises(ValueError):
            q._provider_quote(altered,from_asset=from_asset,
                              to_asset=to_asset,input_units="100000000")
    with pytest.raises(ValueError):
        q._provider_quote({"jsonrpc":"2.0","id":1,"error":{"code":-1}},
            from_asset=from_asset,to_asset=to_asset,input_units="100000000")


def test_true_provider_call_has_no_destination_or_memo(monkeypatch,wallets):
    observed=[]
    original=httpx.AsyncClient

    def mock_client(*args,**kwargs):
        def reply(request):
            assert str(request.url)==q.BTC_QUOTE_URL
            assert kwargs.get("follow_redirects") is False
            payload=json.loads(request.content)
            assert payload["method"]=="tools/call"
            assert payload["params"]["name"]=="get_swap_quote"
            args=payload["params"]["arguments"]
            assert set(args)=={"from_asset","to_asset","amount"}
            assert args["from_asset"]=="BTC.BTC"
            assert args["to_asset"].startswith("ETH.USDC-")
            assert args["amount"]=="10000000"
            assert BTC not in str(payload)
            assert ETH not in str(payload)
            observed.append(payload)
            return httpx.Response(200,json=provider_quote())
        return original(*args,transport=httpx.MockTransport(reply),**kwargs)

    monkeypatch.setattr(q.httpx,"AsyncClient",mock_client)
    out=asyncio.run(q.quote_native_btc(
        DID,side="SELL",stable="USDC",amount="0.1"))
    assert len(observed)==1
    assert out["mode"]=="BTC_CROSSCHAIN_INDICATIVE_ONLY"
    assert out["destination_network"]=="ethereum-mainnet"
    assert out["estimated_output_amount"]=="2"
    assert out["can_submit"] is False
    assert out["transaction_built"] is False
    assert out["execution_enabled"] is False
    assert out["deposit_address_provided"] is False
    assert "inbound_address" not in out
    assert "memo" not in out
    assert "THORCHAIN" in out["route_provider"]


def test_reverse_quote_uses_native_btc_destination(monkeypatch,wallets):
    original=httpx.AsyncClient
    def mock_client(*args,**kwargs):
        def reply(request):
            payload=json.loads(request.content)
            args=payload["params"]["arguments"]
            assert args["from_asset"].startswith("ETH.USDT-")
            assert args["to_asset"]=="BTC.BTC"
            assert args["amount"]=="2500000000"
            return httpx.Response(200,json=provider_quote(out="170000"))
        return original(*args,transport=httpx.MockTransport(reply),**kwargs)
    monkeypatch.setattr(q.httpx,"AsyncClient",mock_client)
    out=asyncio.run(q.quote_native_btc(
        DID,side="BUY",stable="USDT",amount="25"))
    assert out["destination_network"]=="bitcoin-mainnet"
    assert out["estimated_output_amount"]=="0.0017"
    assert out["can_submit"] is False


def test_wallet_mismatch_or_unverified_funds_never_queries_provider(monkeypatch,wallets):
    async def missing(did):
        return {**wallet_balances(), "wallets":{"BTC":BTC,"ETH":"0x"+"c"*40}}
    monkeypatch.setattr(q,"get_wallet_balances",missing)
    with pytest.raises(HTTPException) as err:
        asyncio.run(q.quote_native_btc(DID,side="SELL",stable="USDC",amount="0.1"))
    assert err.value.status_code==503
    async def notfunded(did):
        return wallet_balances(btc="0.0001")
    monkeypatch.setattr(q,"get_wallet_balances",notfunded)
    with pytest.raises(HTTPException) as err:
        asyncio.run(q.quote_native_btc(DID,side="SELL",stable="USDC",amount="0.1"))
    assert err.value.status_code==409
    async def unavailable(did):
        values=wallet_balances()
        values["balances"]["BTC"]["status"]="unavailable"
        return values
    monkeypatch.setattr(q,"get_wallet_balances",unavailable)
    with pytest.raises(HTTPException) as err:
        asyncio.run(q.quote_native_btc(DID,side="SELL",stable="USDC",amount="0.1"))
    assert err.value.status_code==503


def test_private_btc_quote_endpoint_rejects_destination_injections(headers):
    client=TestClient(api.app)
    payload={"side":"SELL","stable":"USDC","amount":"0.1"}
    assert client.post("/api/live/btc/quote",json=payload).status_code==401
    assert client.post("/api/live/btc/quote",headers=headers,
        json={**payload,"destination":"0x"+"c"*40}).status_code==422
    assert client.post("/api/live/btc/quote",headers=headers,
        json={**payload,"memo":"123"}).status_code==422
    assert client.post("/api/live/btc/quote",headers=headers,
        json={**payload,"stable":"USDCx"}).status_code==422
    assert client.get("/api/live/btc/quote",headers=headers).status_code==405
    assert not any(route.path=="/api/live/btc/send" for route in api.app.routes)
