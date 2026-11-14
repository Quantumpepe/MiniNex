"""Native BTC UTXO observations before any Privy BTC delegated policy change."""
import asyncio
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import live_btc_receipts as receipts

DID="did:privy:btc-receipt-owner"
ADDRESS="bc1q"+"a"*38
TXID="a"*64
BLOCK="b"*64


def chain_tx(*,confirmed=True,input_amount=125000,output_amount=19000):
    return {
        "txid":TXID,
        "vin":[{
            "prevout":{
                "value":input_amount,
                "scriptpubkey_address":ADDRESS,
            },
        }],
        "vout":[
            {"value":output_amount,"scriptpubkey_address":ADDRESS},
            {"value":105000,"scriptpubkey_address":"bc1q"+"z"*38},
        ],
        "fee":1000,
        "status":{
            "confirmed":confirmed,
            "block_height":200 if confirmed else None,
            "block_hash":BLOCK if confirmed else None,
        },
    }


def inspect(**changes):
    row=chain_tx()
    row.update(changes.pop("row_changes",{}))
    return receipts.inspect_native_btc_transaction(
        TXID,owner_address=ADDRESS,row=row,tip=210,**changes)


def test_sent_btc_with_change_and_six_confirmations():
    result=inspect()
    assert result["network"]=="bitcoin-mainnet"
    assert result["owned_address_proved_by_privy"] is True
    assert result["wallet_input_sats"]=="125000"
    assert result["wallet_output_sats"]=="19000"
    assert result["net_wallet_movement_btc"]=="-0.00106"
    assert result["transaction_fee_btc"]=="0.00001"
    assert result["confirmations_observed"]==11
    assert result["recommended_confirmation_threshold_met"] is True
    assert result["crosschain_swap_fill_verified"] is False
    assert result["ethereum_stablecoin_settlement_verified"] is False
    assert result["refund_route_verified"] is False
    assert result["privy_policy_verified"] is False
    assert result["trade_order_recorded"] is False
    assert result["can_submit"] is False


def test_owner_inbound_utxo_does_not_prove_a_completed_btc_buy():
    tx=chain_tx()
    tx["vin"][0]["prevout"]["scriptpubkey_address"]="bc1q"+"z"*38
    tx["vout"][0]["value"]=750000
    tx["vout"][1]["value"]=500000
    result=receipts.inspect_native_btc_transaction(
        TXID,owner_address=ADDRESS,row=tx,tip=200)
    assert result["wallet_input_sats"]=="0"
    assert result["wallet_output_sats"]=="750000"
    assert result["net_wallet_movement_btc"]=="0.0075"
    assert result["confirmations_observed"]==1
    assert result["recommended_confirmation_threshold_met"] is False
    assert result["crosschain_swap_fill_verified"] is False


def test_unconfirmed_btc_transaction_is_not_final():
    tx=chain_tx(confirmed=False)
    result=receipts.inspect_native_btc_transaction(
        TXID,owner_address=ADDRESS,row=tx,tip=210)
    assert result["confirmations_observed"]==0
    assert result["explorer_reports_confirmed"] is False
    assert result["recommended_confirmation_threshold_met"] is False


@pytest.mark.parametrize("change",[
    {"txid":"f"*64},
    {"status":{"confirmed":"yes","block_height":200,"block_hash":BLOCK}},
    {"status":{"confirmed":True,"block_height":220,"block_hash":BLOCK}},
    {"status":{"confirmed":True,"block_height":200,"block_hash":"x"*64}},
    {"status":{"confirmed":False,"block_height":200,"block_hash":BLOCK}},
    {"vin":[{"prevout":{"value":-1,"scriptpubkey_address":ADDRESS}}]},
    {"vin":[{"prevout":{"value":"1","scriptpubkey_address":ADDRESS}}]},
    {"vin":[{"prevout":None}]},
    {"vout":[{"value":2**64,"scriptpubkey_address":ADDRESS}]},
    {"vout":"invalid"},
    {"fee":"1000"},
    {"fee":-1},
])
def test_bad_utxo_identity_or_status_rejected(change):
    with pytest.raises(ValueError):
        inspect(row_changes=change)


