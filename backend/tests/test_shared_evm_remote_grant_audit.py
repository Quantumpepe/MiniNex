"""Current remote Privy policies must match pinned owner-approved EVM rules."""
from datetime import datetime,timezone
import hashlib
import json
import inspect

import pytest
from fastapi import HTTPException

from backend.shared_evm_remote_grant_audit import (
    validate_pinned_evm_policy,_read_grant_evidence_locked,
)
from backend.shared_evm_privy_user_policy import build_user_evm_policy_blueprint

DID="did:privy:owner"
WALLET="0x"+"ab"*20
WALLET_ID="wallet_1234567"
OWNER="owner_12345678"
SIGNER="signer_12345678"
POLICY="policy_12345678"
NOW=1800000000
EXP=NOW+900
SHA="a"*64


def pinned(asset):
    stable="USDT" if asset=="BNB" else "USDC"
    candidate=build_user_evm_policy_blueprint(
        asset=asset,stable=stable,wallet=WALLET,owner_id=OWNER,
        executor_id=SIGNER,buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="200000000000000000",
        native_wrap_atomic_cap=("200000000000000000" if
            asset in ("BNB","POL","AVAX") else None),
        expires_at=EXP,now=NOW)
    digest=hashlib.sha256(json.dumps(candidate,
        sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return {
        "asset":asset,"stable":stable,
        "chain_id":{"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1}[asset],
        "owner_id":OWNER,"signer_id":SIGNER,
        "wallet_id":WALLET_ID,"wallet_address":WALLET,
        "candidate_json":candidate,"policy_sha256":digest,
        "remote_policy_sha256":digest,"policy_id":POLICY,
        "consent_sha256":SHA,
        "consent_verified_at":datetime.fromtimestamp(NOW,timezone.utc),
        "grant_verified_at":datetime.fromtimestamp(NOW,timezone.utc),
        "revoked_at":None,"status":"GRANT_VERIFIED","budget_revision":3,
        "expires_at":datetime.fromtimestamp(EXP,timezone.utc),
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_all_five_can_regenerate_the_exact_pinned_allowed_privy_rules(asset):
    p=pinned(asset)
    assert validate_pinned_evm_policy(p,asset=asset,now=NOW)==p["candidate_json"]


@pytest.mark.parametrize("alter",[
    lambda p:p["candidate_json"]["rules"][0]["conditions"].pop(),
    lambda p:p["candidate_json"]["rules"].append(
        {"name":"arbitrary","method":"eth_sendTransaction",
         "action":"ALLOW","conditions":[]}),
    lambda p:p["candidate_json"]["rules"][1]["conditions"][1].update(
        value="0x"+"cd"*20),
    lambda p:p.update(policy_sha256="b"*64),
    lambda p:p.update(wallet_address="0x"+"cd"*20),
    lambda p:p.update(expires_at=datetime.fromtimestamp(NOW+360,timezone.utc)),
])
def test_grant_policy_tampering_or_wrong_wallet_rejected(alter):
    record=pinned("AVAX")
    alter(record)
    with pytest.raises(ValueError):
        validate_pinned_evm_policy(record,asset="AVAX",now=NOW)


def test_expired_policy_cannot_pass_pinned_check():
    with pytest.raises(ValueError):
        validate_pinned_evm_policy(pinned("LINK"),asset="LINK",now=EXP)


class Cur:
    def __init__(self,items):self.items=iter(items);self.calls=[]
    def execute(self,sql,args):self.calls.append((sql,args))
    def fetchone(self):return next(self.items)


def test_signed_intent_must_match_privy_grant_and_budget_revision():
    row=pinned("LINK")
    challenge={
        "status":"SIGNED_INTENT","wallet_id":WALLET_ID,
        "wallet_address":WALLET,"chain_id":1,"stable":"USDC",
        "budget_revision":3,"message_sha256":SHA,
        "confirmed_at":datetime.fromtimestamp(NOW,timezone.utc)}
    cur=Cur([row,challenge])
    assert _read_grant_evidence_locked(cur,did=DID,asset="LINK")==row
    assert len(cur.calls)==2
    assert all("FOR UPDATE" in query for query,args in cur.calls)


@pytest.mark.parametrize("alter",[
    lambda r:r.update(wallet_id="wallet_other"),
    lambda r:r.update(status="PENDING"),
    lambda r:r.update(budget_revision=4),
    lambda r:r.update(message_sha256="b"*64),
    lambda r:r.update(chain_id=56),
])
def test_remote_grant_can_never_bypass_original_owner_signature(alter):
    row=pinned("LINK")
    challenge={
        "status":"SIGNED_INTENT","wallet_id":WALLET_ID,
        "wallet_address":WALLET,"chain_id":1,"stable":"USDC",
        "budget_revision":3,"message_sha256":SHA,
        "confirmed_at":datetime.fromtimestamp(NOW,timezone.utc)}
    alter(challenge)
    cur=Cur([row,challenge])
    with pytest.raises(HTTPException):
        _read_grant_evidence_locked(cur,did=DID,asset="LINK")


def test_remote_audit_endpoint_owner_only_and_never_signs():
    from backend.app import app
    route=next(x for x in app.routes if
        x.path=="/api/live/evm/remote-grant-audit/{asset}")
    source=inspect.getsource(route.endpoint)
    assert route.methods=={"GET"}
    assert "verify_privy_bearer" in source
    assert "get_owner_evm_remote_grant_audit" in source
