"""Actual BTC Privy input sign transport tested only with mocked endpoints."""
from __future__ import annotations
import asyncio
import base64
import httpx
import pytest
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import ec
from backend import native_privy_btc_signer as s
from backend import eth_privy_p256_signer as pinned
from backend.tests.test_native_btc_signed_wire import valid
DID="did:privy:btc_owner_very_real"
WALLET="wallet_btc_verified_012345678"
QUORUM="quorum_btc_verified_012345678"
POLICY="policy_btc_verified_012345678"
INTENT="native_"+"a"*64

def configure(monkeypatch,two=False):
    psbt,utxos,public,raw_signatures=valid(two=two)
    owner=psbt["source_wallet"]
    private=ec.generate_private_key(ec.SECP256R1())
    encoded=private.private_bytes(serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    pub=private.public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setenv(pinned._KEY_ENV,base64.b64encode(encoded).decode())
    monkeypatch.setenv(pinned._PUBLIC_ENV,base64.b64encode(pub).decode())
    monkeypatch.setenv(s._QUORUM_ENV,QUORUM)
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET","test-sol-btc-credential-xxxxxxxxx")
    async def get_user(did):
        assert did==DID
        return {"id":DID}
    def linked(user,did,asset):
        assert asset=="BTC"
        return {"wallet_id":WALLET,"address":owner,"delegated":True}
    async def get_remote(wallet):
        assert wallet==WALLET
        return {"public_key":"0x"+public,"additional_signers":[
            {"signer_id":QUORUM,"override_policy_ids":[POLICY]}]}
    def inspect(**kw):
        assert kw["asset"]=="BTC"
        return {"original_wallet_remote_owner_verified":True,
                "original_public_key_matches_wallet":True,
                "privy_native_wallet_api_supported":True}
    monkeypatch.setattr(s,"fetch_privy_user",get_user)
    monkeypatch.setattr(s,"_original_privy_native_wallet",linked)
    monkeypatch.setattr(s,"fetch_privy_wallet_metadata",get_remote)
    monkeypatch.setattr(s,"inspect_original_native_signer_metadata",inspect)
    audit={
        "audit_mode":"CURRENT_NATIVE_OWNER_SIGNED_GRANT",
        "asset":"BTC","did":DID,"intent_key":INTENT,
        "wallet_id":WALLET,"source_wallet":owner,
        "signer_id":QUORUM,"policy_id":POLICY,
        "original_psbt_sha256":psbt["psbt_sha256"],
        "owner_wallet_current":True,
        "exact_remote_policy_verified":True,
        "backend_quorum_current":True,
        "owner_signed_budget_current":True,
        "user_consent_current":True,
        "budget_revision":1,
        "owner_consent_sha256":"b"*64,
        "policy_sha256":"c"*64,
    }
    return psbt,utxos,raw_signatures,audit

class Client:
    def __init__(self,signatures):
        self.calls=[]
        self.signatures=signatures
    async def post(self,url,**kwargs):
        self.calls.append((url,kwargs))
        idx=len(self.calls)-1
        signature=self.signatures[idx]
        return httpx.Response(status_code=200,
            json={"method":"raw_sign","data":{"signature":"0x"+signature+"00","encoding":"hex"}},
            request=httpx.Request("POST",url))

@pytest.mark.parametrize("two",[False,True])
def test_privy_btc_signs_every_exact_original_utxo_then_builds_canonical_wire(monkeypatch,two):
    psbt,utxos,signatures,audit=configure(monkeypatch,two)
    client=Client(signatures)
    signed=asyncio.run(s.sign_original_btc_with_privy_once(
        DID,psbt=psbt,original_utxos=utxos,
        intent_key=INTENT,permission_audit=audit,client=client))
    assert signed["owner_signatures_verified"] is True
    assert signed["verified_input_count"]==(2 if two else 1)
    assert signed["source_transaction_sent"] is False
    assert len(client.calls)==(2 if two else 1)
    for i,(url,kwargs) in enumerate(client.calls):
        assert url.endswith(WALLET+"/raw_sign")
        assert set(kwargs["json"])=={"params"}
        assert kwargs["json"]["params"]["hash"].startswith("0x")
        assert kwargs["headers"]["privy-app-id"]==s.APP_ID
        assert "privy-authorization-signature" in kwargs["headers"]
        assert len(kwargs["headers"]["privy-idempotency-key"])==64
    assert len({x[1]["headers"]["privy-idempotency-key"] for x in client.calls})==len(client.calls)

@pytest.mark.parametrize("field,value",[
    ("exact_remote_policy_verified",False),
    ("user_consent_current",False),
    ("owner_signed_budget_current",False),
    ("backend_quorum_current",False),
    ("wallet_id","wallet_another_00000"),
    ("original_psbt_sha256","f"*64),
    ("budget_revision",0),
])
def test_privy_btc_signing_requires_original_grant_and_current_psbt(monkeypatch,field,value):
    psbt,utxos,sigs,audit=configure(monkeypatch)
    audit[field]=value
    client=Client(sigs)
    with pytest.raises((ValueError,s.HTTPException)):
        asyncio.run(s.sign_original_btc_with_privy_once(
            DID,psbt=psbt,original_utxos=utxos,
            intent_key=INTENT,permission_audit=audit,client=client))
    assert client.calls==[]

def test_privy_btc_wrong_signature_fails_before_dispatch(monkeypatch):
    psbt,utxos,sigs,audit=configure(monkeypatch)
    client=Client(["00"*64])
    with pytest.raises(ValueError):
        asyncio.run(s.sign_original_btc_with_privy_once(
            DID,psbt=psbt,original_utxos=utxos,
            intent_key=INTENT,permission_audit=audit,client=client))
    assert len(client.calls)==1

def test_raw_btc_signature_parser_rejects_malformed_recovery():
    assert s._parse_privy_secp256k1_signature(
        {"method":"raw_sign","data":{"signature":"0x"+"42"*64+"01","encoding":"hex"}})=="42"*64
    for sig in ("0x"+"11"*63,"0x"+"ff"*64+"0a","oops","0x"+"ff"*75):
        with pytest.raises(ValueError):
            s._parse_privy_secp256k1_signature({"method":"raw_sign","data":{"signature":sig,"encoding":"hex"}})

def test_btc_raw_sign_connects_exactly_one_dispatch_after_all_inputs(monkeypatch):
    psbt,utxos,sigs,audit=configure(monkeypatch,two=True)
    client=Client(sigs)
    called=[]
    async def dispatch(did,**kwargs):
        called.append((did,kwargs))
        return {"mode":"ORIGINAL_NATIVE_EXECUTED_ONCE_PENDING_FINALITY",
                "retry_allowed":False}
    monkeypatch.setattr(s,"dispatch_signed_original_native_once",dispatch)
    result=asyncio.run(s.sign_and_dispatch_original_btc_sell_once(
        DID,intent_key=INTENT,quote={"asset":"BTC"},
        psbt=psbt,original_utxos=utxos,fee_rate_sats_per_vbyte=5,
        permission_audit=audit,client=client))
    assert result["retry_allowed"] is False
    assert len(client.calls)==2 and len(called)==1
    assert called[0][1]["asset"]=="BTC"
    assert called[0][1]["original_utxos"] is utxos
    assert called[0][1]["fee_rate_sats_per_vbyte"]==5

def test_no_btc_grant_or_chain_submission_while_signing():
    import inspect
    src=inspect.getsource(s.sign_original_btc_with_privy_once)
    assert "sendrawtransaction" not in src
    assert "broadcast_original_native_signed_once" not in src
    assert "createPolicy(" not in src


def test_privy_canonical_authorizer_accepts_only_exact_btc_raw_hash():
    url=s._ENDPOINT+WALLET+"/raw_sign"
    key="a"*64
    authorized=pinned._canonical_authorization_payload(
        url=url,body={"params":{"hash":"0x"+"c"*64}},key=key)
    assert b"raw_sign" in authorized
    for body in [
        {"method":"raw_sign","params":{"hash":"0x"+"c"*64}},
        {"params":{"hash":"0x"+"c"*63}},
        {"params":{"hash":"0x"+"c"*64,"bytes":"abc"}},
        {"params":{"bytes":"0xff","encoding":"hex"}},
    ]:
        with pytest.raises(ValueError):
            pinned._canonical_authorization_payload(url=url,body=body,key=key)