def test_unrelated_transaction_cannot_be_used_as_owned_receipt():
    tx=chain_tx()
    for entry in tx["vin"]:
        entry["prevout"]["scriptpubkey_address"]="bc1q"+"z"*38
    for entry in tx["vout"]:
        entry["scriptpubkey_address"]="bc1q"+"z"*38
    with pytest.raises(ValueError):
        receipts.inspect_native_btc_transaction(
            TXID,owner_address=ADDRESS,row=tx,tip=210)


def test_coinbase_receipt_allows_inbound_only():
    tx=chain_tx()
    tx["vin"]=[{"is_coinbase":True}]
    tx["vout"]=[{"value":50_000_000,"scriptpubkey_address":ADDRESS}]
    tx["fee"]=0
    assert receipts.inspect_native_btc_transaction(
        TXID,owner_address=ADDRESS,row=tx,tip=210)["wallet_input_sats"]=="0"


@pytest.mark.parametrize("bad",["0x"+"a"*64,"ab",None,True,33,"z"*64])
def test_invalid_hash_no_network_call(bad):
    with pytest.raises(HTTPException) as error:
        asyncio.run(receipts.check_native_btc_transaction(DID,bad))
    assert error.value.status_code==422


class FakeResponse:
    def __init__(self,result):
        self.result=result
        self.content=json.dumps(result).encode() if isinstance(result,dict) else str(result).encode()
        self.text=str(result) if not isinstance(result,dict) else self.content.decode()
    def raise_for_status(self):
        return None
    def json(self):
        return self.result


@pytest.fixture
def network(monkeypatch):
    requests=[]
    state={"tx":chain_tx(),"tip":"211"}
    async def wallet(did):
        assert did==DID
        return {"id":DID,"linked_accounts":[{
            "type":"wallet","chain_type":"bitcoin-segwit",
            "address":ADDRESS,
        }]}
    class Client:
        def __init__(self,**kwargs):
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self,*args):
            return False
        async def get(self,url,*,headers):
            requests.append(url)
            if url.endswith("/blocks/tip/height"):
                return FakeResponse(state["tip"])
            assert url.endswith("/tx/"+TXID)
            return FakeResponse(state["tx"])
    monkeypatch.setattr(receipts,"fetch_privy_user",wallet)
    monkeypatch.setattr(receipts.httpx,"AsyncClient",Client)
    return state,requests


def test_btc_rpc_reads_are_did_owned_and_observational(network):
    state,calls=network
    data=asyncio.run(receipts.check_native_btc_transaction(DID,TXID))
    assert data["confirmations_observed"]==12
    assert calls==[
        receipts.BTC_API+"/tx/"+TXID,
        receipts.BTC_API+"/blocks/tip/height",
    ]
    assert "private_key" not in data and "memo" not in data
    assert data["wallet_signed"] is False


def test_provider_inconsistent_tip_or_missing_tx_is_not_receipt(network):
    state,_=network
    state["tip"]="malformed"
    with pytest.raises(HTTPException) as e:
        asyncio.run(receipts.check_native_btc_transaction(DID,TXID))
    assert e.value.status_code==502


def test_unknown_wallet_rejected_before_transaction_lookup(network,monkeypatch):
    _,requests=network
    async def other(did):
        return {"id":did,"linked_accounts":[]}
    monkeypatch.setattr(receipts,"fetch_privy_user",other)
    with pytest.raises(HTTPException) as e:
        asyncio.run(receipts.check_native_btc_transaction(DID,TXID))
    assert e.value.status_code==503
    assert requests==[]


def test_authenticated_api_for_real_btc_tx_check(network,monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    pub=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"btc-transaction-receipt",
        "iat":now,"exp":now+600,
    },key,algorithm="ES256")
    client=TestClient(api.app)
    endpoint="/api/live/btc/tx-receipt/"+TXID
    assert client.get(endpoint).status_code==401
    assert client.post(endpoint,headers={"Authorization":"Bearer "+token}).status_code==405
    ok=client.get(endpoint,headers={"Authorization":"Bearer "+token})
    assert ok.status_code==200
    assert ok.headers["cache-control"]=="private, no-store"
    assert ok.json()["execution_enabled"] is False
    assert ok.json()["ethereum_stablecoin_settlement_verified"] is False
