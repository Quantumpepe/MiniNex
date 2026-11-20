"""Real-source RPC transport contract, mocked HTTP (never real mainnet)."""
from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from backend.native_live_broadcast_transport import (
    broadcast_original_native_signed_once,
    signed_native_transaction_hash,
    _b58,
)


class Transport:
    def __init__(self, content, *, status=200):
        self.content=content
        self.status=status
        self.calls=[]
    async def post(self,url,**kwargs):
        self.calls.append((url,kwargs))
        request=httpx.Request("POST",url)
        if type(self.content) is dict:
            return httpx.Response(self.status,json=self.content,request=request)
        return httpx.Response(self.status,text=self.content,request=request)


def btc():
    from backend.tests.test_native_btc_signed_wire import valid
    from backend.native_btc_privy_signed_readback import inspect_original_btc_signed_transaction
    from backend.native_btc_signed_wire import verify_original_btc_detached_signatures
    psbt,utxos,pub,sigs=valid(two=True)
    result=verify_original_btc_detached_signatures(
        psbt,original_utxos=utxos,
        original_owner_public_key_hex=pub,
        original_signatures_hex=sigs,
    )
    raw=result["signed_wire_base64"]
    proof=inspect_original_btc_signed_transaction(
        psbt,original_utxos=utxos,signed_wire_base64=raw,
    )
    return raw,proof


