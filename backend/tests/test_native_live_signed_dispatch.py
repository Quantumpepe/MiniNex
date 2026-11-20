"""Internal native SELL execution: local signatures, durable claim, one send.

Every HTTP call is mocked. These tests must NEVER transmit real funds.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from backend import native_live_signed_dispatch as d
from backend.native_sol_compiled_message import compile_original_unsigned_sol_message
from backend.tests.test_native_sol_compiled_message import params, NOW,b58


def signed_sol():
    key=ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(1,33)))
    pub=key.public_key().public_bytes(
        serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    p=params()
    previous=p["quote"]["source_wallet"]
    source=b58(pub)
    p["quote"]["source_wallet"]=source
    p["quote"]["memo"]=p["quote"]["memo"].replace(
        "/"+previous+":", "/"+source+":")
    p["source_account"]["owner"]=source
    manifest=compile_original_unsigned_sol_message(**p)
    message=base64.b64decode(manifest["message_base64"])
    wire=base64.b64encode(b"\x01"+key.sign(message)+message).decode("ascii")
    quote=p["quote"]
    order={
        "privy_did":"did:privy:real_owner_01",
        "asset":"SOL","side":"SELL","stable":"USDC",
        "status":"PREPARED","source_wallet":source,
        "destination_wallet":quote["destination_wallet"],
        "input_amount":Decimal(quote["amount"]),
        "intent_key":"native_"+"a"*64,
        "payload_sha256":"b"*64,
        "immutable_quote_sha256":d._original_quote_digest(quote),
        "quote_expires_at":quote["quote_expires_at"],
    }
    return order,quote,manifest,wire


def test_source_quote_and_signed_sol_are_bound_to_immutable_order():
    order,quote,manifest,wire=signed_sol()
    proof=d.inspect_original_order_source_for_dispatch(
        order,quote=quote,manifest=manifest,
        signed_wire_base64=wire,now=NOW,
    )
    assert proof["asset"]=="SOL"
    assert proof["source_wallet"]==order["source_wallet"]
    assert proof["original_intent_key"]==order["intent_key"]
    assert proof["source_crypto_verified"] is True
    assert proof["source_transaction_sent"] is False
    assert proof["crosschain_swap_filled"] is False
    assert len(proof["signed_source_tx_hash"])>=80


@pytest.mark.parametrize("field,wrong",[
    ("source_wallet","forged"),
    ("destination_wallet","0x"+"f"*40),
    ("input_amount",Decimal("0.2")),
    ("status","SENDING"),
    ("immutable_quote_sha256","a"*64),
])
def test_order_changes_rejected_before_claim(field,wrong):
    order,quote,manifest,wire=signed_sol()
    order[field]=wrong
    with pytest.raises(ValueError):
        d.inspect_original_order_source_for_dispatch(
            order,quote=quote,manifest=manifest,
            signed_wire_base64=wire,now=NOW,
        )


@pytest.mark.parametrize("field,wrong",[
    ("memo","=:USDC.USDC:another/other:0/1/0"),
    ("inbound_vault","different"),
    ("amount","2.25"),
    ("source_wallet","not owner"),
    ("destination_wallet","0x"+"c"*40),
])
def test_original_quote_cannot_be_substituted(field,wrong):
    order,quote,manifest,wire=signed_sol()
    quote[field]=wrong
    with pytest.raises(ValueError):
        d.inspect_original_order_source_for_dispatch(
            order,quote=quote,manifest=manifest,
            signed_wire_base64=wire,now=NOW,
        )


def test_expired_quote_or_altered_signature_cannot_claim():
    order,quote,manifest,wire=signed_sol()
    with pytest.raises(ValueError):
        d.inspect_original_order_source_for_dispatch(
            order,quote=quote,manifest=manifest,
            signed_wire_base64=wire,now=NOW+180)
    raw=bytearray(base64.b64decode(wire))
    raw[20]^=1
    with pytest.raises(ValueError):
        d.inspect_original_order_source_for_dispatch(
            order,quote=quote,manifest=manifest,
            signed_wire_base64=base64.b64encode(raw).decode("ascii"),
            now=NOW)


class _FakeCursor:
    def __enter__(self):return self
    def __exit__(self,*a):return False

class _FakeConnection:
    def __init__(self,events):self.events=events
    def __enter__(self):self.events.append("db-start");return self
    def __exit__(self,*a):self.events.append("db-committed");return False
    def cursor(self,**kw):return _FakeCursor()

class _FakeClient:
    def __init__(self,events):self.events=events
    async def __aenter__(self):return self
    async def __aexit__(self,*a):return False


async def _run(monkeypatch,*,remote_ok=True,transport_error=False):
    order,quote,manifest,wire=signed_sol()
    events=[]
    def account(did,asset,intent_key):
        events.append("read-durable-order")
        return order
    async def remote(did,*,asset):
        events.append("remote-owner")
        return {
            "original_wallet_remote_owner_verified":remote_ok,
            "original_public_key_matches_wallet":True,
            "privy_native_wallet_api_supported":True,
            "execution_enabled":False,"can_submit":False,
        }
    def claim(cur,**kwargs):
        events.append("durable-claim")
        assert kwargs["signed_source_tx_hash"]
        assert kwargs["permission_audit"]["exact_remote_policy_verified"] is True
        return "SENDING"
    async def send(client,**kwargs):
        events.append("one-mainnet-broadcast")
        if transport_error:
            raise httpx.ReadTimeout("ambiguous source submission")
        return {"network_accepted":True}
    def record(cur,**kwargs):
        events.append("persist-network-outcome")
        return "SUBMITTED" if kwargs["network_accepted"] else "RECONCILIATION_REQUIRED"
    monkeypatch.setattr(d,"_get_original_prepared_source",account)
    monkeypatch.setattr(d,"get_original_native_privy_signer_status",remote)
    monkeypatch.setattr(d,"_claim_signed_native_source_once_locked",claim)
    monkeypatch.setattr(d,"broadcast_original_native_signed_once",send)
    monkeypatch.setattr(d,"_record_signed_native_source_result_locked",record)
    monkeypatch.setattr(d,"_db_url",lambda:"postgres://mock")
    monkeypatch.setattr(d,"_ensure_schema",lambda *_:None)
    monkeypatch.setattr(d.psycopg,"connect",lambda *a,**kw:_FakeConnection(events))
    monkeypatch.setattr(d.httpx,"AsyncClient",lambda **kw:_FakeClient(events))
    monkeypatch.setattr(d.time,"time",lambda:NOW)
    audit={
        "did":order["privy_did"],"asset":"SOL",
        "exact_remote_policy_verified":True,
        "owner_wallet_current":True,"backend_quorum_current":True,
        "owner_signed_budget_current":True,
    }
    if remote_ok:
        result=await d.dispatch_signed_original_native_once(
            order["privy_did"],asset="SOL",intent_key=order["intent_key"],
            quote=quote,manifest=manifest,signed_wire_base64=wire,
            permission_audit=audit,
        )
        assert result["retry_allowed"] is False
        assert result["finalized"] is False
        assert result["destination_settled"] is False
        assert result["journal_state"]==(
            "RECONCILIATION_REQUIRED" if transport_error else "SUBMITTED")
        assert events==[
            "read-durable-order","remote-owner","db-start","durable-claim",
            "db-committed","one-mainnet-broadcast","db-start",
            "persist-network-outcome","db-committed",
        ]
    else:
        with pytest.raises(HTTPException) as exc:
            await d.dispatch_signed_original_native_once(
                order["privy_did"],asset="SOL",intent_key=order["intent_key"],
                quote=quote,manifest=manifest,signed_wire_base64=wire,
                permission_audit=audit,
            )
        assert exc.value.status_code==409
        assert events==["read-durable-order","remote-owner"]


def test_durable_claim_must_commit_before_real_native_http(monkeypatch):
    asyncio.run(_run(monkeypatch))

def test_ambiguous_network_outcome_never_rebroadcasts(monkeypatch):
    asyncio.run(_run(monkeypatch,transport_error=True))

def test_unverified_remote_owner_blocks_before_journal_claim(monkeypatch):
    asyncio.run(_run(monkeypatch,remote_ok=False))


def test_dispatch_has_no_public_route_or_live_auto_scheduler():
    import inspect
    src=inspect.getsource(d)
    for forbidden in (
        '@app.post','@app.get','BackgroundTasks','scheduler.add_job',
        'create_task(','while True:', 'privy-wallet-action',
    ):
        assert forbidden not in src
