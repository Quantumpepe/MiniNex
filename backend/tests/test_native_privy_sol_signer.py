"""Real Privy SOL sign-only integration; mocked HTTP never spends actual funds."""
from __future__ import annotations

import asyncio
import base64
import copy

import httpx
import pytest
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend import native_privy_sol_signer as s
from backend.tests.test_native_sol_privy_signed_readback import original_signed
from backend.native_coin_balances import _BASE58,_decode_b58

DID="did:privy:original_sol_wallet"
WALLET="wallet_sol_verified_012345678"
SIGNER="quorum_sol_verified_012345678"
POLICY="policy_sol_verified_012345678"
INTENT="native_"+"a"*64


def setup(monkeypatch):
    manifest,wire=original_signed()
    owner=manifest["source_wallet"]
    pub=_decode_b58(owner,_BASE58)
    key=ec.generate_private_key(ec.SECP256R1())
    private=key.private_bytes(serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    public=key.public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setenv(s._PRIVATE_ENV,base64.b64encode(private).decode())
    monkeypatch.setenv(s._PUBLIC_ENV,base64.b64encode(public).decode())
    monkeypatch.setenv(s._QUORUM_ENV,SIGNER)
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET","only-test-not-real-secret-xxxxxxxx")
    async def user(did):
        assert did==DID
        return {"id":DID}
    async def remote(wallet_id):
        assert wallet_id==WALLET
        return {"id":WALLET,"public_key":"0x"+pub.hex(),
                "additional_signers":[{"signer_id":SIGNER,
                    "override_policy_ids":[POLICY]}]}
    def owned(user,did,asset):
        assert (user["id"],did,asset)==(DID,DID,"SOL")
        return {"wallet_id":WALLET,"address":owner,"delegated":True}
    def inspect(**kw):
        assert kw["did"]==DID and kw["asset"]=="SOL"
        return {"original_wallet_remote_owner_verified":True,
                "original_public_key_matches_wallet":True,
                "privy_native_wallet_api_supported":True}
    monkeypatch.setattr(s,"fetch_privy_user",user)
    monkeypatch.setattr(s,"fetch_privy_wallet_metadata",remote)
    monkeypatch.setattr(s,"_original_privy_native_wallet",owned)
    monkeypatch.setattr(s,"inspect_original_native_signer_metadata",inspect)
    audit={
        "audit_mode":"CURRENT_NATIVE_OWNER_SIGNED_GRANT",
        "did":DID,"asset":"SOL","intent_key":INTENT,
        "source_wallet":owner,"wallet_id":WALLET,
        "signer_id":SIGNER,"policy_id":POLICY,
        "original_message_sha256":manifest["message_sha256"],
        "source_message_sha256":manifest["message_sha256"],
        "owner_wallet_current":True,
        "exact_remote_policy_verified":True,
        "backend_quorum_current":True,
        "owner_signed_budget_current":True,
        "user_consent_current":True,
        "budget_revision":1,
        "policy_sha256":"a"*64,"owner_consent_sha256":"b"*64,
    }
    return manifest,wire,audit,key


class FakeClient:
    def __init__(self,wire,*,broken=False):
        self.wire=wire
        self.calls=[]
        self.broken=broken
    async def post(self,url,**kwargs):
        self.calls.append((url,kwargs))
        supplied=self.wire if not self.broken else b"\x01"+bytes(64)+self.wire[65:]
        return httpx.Response(status_code=200,
            json={"data":{"signed_transaction":base64.b64encode(supplied).decode()}},
            request=httpx.Request("POST",url))


def test_real_sol_privy_rpc_signs_only_original_wallet_message(monkeypatch):
    manifest,wire,audit,key=setup(monkeypatch)
    client=FakeClient(wire)
    result=asyncio.run(s.sign_original_sol_with_privy_once(
        DID,original_message=manifest,intent_key=INTENT,
        permission_audit=audit,client=client))
    assert result["mode"]=="SIGNED_ORIGINAL_SOL_PRIVY_INTERNAL_ONLY"
    assert result["original_owner_signature_verified"] is True
    assert result["wallet_signed"] is True
    assert result["source_transaction_sent"] is False
    assert result["destination_settled"] is False
    assert result["signed_transaction_base64"]==base64.b64encode(wire).decode()
    assert len(client.calls)==1
    url,kwargs=client.calls[0]
    assert url.endswith(WALLET+"/rpc")
    assert kwargs["json"]["method"]=="solana_signTransaction"
    assert kwargs["json"]["params"]["encoding"]=="base64"
    assert kwargs["json"]["params"]["transaction"]
    assert kwargs["headers"]["privy-app-id"]==s.APP_ID
    assert "privy-authorization-signature" in kwargs["headers"]
    signature=base64.b64decode(kwargs["headers"]["privy-authorization-signature"])
    payload=s._canonical_authorization_payload(
        url=url,body=kwargs["json"],key=kwargs["headers"]["privy-idempotency-key"])
    key.public_key().verify(signature,payload,ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize("changed",[
    ("exact_remote_policy_verified",False),
    ("user_consent_current",False),
    ("owner_signed_budget_current",False),
    ("backend_quorum_current",False),
    ("wallet_id","another_wallet"),
    ("intent_key","native_"+"f"*64),
    ("original_message_sha256","b"*64),
    ("budget_revision",0),
])
def test_no_sol_signing_without_exact_owner_grant_and_signal(monkeypatch,changed):
    manifest,wire,audit,_=setup(monkeypatch)
    audit[changed[0]]=changed[1]
    client=FakeClient(wire)
    with pytest.raises((ValueError,s.HTTPException)):
        asyncio.run(s.sign_original_sol_with_privy_once(
            DID,original_message=manifest,intent_key=INTENT,
            permission_audit=audit,client=client))
    assert client.calls==[]


def test_sol_privy_wrong_signed_message_never_dispatches(monkeypatch):
    manifest,wire,audit,_=setup(monkeypatch)
    client=FakeClient(wire,broken=True)
    with pytest.raises(s.HTTPException) as exc:
        asyncio.run(s.sign_original_sol_with_privy_once(
            DID,original_message=manifest,intent_key=INTENT,
            permission_audit=audit,client=client))
    assert exc.value.status_code==503
    assert len(client.calls)==1


def test_sign_then_dispatch_connects_once_without_public_api(monkeypatch):
    manifest,wire,audit,_=setup(monkeypatch)
    client=FakeClient(wire)
    sent=[]
    async def dispatch(did,**kwargs):
        sent.append((did,kwargs))
        return {"mode":"ORIGINAL_NATIVE_EXECUTED_ONCE_PENDING_FINALITY",
                "retry_allowed":False,"journal_state":"SUBMITTED"}
    monkeypatch.setattr(s,"dispatch_signed_original_native_once",dispatch)
    quote={"from_network":"solana-mainnet"}
    result=asyncio.run(s.sign_and_dispatch_original_sol_sell_once(
        DID,intent_key=INTENT,quote=quote,manifest=manifest,
        permission_audit=audit,client=client))
    assert result["journal_state"]=="SUBMITTED"
    assert len(client.calls)==1 and len(sent)==1
    assert sent[0][1]["asset"]=="SOL"
    assert sent[0][1]["signed_wire_base64"]==base64.b64encode(wire).decode()
    assert sent[0][1]["quote"] is quote


def test_native_signer_code_cannot_grant_policy_or_send_funds_itself():
    import inspect
    source=inspect.getsource(s.sign_original_sol_with_privy_once)
    assert '"solana_signAndSendTransaction"' not in source
    assert "broadcast_original_native_signed_once" not in source
    assert "addSigners(" not in source
    assert "createPolicy(" not in source
