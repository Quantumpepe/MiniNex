"""Real Ethereum mainnet wallet-owned receipt verification, never signing."""
import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from fastapi import HTTPException

from backend import app as api
from backend import live_eth_receipts as er
from backend.live_eth_swap_plan import _ROUTER

DID = "did:privy:receipt-test"
WALLET = "0x" + "b"*40
HASH = "0x" + "a"*64
BLOCK_HASH = "0x" + "c"*64


def tx():
    return {
        "hash": HASH, "from": WALLET, "to": _ROUTER,
        "chainId": "0x1",
        "input": "0x3593564c" + "00"*96,
        "blockNumber": "0x64", "blockHash": BLOCK_HASH,
    }


def receipt(status="0x1"):
    return {
        "transactionHash": HASH, "from": WALLET, "to": _ROUTER,
        "blockNumber": "0x64", "blockHash": BLOCK_HASH,
        "status": status, "gasUsed": "0x5208",
        "effectiveGasPrice": "0x3b9aca00",
    }


def inspect(**patch):
    options = {
        "wallet": WALLET, "chain_id": "0x1",
        "transaction": tx(), "receipt": receipt(),
        "latest": "0x78", "finalized": {"number": "0x6e"},
    }
    options.update(patch)
    return er.inspect_eth_swap_receipt(HASH, **options)


def test_verified_receipt_finality_and_paid_fee_are_exact():
    result = inspect()
    assert result["state"] == "FINALIZED_SUCCESS"
    assert result["transaction_finalized"] is True
    assert result["transaction_success"] is True
    assert result["transaction_seen"] is True
    assert result["confirmations"] == 21
    assert result["network_fee_eth"] == "0.000021"
    assert result["execution_enabled"] is False
    assert result["can_submit"] is False
    assert result["swap_outputs_verified"] is False
    assert result["live_order_recorded"] is False
    assert "token" in result["warning"].lower()


def test_real_pending_unknown_included_and_reverted_never_claim_fill():
    assert inspect(transaction=None,receipt=None)["state"] == "NOT_FOUND"
    pending = {**tx(), "blockNumber": None, "blockHash": None}
    p = inspect(transaction=pending,receipt=None)
    assert p["state"] == "PENDING" and p["network_fee_eth"] is None
    assert p["transaction_finalized"] is False
    included = inspect(finalized={"number":"0x60"})
    assert included["state"] == "INCLUDED_SUCCESS"
    assert included["transaction_finalized"] is False
    assert inspect(finalized=None)["state"] == "INCLUDED_SUCCESS"
    assert inspect(receipt=receipt("0x0"))["state"] == "FINALIZED_REVERTED"
    assert inspect(receipt=receipt("0x0"),finalized=None)["state"] == "INCLUDED_REVERTED"


@pytest.mark.parametrize("changed", [
    {"chain_id":"0x38"},
    {"wallet":"0x"+"f"*40},
    {"transaction":{**tx(),"from":"0x"+"d"*40}},
    {"transaction":{**tx(),"to":"0x"+"d"*40}},
    {"transaction":{**tx(),"hash":"0x"+"f"*64}},
    {"transaction":{**tx(),"chainId":"0x38"}},
    {"transaction":{**tx(),"input":"0xdeadbeef"+"00"*96}},
    {"transaction":{**tx(),"input":"0x3593564c00"}},
    {"transaction":{**tx(),"blockHash":"0x"+"e"*64}},
    {"receipt":{**receipt(),"transactionHash":"0x"+"e"*64}},
    {"receipt":{**receipt(),"from":"0x"+"f"*40}},
    {"receipt":{**receipt(),"to":"0x"+"f"*40}},
    {"receipt":{**receipt(),"blockNumber":"0x65"}},
    {"receipt":{**receipt(),"blockHash":"0x"+"f"*64}},
    {"receipt":{**receipt(),"status":"0x2"}},
    {"receipt":{**receipt(),"gasUsed":"0x0"}},
    {"receipt":{**receipt(),"effectiveGasPrice":"0x0"}},
    {"receipt":{**receipt(),"gasUsed":"0x1c9c381"}},
    {"latest":"0x63"},
    {"finalized":{"number":"0x79"}},
    {"transaction":None},
])
def test_untrusted_rpc_identity_block_status_and_gas_fail_closed(changed):
    if changed == {"transaction":None}:
        with pytest.raises(ValueError):
            inspect(**changed)
    else:
        with pytest.raises(ValueError):
            inspect(**changed)


