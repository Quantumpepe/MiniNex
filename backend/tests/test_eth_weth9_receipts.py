"""WETH9 final native ETH wrap/unwrap receipts before Privy policy activation."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_weth9_receipts as check
from backend.eth_native_weth9_review import (
    WETH9_DEPOSIT_SELECTOR,WETH9_WITHDRAW_SELECTOR,
)
from backend.eth_swaprouter02_preflight import WETH_MAINNET

DID="did:privy:weth9-receipt-user"
WALLET="0x"+"a"*40
HASH="0x"+"b"*64
BLOCK="0x"+"c"*64
AMOUNT=10**17


def tx(op="DEPOSIT"):
    return {
        "hash":HASH,"from":WALLET,"to":WETH_MAINNET,
        "chainId":"0x1","value":hex(AMOUNT if op=="DEPOSIT" else 0),
        "input":(WETH9_DEPOSIT_SELECTOR if op=="DEPOSIT" else
                 WETH9_WITHDRAW_SELECTOR+f"{AMOUNT:064x}"),
        "blockHash":BLOCK,"blockNumber":"0x64",
    }


def receipt(op="DEPOSIT",status="0x1"):
    event=check._DEPOSIT_TOPIC if op=="DEPOSIT" else check._WITHDRAW_TOPIC
    logs=([{"address":WETH_MAINNET,
            "topics":[event,"0x"+"0"*24+WALLET[2:].lower()],
            "data":"0x"+f"{AMOUNT:064x}"}] if status=="0x1" else [])
    return {
        "transactionHash":HASH,"from":WALLET,"to":WETH_MAINNET,
        "blockNumber":"0x64","blockHash":BLOCK,
        "status":status,"gasUsed":"0x5208",
        "effectiveGasPrice":"0x3b9aca00","logs":logs,
    }


def inspect(op="DEPOSIT",**patch):
    args={
        "wallet":WALLET,"operation":op,
        "chain_id":"0x1","transaction":tx(op),
        "receipt":receipt(op),"latest":"0x78",
        "finalized":{"number":"0x6e"},
    }
    args.update(patch)
    return check.inspect_weth9_receipt(HASH,**args)


@pytest.mark.parametrize("op,output",[
    ("DEPOSIT","WETH"),("WITHDRAW","ETH"),
])
def test_owner_finalized_weth9_conversion_matches_emitted_token_amount(op,output):
    r=inspect(op)
    assert r["state"]=="FINALIZED_WETH9_CONVERSION"
    assert r["confirmed_native_conversion"] is True
    assert r["converted_asset"]==output
    assert r["weth9_event_amount"]=="0.1"
    assert r["confirmations"]==21
    assert r["network_fee_eth"]=="0.000021"
    assert r["native_eth_trade_cycle_complete"] is False
    assert r["swap_leg_linked"] is False
    assert r["privy_policy_verified"] is False
    assert r["live_order_recorded"] is False
    assert r["can_submit"] is False


def test_pending_and_included_receipts_never_claim_native_conversion():
    result=inspect(finalized={"number":"0x63"})
    assert result["state"]=="INCLUDED_SUCCESS"
    assert result["confirmed_native_conversion"] is False
    assert result["weth9_event_amount"] is None
    pending=tx()
    pending.update(blockHash=None,blockNumber=None)
    result=inspect(transaction=pending,receipt=None,finalized=None)
    assert result["state"]=="PENDING"
    assert result["confirmed_native_conversion"] is False


def test_reverted_weth9_does_not_claim_native_eth_return():
    r=inspect(receipt=receipt(status="0x0"))
    assert r["state"]=="FINALIZED_REVERTED"
    assert r["transaction_finalized"] is True
    assert r["confirmed_native_conversion"] is False
    assert r["weth9_event_amount"] is None


@pytest.mark.parametrize("bad",[
    {"chain_id":"0x38"},
    {"wallet":"0x"+"d"*40},
    {"latest":"0x63"},
    {"transaction":{**tx(),"from":"0x"+"d"*40}},
    {"transaction":{**tx(),"to":"0x"+"d"*40}},
    {"transaction":{**tx(),"input":"0xdeadbeef"}},
    {"transaction":{**tx(),"value":"0x0"}},
    {"transaction":{**tx(),"blockHash":"0x"+"d"*64}},
    {"transaction":{**tx("WITHDRAW"),"input":WETH9_WITHDRAW_SELECTOR+"0"*64}},
    {"receipt":{**receipt(),"status":"0x2"}},
    {"receipt":{**receipt(),"to":"0x"+"d"*40}},
    {"receipt":{**receipt(),"blockNumber":"0x65"}},
    {"receipt":{**receipt(),"gasUsed":"0x0"}},
    {"finalized":{"number":"0x90"}},
])
def test_malformed_chain_or_call_never_confirms_wrap(bad):
    with pytest.raises(ValueError):
        inspect(**bad)


@pytest.mark.parametrize("op",["DEPOSIT","WITHDRAW"])
@pytest.mark.parametrize("mutation",[
    lambda l:l[0].update(address="0x"+"d"*40),
    lambda l:l[0].update(topics=["0x"+"f"*64]),
    lambda l:l[0]["topics"].__setitem__(1,"0x"+"d"*64),
    lambda l:l[0].update(data="0x"+f"{AMOUNT-1:064x}"),
    lambda l:l[0].update(data="0x"+"0"*64),
    lambda l:l.append(dict(l[0])),
])
def test_unsupported_or_duplicate_weth9_events_fail_closed(op,mutation):
    raw=receipt(op)
    mutation(raw["logs"])
    with pytest.raises(ValueError):
        inspect(op,receipt=raw)


@pytest.mark.parametrize("bad",["","0x123","f"*64,42,None,"0x"+"z"*64])
def test_invalid_receipt_hash_raises_before_rpc(bad):
    with pytest.raises(HTTPException) as err:
        asyncio.run(check.check_user_weth9_receipt(
            DID,bad,operation="DEPOSIT"))
    assert err.value.status_code==422


@pytest.fixture
def verified_rpc(monkeypatch):
    calls=[]
    async def owner(did):
        return {"id":did,"linked_accounts":[{
            "type":"wallet","chain_type":"ethereum",
            "wallet_client_type":"privy","address":WALLET,
        }]}
    async def rpc(client,method,params,req_id):
        calls.append(method)
        return {
            "eth_chainId":"0x1",
            "eth_getTransactionByHash":tx(),
            "eth_getTransactionReceipt":receipt(),
            "eth_blockNumber":"0x78",
            "eth_getBlockByNumber":{"number":"0x6e"},
        }[method]
    monkeypatch.setattr(check,"fetch_privy_user",owner)
    monkeypatch.setattr(check,"_rpc",rpc)
    return calls


def test_on_demand_native_eth_receipt_reads_no_authorized_money_movements(verified_rpc):
    result=asyncio.run(check.check_user_weth9_receipt(
        DID,HASH,operation="DEPOSIT"))
    assert result["confirmed_native_conversion"] is True
    assert result["execution_enabled"] is False
    assert verified_rpc==[
        "eth_chainId","eth_getTransactionByHash",
        "eth_getTransactionReceipt","eth_blockNumber","eth_getBlockByNumber",
    ]


def test_verified_privy_wallet_required(verified_rpc,monkeypatch):
    async def missing(did):
        return {"id":did,"linked_accounts":[]}
    monkeypatch.setattr(check,"fetch_privy_user",missing)
    with pytest.raises(HTTPException) as err:
        asyncio.run(check.check_user_weth9_receipt(
            DID,HASH,operation="DEPOSIT"))
    assert err.value.status_code==503
    assert verified_rpc==[]


def test_authenticated_route_for_native_wrap_receipt(verified_rpc,monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    pub=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"weth9-receipt",
        "iat":now,"exp":now+600,
    },key,algorithm="ES256")
    headers={"Authorization":"Bearer "+token}
    client=TestClient(api.app)
    endpoint="/api/live/eth/weth9-receipt/"+HASH+"?operation=DEPOSIT"
    assert client.get(endpoint).status_code==401
    assert client.post(endpoint,headers=headers).status_code==405
    r=client.get(endpoint,headers=headers)
    assert r.status_code==200
    assert r.headers["cache-control"]=="private, no-store"
    assert r.json()["confirmed_native_conversion"] is True
    assert r.json()["native_eth_trade_cycle_complete"] is False
