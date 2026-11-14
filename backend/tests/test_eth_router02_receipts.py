"""Real ETH swap leg amount checks from finalized, wallet-owned Transfer logs."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_router02_receipts as check
from backend.eth_router02_live_quote import build_unsigned_router02_swap
from backend.eth_swaprouter02_preflight import SWAPROUTER02_MAINNET, WETH_MAINNET
from backend.live_eth_quotes import STABLES

DID="did:privy:finalized-router02-owner"
WALLET="0x"+"a"*40
POOL="0x"+"b"*40
HASH="0x"+"c"*64
BLOCK="0x"+"d"*64


def topic(address):
    return "0x"+("0"*24)+address[2:].lower()


def event(contract,from_addr,to_addr,amount):
    return {"address":contract,"topics":[
        check._TRANSFER_TOPIC,topic(from_addr),topic(to_addr),
    ],"data":"0x"+f"{amount:064x}"}


def tx(side="BUY", stable="USDC"):
    q=build_unsigned_router02_swap(
        wallet=WALLET,side=side,stable=stable,
        amount_in=(100*10**6 if side=="BUY" else 10**17),
        min_out=(19*10**16 if side=="BUY" else 199*10**6),
        fee=500)
    return {
        "hash":HASH,"from":WALLET,"to":SWAPROUTER02_MAINNET,
        "chainId":"0x1","value":"0x0","input":q["data"],
        "blockNumber":"0x64","blockHash":BLOCK,
    }


def receipt(side="BUY",stable="USDC",status="0x1"):
    source=STABLES[stable] if side=="BUY" else WETH_MAINNET
    output=WETH_MAINNET if side=="BUY" else STABLES[stable]
    logs=([event(source,WALLET,POOL,
                 100*10**6 if side=="BUY" else 10**17),
           event(output,POOL,WALLET,
                 2*10**17 if side=="BUY" else 200*10**6)]
          if status=="0x1" else [])
    return {
        "transactionHash":HASH,"from":WALLET,
        "to":SWAPROUTER02_MAINNET,
        "blockHash":BLOCK,"blockNumber":"0x64",
        "status":status,"gasUsed":"0x5208",
        "effectiveGasPrice":"0x3b9aca00",
        "logs":logs,
    }


def inspect(*,side="BUY",stable="USDC",**patch):
    params={
        "wallet":WALLET,"side":side,"stable":stable,
        "chain_id":"0x1","transaction":tx(side,stable),
        "receipt":receipt(side,stable),"latest":"0x78",
        "finalized":{"number":"0x6e"},
    }
    params.update(patch)
    return check.inspect_eth_router02_receipt(HASH,**params)


@pytest.mark.parametrize("side,stable",[
    ("BUY","USDC"),("BUY","USDT"),("SELL","USDC"),("SELL","USDT"),
])
def test_finalized_router02_swap_has_exact_erc20_spend_and_received(
    side,stable,
):
    result=inspect(side=side,stable=stable)
    assert result["state"]=="FINALIZED_ERC20_SWAP_LEG"
    assert result["swap_erc20_leg_verified"] is True
    assert result["transaction_finalized"] is True
    assert result["confirmations"]==21
    assert result["network_fee_eth"]=="0.000021"
    assert result["source_spent_amount"]==(
        "100" if side=="BUY" else "0.1")
    assert result["destination_received_amount"]==(
        "0.2" if side=="BUY" else "200")
    assert result["swap_output_asset"]==(
        "WETH" if side=="BUY" else stable)
    assert result["native_eth_cycle_complete"] is False
    assert result["privy_signer_authorized"] is False
    assert result["execution_enabled"] is False
    assert result["real_order_recorded"] is False


def test_receipt_without_finality_is_not_a_verified_fill():
    result=inspect(finalized={"number":"0x63"})
    assert result["state"]=="INCLUDED_SUCCESS"
    assert result["swap_erc20_leg_verified"] is False
    assert result["destination_received_amount"] is None
    result=inspect(finalized=None)
    assert result["swap_erc20_leg_verified"] is False


def test_reverted_finalized_transaction_never_claims_swap_amount():
    result=inspect(receipt=receipt(status="0x0"))
    assert result["state"]=="FINALIZED_REVERTED"
    assert result["transaction_finalized"] is True
    assert result["swap_erc20_leg_verified"] is False
    assert result["source_spent_amount"] is None


def test_pending_unmined_router02_transaction_has_no_spend_or_fill():
    pending=tx()
    pending.update(blockNumber=None,blockHash=None)
    result=inspect(transaction=pending,receipt=None,finalized=None)
    assert result["state"]=="PENDING"
    assert result["transaction_seen"] is True
    assert result["swap_erc20_leg_verified"] is False
    assert result["network_fee_eth"] is None


def test_not_found_transaction_is_not_a_fill():
    result=inspect(transaction=None,receipt=None,finalized=None)
    assert result["state"]=="NOT_FOUND"
    assert result["transaction_seen"] is False


@pytest.mark.parametrize("patch",[
    {"wallet":"0x"+"f"*40},
    {"chain_id":"0x89"},
    {"latest":"0x63"},
    {"finalized":{"number":"0x90"}},
    {"transaction":{**tx(),"to":"0x"+"f"*40}},
    {"transaction":{**tx(),"from":"0x"+"f"*40}},
    {"transaction":{**tx(),"chainId":"0x89"}},
    {"transaction":{**tx(),"input":"0x3593564c"+"00"*224}},
    {"transaction":{**tx(),"value":"0x1"}},
    {"receipt":{**receipt(),"to":"0x"+"f"*40}},
    {"receipt":{**receipt(),"status":"0x2"}},
    {"receipt":{**receipt(),"blockHash":"0x"+"f"*64}},
    {"receipt":{**receipt(),"gasUsed":"0x0"}},
    {"receipt":{**receipt(),"effectiveGasPrice":"0x0"}},
])
def test_unowned_or_invalid_chain_data_never_reports_a_fill(patch):
    with pytest.raises(ValueError):
        inspect(**patch)


@pytest.mark.parametrize("mutate",[
    lambda logs:logs[0].update(address=STABLES["USDT"]),
    lambda logs:logs[1].update(address=STABLES["USDT"]),
    lambda logs:logs[0].update(data="0x"+f"{99*10**6:064x}"),
    lambda logs:logs[1].update(data="0x"+f"{1:064x}"),
    lambda logs:logs[1]["topics"].__setitem__(2,topic(POOL)),
    lambda logs:logs[0]["topics"].__setitem__(1,topic(POOL)),
    lambda logs:logs.append(logs[1].copy()),
    lambda logs:logs[1].update(data="0x"+f"{(10**45):064x}"),
    lambda logs:logs[1].update(topics=["0x123"]),
])
def test_fake_or_duplicate_transfer_event_rejected(mutate):
    raw=receipt()
    mutate(raw["logs"])
    with pytest.raises(ValueError):
        inspect(receipt=raw)


def test_transfer_topic_must_be_canonical_and_wallet_bound():
    with pytest.raises(ValueError):
        check._topic_address("0x"+("f"*64))
    with pytest.raises(ValueError):
        check._topic_address("not a topic")


@pytest.mark.parametrize("bad",["",42,None,"a"*64,"0x"+"z"*64])
def test_invalid_transaction_hash_rejected_before_wallet_or_rpc(bad):
    with pytest.raises(HTTPException) as err:
        asyncio.run(check.check_user_router02_receipt(
            DID,bad,side="BUY",stable="USDC"))
    assert err.value.status_code==422


@pytest.fixture
def verified_rpc(monkeypatch):
    calls=[]
    async def owner(did):
        assert did==DID
        return {"id":DID,"linked_accounts":[{
            "type":"wallet","chain_type":"ethereum",
            "wallet_client_type":"privy","address":WALLET,
        }]}
    async def rpc(client,method,params,req_id):
        calls.append((method,params,req_id))
        data={
            "eth_chainId":"0x1",
            "eth_getTransactionByHash":tx(),
            "eth_getTransactionReceipt":receipt(),
            "eth_blockNumber":"0x78",
            "eth_getBlockByNumber":{"number":"0x6e"},
        }
        return data[method]
    monkeypatch.setattr(check,"fetch_privy_user",owner)
    monkeypatch.setattr(check,"_rpc",rpc)
    return calls


def test_read_real_rpc_for_owner_only_and_do_not_mutate_any_ledger(verified_rpc):
    result=asyncio.run(check.check_user_router02_receipt(
        DID,HASH,side="BUY",stable="USDC"))
    assert result["swap_erc20_leg_verified"] is True
    assert result["native_eth_cycle_complete"] is False
    assert result["real_order_recorded"] is False
    assert [item[0] for item in verified_rpc]==[
        "eth_chainId","eth_getTransactionByHash",
        "eth_getTransactionReceipt","eth_blockNumber","eth_getBlockByNumber",
    ]


def test_user_without_verified_privy_eth_wallet_cannot_probe_receipts(
    verified_rpc,monkeypatch,
):
    async def missing(did):
        return {"id":did,"linked_accounts":[]}
    monkeypatch.setattr(check,"fetch_privy_user",missing)
    with pytest.raises(HTTPException) as err:
        asyncio.run(check.check_user_router02_receipt(
            DID,HASH,side="BUY",stable="USDC"))
    assert err.value.status_code==503
    assert verified_rpc==[]


def test_authenticated_router02_receipt_api(verified_rpc,monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    public=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"router02-fill-receipt",
        "iat":now,"exp":now+600,
    },key,algorithm="ES256")
    headers={"Authorization":"Bearer "+token}
    client=TestClient(api.app)
    endpoint="/api/live/eth/router02-receipt/"+HASH+"?side=BUY&stable=USDC"
    assert client.get(endpoint).status_code==401
    assert client.post(endpoint,headers=headers).status_code==405
    ok=client.get(endpoint,headers=headers)
    assert ok.status_code==200
    assert ok.headers["cache-control"]=="private, no-store"
    assert ok.json()["swap_erc20_leg_verified"] is True
    assert ok.json()["native_eth_cycle_complete"] is False