def sol():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    from backend.tests.test_native_sol_compiled_message import params,b58
    from backend.native_sol_compiled_message import compile_original_unsigned_sol_message
    from backend.native_sol_privy_signed_readback import inspect_original_sol_signed_transaction
    priv=ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(1,33)))
    pub=priv.public_key().public_bytes(
        serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    opts=params()
    old=opts["quote"]["source_wallet"]
    owner=b58(pub)
    opts["quote"]["source_wallet"]=owner
    opts["quote"]["memo"]=opts["quote"]["memo"].replace("/"+old+":","/"+owner+":")
    opts["source_account"]["owner"]=owner
    unsigned=compile_original_unsigned_sol_message(**opts)
    message=base64.b64decode(unsigned["message_base64"])
    wire=base64.b64encode(b"\x01"+priv.sign(message)+message).decode("ascii")
    proof=inspect_original_sol_signed_transaction(
        unsigned,signed_transaction_base64=wire)
    return wire,proof


def xrp():
    from backend.tests.test_native_xrp_signed_wire import signed
    from backend.tests.test_native_xrp_thor_payment_tx import NOW
    from backend.native_xrp_signed_wire import verify_original_xrp_detached_signature
    from backend.native_xrp_privy_signed_readback import inspect_original_xrp_signed_payment
    unsigned,pub,sig=signed("secp256k1")
    local=verify_original_xrp_detached_signature(
        unsigned,original_owner_public_key_hex=pub,
        detached_signature_hex=sig,now=NOW)
    wire=local["signed_wire_base64"]
    proof=inspect_original_xrp_signed_payment(
        unsigned,original_owner_public_key_hex=pub,
        signed_payment_base64=wire,now=NOW)
    return wire,proof


@pytest.mark.parametrize("asset,fixture",[
    ("BTC",btc),("SOL",sol),("XRP",xrp),
])
def test_actual_signed_source_proof_determines_hash_prior_to_any_send(asset,fixture):
    wire,proof=fixture()
    txid=signed_native_transaction_hash(asset,proof,wire)
    if asset=="BTC":
        assert txid==proof["original_unsigned_txid"]
        assert len(txid)==64
    elif asset=="SOL":
        assert txid==_b58(base64.b64decode(wire)[1:65])
        assert 80<=len(txid)<=90
    else:
        assert txid==proof["signed_transaction_hash"]
        assert len(txid)==64
    assert proof["can_submit"] is False


@pytest.mark.parametrize("asset,fixture",[
    ("BTC",btc),("SOL",sol),("XRP",xrp),
])
def test_signed_source_mutation_cannot_reach_transport(asset,fixture):
    wire,proof=fixture()
    raw=bytearray(base64.b64decode(wire))
    raw[-1]^=1
    changed=base64.b64encode(raw).decode("ascii")
    with pytest.raises(ValueError):
        signed_native_transaction_hash(asset,proof,changed)
    with pytest.raises(ValueError):
        signed_native_transaction_hash(asset,{**proof,"asset":"OTHER"},wire)
    with pytest.raises(ValueError):
        signed_native_transaction_hash(asset,{**proof,"signed_wire_sha256":"0"*64},wire)


def test_btc_broadcast_posts_raw_hex_once_no_retries_or_finality():
    wire,proof=btc()
    txid=signed_native_transaction_hash("BTC",proof,wire)
    http=Transport(txid)
    result=asyncio.run(broadcast_original_native_signed_once(
        http,asset="BTC",signed_base64=wire,expected_source_hash=txid))
    assert len(http.calls)==1
    url,call=http.calls[0]
    assert url.endswith("/tx")
    assert call["content"]==base64.b64decode(wire).hex()
    assert call["headers"]["Content-Type"]=="text/plain"
    assert result["source_tx_hash"]==txid
    assert result["network_accepted"] is True
    assert result["source_chain_confirmed"] is False
    assert result["crosschain_swap_settled"] is False
    assert result["fee_assessed"] is False


def test_sol_broadcast_rpc_signature_verified_with_no_rpc_retries():
    wire,proof=sol()
    txid=signed_native_transaction_hash("SOL",proof,wire)
    http=Transport({"jsonrpc":"2.0","id":1,"result":txid})
    result=asyncio.run(broadcast_original_native_signed_once(
        http,asset="SOL",signed_base64=wire,expected_source_hash=txid))
    assert result["network_accepted"] is True
    assert result["source_chain_confirmed"] is False
    assert len(http.calls)==1
    url,call=http.calls[0]
    assert call["json"]["method"]=="sendTransaction"
    assert call["json"]["params"][0]==wire
    assert call["json"]["params"][1]=={
        "encoding":"base64","skipPreflight":False,
        "preflightCommitment":"finalized","maxRetries":0,
    }


def test_xrp_broadcast_submits_only_signed_tx_blob_and_checks_acceptance():
    wire,proof=xrp()
    txid=signed_native_transaction_hash("XRP",proof,wire)
    http=Transport({"result":{
        "status":"success","accepted":True,
        "engine_result":"tesSUCCESS",
        "tx_blob":base64.b64decode(wire).hex().upper(),
        "tx_json":{"hash":txid},
    }})
    result=asyncio.run(broadcast_original_native_signed_once(
        http,asset="XRP",signed_base64=wire,expected_source_hash=txid))
    assert result["source_tx_hash"]==txid
    assert result["destination_fill_verified"] is False
    assert len(http.calls)==1
    url,call=http.calls[0]
    assert call["json"]["method"]=="submit"
    assert call["json"]["params"]==[{
        "tx_blob":base64.b64decode(wire).hex().upper(),
        "fail_hard":True,
    }]


@pytest.mark.parametrize("asset,fixture,rejected",[
    ("BTC",btc,"f"*64),
    ("SOL",sol,{"jsonrpc":"2.0","id":1,"result":"different"}),
    ("XRP",xrp,{"result":{
        "status":"success","accepted":False,"engine_result":"tefFAILURE"}}),
])
def test_network_rejection_is_not_a_second_send(asset,fixture,rejected):
    wire,proof=fixture()
    txid=signed_native_transaction_hash(asset,proof,wire)
    http=Transport(rejected)
    with pytest.raises(ValueError):
        asyncio.run(broadcast_original_native_signed_once(
            http,asset=asset,signed_base64=wire,
            expected_source_hash=txid))
    assert len(http.calls)==1


@pytest.mark.parametrize("asset,fixture",[
    ("BTC",btc),("SOL",sol),("XRP",xrp),
])
def test_network_http_error_never_triggers_implicit_resend(asset,fixture):
    wire,proof=fixture()
    txid=signed_native_transaction_hash(asset,proof,wire)
    http=Transport("upstream fail",status=500)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(broadcast_original_native_signed_once(
            http,asset=asset,signed_base64=wire,
            expected_source_hash=txid))
    assert len(http.calls)==1