@pytest.mark.parametrize("bad", ["", "0x123", "0x"+"z"*64, "a"*64, None, 42])
def test_invalid_transaction_hash_rejected_before_rpc(bad):
    with pytest.raises(HTTPException) as error:
        asyncio.run(er.check_eth_swap_receipt(DID, bad))
    assert error.value.status_code == 422


def test_no_rpc_if_user_wallet_not_independently_verified(monkeypatch):
    async def bad_user(did):
        return {"id":DID,"linked_accounts":[]}
    monkeypatch.setattr(er,"fetch_privy_user",bad_user)
    with pytest.raises(HTTPException) as error:
        asyncio.run(er.check_eth_swap_receipt(DID,HASH))
    assert error.value.status_code == 503


def test_real_on_demand_receipt_rpc_uses_only_verified_owner_and_read_calls(monkeypatch):
    async def user(did):
        assert did == DID
        return {"id":DID,"linked_accounts":[{
            "type":"wallet","chain_type":"ethereum",
            "wallet_client_type":"privy","address":WALLET,
        }]}
    monkeypatch.setattr(er,"fetch_privy_user",user)
    seen=[]
    originals=httpx.AsyncClient
    results={
        "eth_chainId":"0x1",
        "eth_getTransactionByHash":tx(),
        "eth_getTransactionReceipt":receipt(),
        "eth_blockNumber":"0x78",
        "eth_getBlockByNumber":{"number":"0x6e"},
    }
    def mocked_client(*args,**kwargs):
        assert kwargs["follow_redirects"] is False
        def serve(request):
            assert request.url == er.ETH_RPC
            body=json.loads(request.content)
            seen.append((body["id"],body["method"],body["params"]))
            return httpx.Response(200,json={
                "jsonrpc":"2.0","id":body["id"],"result":results[body["method"]],
            })
        return originals(*args,transport=httpx.MockTransport(serve),**kwargs)
    monkeypatch.setattr(er.httpx,"AsyncClient",mocked_client)
    found=asyncio.run(er.check_eth_swap_receipt(DID,HASH))
    assert found["state"]=="FINALIZED_SUCCESS"
    assert len(seen)==5
    assert {x[1] for x in seen}==set(results)
    assert not any(name.startswith(("eth_send","personal_","eth_sign")) for _,name,_ in seen)
    assert all(params==[HASH] for _,name,params in seen if name in
               ("eth_getTransactionByHash","eth_getTransactionReceipt"))


@pytest.fixture
def headers(monkeypatch):
    signer=ec.generate_private_key(ec.SECP256R1())
    public=signer.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"eth-chain-status",
        "iat":now,"exp":now+600,
    },signer,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_receipt_http_is_authenticated_get_only_and_never_signs(monkeypatch,headers):
    client=TestClient(api.app)
    route=f"/api/live/eth/tx-receipt/{HASH}"
    assert client.get(route).status_code==401
    assert client.get(route,headers={"Authorization":"Bearer invalid"}).status_code==401
    recorded=[]
    async def readonly(did,tx_hash):
        recorded.append((did,tx_hash))
        return inspect()
    monkeypatch.setattr(api,"check_eth_swap_receipt",readonly)
    result=client.get(route,headers=headers)
    assert result.status_code==200
    assert result.headers["cache-control"]=="private, no-store"
    assert result.headers["x-content-type-options"]=="nosniff"
    assert result.json()["transaction_finalized"] is True
    assert result.json()["swap_outputs_verified"] is False
    assert recorded==[(DID,HASH)]
    assert client.post(route,headers=headers,json={}).status_code==405
    assert not any(r.path=="/api/live/eth/tx-receipt/{tx_hash}/send"
                   for r in api.app.routes)
