"""One real-quote plan dispatcher for all eleven trade coins; no real sends."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import live_trade_router as router
from backend.live_asset_registry import get_asset

DID="did:privy:live-dispatch-owner"

@pytest.fixture
def venues(monkeypatch):
    seen=[]
    async def eth(did,*,side,stable,amount):
        seen.append(("ETH",did,side,stable,amount))
        return {
            "mode":"LIVE_ETH_SWAP_PLAN_ONLY","asset":"ETH",
            "side":side,"input_amount":amount,
            "expected_output_amount":"0.2",
            "minimum_output_amount":"0.19","state":"UNSIGNED_TX_REVIEW_REQUIRED",
            "transaction_built":True,
            "execution_enabled":False,"can_submit":False,
        }
    async def btc(did,*,side,stable,amount):
        seen.append(("BTC",did,side,stable,amount))
        return {
            "mode":"BTC_CROSSCHAIN_INDICATIVE_ONLY","asset":"BTC",
            "side":side,"input_amount":amount,
            "source_asset":stable if side=="BUY" else "BTC",
            "destination_asset":"BTC" if side=="BUY" else stable,
            "estimated_output_amount":"0.2",
            "execution_enabled":False,"can_submit":False,
        }
    async def other(source,target,amount):
        seen.append(("OTHER",source,target,amount))
        return {
            "mode":"NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY",
            "source_asset":source,"destination_asset":target,
            "input_amount":amount,"estimated_output_amount":"0.2",
            "execution_enabled":False,"can_submit":False,
        }
    monkeypatch.setattr(router,"make_eth_swap_plan",eth)
    monkeypatch.setattr(router,"quote_native_btc",btc)
    monkeypatch.setattr(router,"get_native_swap_quote",other)
    return seen


@pytest.mark.parametrize("asset",router.TRADE_COINS)
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_each_coin_uses_same_trade_plan_and_correct_route(venues,asset,side):
    stable=get_asset(asset).stablecoin_candidates[0]
    amount="25" if side=="BUY" else "0.1"
    plan=asyncio.run(router.get_unified_trade_plan(
        DID,asset=asset,side=side,stable=stable,amount=amount))
    assert plan["mode"]=="UNIFIED_ELEVEN_COIN_TRADE_PLAN"
    assert plan["asset"]==asset
    assert plan["side"]==side
    assert plan["source_asset"]==(stable if side=="BUY" else asset)
    assert plan["destination_asset"]==(asset if side=="BUY" else stable)
    assert plan["network"]==get_asset(asset).blockchain
    assert plan["input_amount"]==amount
    assert plan["estimated_output_amount"]=="0.2"
    assert plan["wallet_signed"] is False
    assert plan["real_order_created"] is False
    assert plan["funds_transferred"] is False
    assert plan["execution_enabled"] is False
    assert plan["can_submit"] is False
    if asset=="ETH":
        assert venues==[("ETH",DID,side,stable,amount)]
        assert plan["transaction_preview_created"] is True
        assert plan["wallet_funds_verified"] is True
        assert plan["quote_level"]=="WALLET_VERIFIED_UNSIGNED_SWAP"
    elif asset=="BTC":
        assert venues==[("BTC",DID,side,stable,amount)]
        assert plan["wallet_funds_verified"] is True
        assert plan["quote_level"]=="WALLET_VERIFIED_CROSSCHAIN_INDICATIVE"
    else:
        assert venues==[("OTHER",plan["source_asset"],plan["destination_asset"],amount)]
        assert plan["wallet_funds_verified"] is False
        assert plan["quote_level"]=="VENUE_INDICATIVE_WALLET_NOT_VERIFIED"


@pytest.mark.parametrize("asset,stable",[
    ("ADA","USDC"),("TRX","USDC"),("XRP","USDT"),
    ("POL","USDT"),("BNB","USDC"),("MATIC","USDC"),
])
def test_unsupported_coin_or_wrong_settlement_never_contacts_venue(venues,asset,stable):
    with pytest.raises(HTTPException) as e:
        asyncio.run(router.get_unified_trade_plan(
            DID,asset=asset,side="BUY",stable=stable,amount="100"))
    assert e.value.status_code in (404,422)
    assert venues==[]


@pytest.mark.parametrize("bad",["0","-1","1e3","NaN","Infinity","01","0.0.1","",1,1.0,None,True])
def test_amount_rejected_before_provider_request(venues,bad):
    with pytest.raises(HTTPException) as e:
        asyncio.run(router.get_unified_trade_plan(
            DID,asset="ETH",side="BUY",stable="USDC",amount=bad))
    assert e.value.status_code==422
    assert venues==[]


@pytest.mark.parametrize("field,value",[
    ("mode","UNSAFE"),("execution_enabled",True),("can_submit",True),
    ("source_asset","WRAPPED_BTC"),("destination_asset","ADA"),
])
def test_wrong_provider_quote_never_claims_live_order(venues,monkeypatch,field,value):
    async def wrong(src,dst,amount):
        result={
            "mode":"NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY",
            "source_asset":src,"destination_asset":dst,
            "input_amount":amount,"estimated_output_amount":"100",
            "execution_enabled":False,"can_submit":False,
        }
        result[field]=value
        return result
    monkeypatch.setattr(router,"get_native_swap_quote",wrong)
    with pytest.raises(HTTPException) as e:
        asyncio.run(router.get_unified_trade_plan(
            DID,asset="SOL",side="SELL",stable="USDC",amount="1"))
    assert e.value.status_code==502


@pytest.mark.parametrize("amount",["NaN","-1","0.00000","",None])
def test_untrusted_provider_output_never_becomes_order(venues,monkeypatch,amount):
    async def invalid(did,*,side,stable,amount):
        return {
            "mode":"BTC_CROSSCHAIN_INDICATIVE_ONLY","asset":"BTC",
            "side":side,"source_asset":"BTC","destination_asset":stable,
            "estimated_output_amount":amount,
            "execution_enabled":False,"can_submit":False,
        }
    monkeypatch.setattr(router,"quote_native_btc",invalid)
    with pytest.raises(HTTPException) as e:
        asyncio.run(router.get_unified_trade_plan(
            DID,asset="BTC",side="SELL",stable="USDC",amount="0.1"))
    assert e.value.status_code==502


@pytest.fixture
def headers(monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    pub=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"unified-quote","iat":now,"exp":now+600,
    },key,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_one_authenticated_api_for_all_coins_no_client_address_or_send(venues,headers):
    client=TestClient(api.app)
    url="/api/live/trade-plan/SOL"
    body={"side":"BUY","stable":"USDC","amount":"50"}
    assert client.post(url,json=body).status_code==401
    assert client.get(url,headers=headers).status_code==405
    assert client.post(url,headers=headers,json={
        **body,"wallet_id":"steal_user_wallet",
    }).status_code==422
    assert client.post("/api/live/trade-plan/ADA",headers=headers,json=body).status_code==404
    response=client.post(url,headers=headers,json=body)
    assert response.status_code==200
    assert response.headers["cache-control"]=="private, no-store"
    assert response.json()["execution_enabled"] is False
    assert venues==[("OTHER","USDC","SOL","50")]
