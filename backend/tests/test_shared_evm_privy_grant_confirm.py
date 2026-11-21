"""Confirm remote Privy signer *after* independent owner action, never add it.

A verified remote policy alone cannot authorize a user wallet; each of the
five assets must have original EIP191 consent, current settings, fresh remote
policy and exact scoped signer + backend quorum before GRANT_VERIFIED.
"""
from datetime import datetime,timezone,timedelta
import hashlib
import inspect
import json

import pytest
from fastapi import HTTPException

from backend.shared_evm_privy_grant_confirm import (
    _load_owner_grant_candidate_locked,
    _prove_remote_owner_grant,
    _commit_owner_grant_locked,
)
from backend.shared_evm_privy_user_policy import build_user_evm_policy_blueprint

DID="did:privy:owner12345"
WALLET="0x"+"ab"*20
WALLET_ID="wallet_12345678"
OWNER="owner_12345678"
SIGNER="signer_12345678"
POLICY_ID="policy_12345678"
SHA="a"*64
NOW=1800000000
CHAIN={"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1}


def record(asset):
    stable="USDT" if asset=="BNB" else "USDC"
    blueprint=build_user_evm_policy_blueprint(
        asset=asset,stable=stable,wallet=WALLET,
        owner_id=OWNER,executor_id=SIGNER,
        buy_source_atomic_cap="1000000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=(
            "1000000000000000000" if asset in ("BNB","POL","AVAX") else None),
        expires_at=NOW+900,now=NOW)
    digest=hashlib.sha256(json.dumps(
        blueprint,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return {
        "asset":asset,"stable":stable,"chain_id":CHAIN[asset],
        "wallet_id":WALLET_ID,"wallet_address":WALLET,
        "owner_id":OWNER,"signer_id":SIGNER,"policy_id":POLICY_ID,
        "policy_sha256":digest,"remote_policy_sha256":digest,
        "consent_sha256":SHA,
        "consent_verified_at":datetime.fromtimestamp(NOW,timezone.utc),
        "revoked_at":None,
        "expires_at":datetime.fromtimestamp(NOW+900,timezone.utc),
        "status":"CONSENT_VERIFIED","budget_revision":3,
        "candidate_json":blueprint,
    }


def consent(asset):
    r=record(asset)
    return {
        "status":"SIGNED_INTENT","wallet_id":WALLET_ID,
        "wallet_address":WALLET,"chain_id":CHAIN[asset],
        "stable":r["stable"],"budget_revision":3,
        "message_sha256":SHA,
        "confirmed_at":datetime.fromtimestamp(NOW,timezone.utc),
    }


class Cursor:
    def __init__(self,rows):
        self.items=iter(rows)
        self.queries=[]
    def execute(self,q,args):
        self.queries.append((q,args))
    def fetchone(self):
        return next(self.items)


def rows(asset):
    r=record(asset)
    return [r,{
        "status":"REMOTE_VERIFIED_UNATTACHED",
        "policy_id":POLICY_ID,"policy_sha256":r["policy_sha256"],
    },consent(asset),{
        "stable":r["stable"],"revision":3,
    }]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_full_signed_candidate_requires_publication_and_current_budget(asset):
    cur=Cursor(rows(asset))
    r=_load_owner_grant_candidate_locked(
        cur,did=DID,asset=asset,now=NOW)
    assert r["asset"]==asset
    assert len(cur.queries)==4
    assert all("FOR UPDATE" in query for query,args in cur.queries)


@pytest.mark.parametrize("alter",[
    lambda rr:rr[0].update(status="DRAFT"),
    lambda rr:rr[0].update(revoked_at=datetime.fromtimestamp(NOW,timezone.utc)),
    lambda rr:rr[1].update(status="UNCERTAIN"),
    lambda rr:rr[1].update(policy_sha256="b"*64),
    lambda rr:rr[2].update(wallet_address="0x"+"cd"*20),
    lambda rr:rr[2].update(message_sha256="b"*64),
    lambda rr:rr[2].update(status="PENDING"),
    lambda rr:rr[3].update(revision=4),
    lambda rr:rr[3].update(stable="USDT"),
])
def test_no_grant_without_fresh_unmodified_signed_owner_approval(alter):
    dataset=rows("AVAX")
    alter(dataset)
    cur=Cursor(dataset)
    with pytest.raises(HTTPException) as err:
        _load_owner_grant_candidate_locked(
            cur,did=DID,asset="AVAX",now=NOW)
    assert err.value.status_code==409


def user(delegated=True):
    return {"id":DID,"linked_accounts":[{
        "type":"wallet","chain_type":"ethereum",
        "wallet_client_type":"privy",
        "id":WALLET_ID,"address":WALLET,"delegated":delegated,
    }]}


def metadata():
    return {"id":WALLET_ID,"address":WALLET,
        "chain_type":"ethereum","entity":{"type":"user","id":DID},
        "additional_signers":[{
            "signer_id":SIGNER,"override_policy_ids":[POLICY_ID],
        }]}


def remote(asset):
    draft=record(asset)["candidate_json"]
    result={**draft,"id":POLICY_ID}
    result["rules"]=[
        {**rule,"id":"rule_12345678"+str(i)}
        for i,rule in enumerate(draft["rules"])]
    return result


def prove(asset,*,u=None,w=None,r=None,q=None):
    candidate=record(asset)
    return _prove_remote_owner_grant(
        did=DID,record=candidate,expected=candidate["candidate_json"],
        user=user() if u is None else u,
        wallet_metadata=metadata() if w is None else w,
        remote_policy=remote(asset) if r is None else r,
        quorum={"remote_quorum_public_key_verified":True} if q is None else q,
    )


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_five_coins_require_privy_owner_signer_with_exact_policy(asset):
    proof=prove(asset)
    assert proof["verified"] is True
    assert proof["asset"]==asset
    assert proof["policy_id"]==POLICY_ID
    assert proof["budget_revision"]==3


@pytest.mark.parametrize("tamper",[
    lambda x:x["additional_signers"][0].update(override_policy_ids=[]),
    lambda x:x["additional_signers"][0].update(
        override_policy_ids=[POLICY_ID,"unrestricted_123456"]),
    lambda x:x["additional_signers"][0].update(signer_id="other_123456"),
    lambda x:x["entity"].update(id="did:privy:another"),
    lambda x:x.update(address="0x"+"cd"*20),
])
def test_unsafe_grant_scope_refused(tamper):
    w=metadata()
    tamper(w)
    with pytest.raises(HTTPException) as e:
        prove("LINK",w=w)
    assert e.value.status_code==409


def test_remote_policy_broadening_and_missing_backend_quorum_refused():
    r=remote("UNI")
    r["rules"][0]["conditions"].pop()
    with pytest.raises(HTTPException):
        prove("UNI",r=r)
    with pytest.raises(HTTPException):
        prove("UNI",q={"remote_quorum_public_key_verified":False})
    with pytest.raises(HTTPException):
        prove("UNI",u=user(delegated=False))


def test_commit_rechecks_every_record_after_remote_network_wait():
    rr=rows("BNB")
    cur=Cursor([*rr,{"status":"GRANT_VERIFIED"}])
    proof=prove("BNB")
    result=_commit_owner_grant_locked(
        cur,did=DID,asset="BNB",proof=proof,now=NOW)
    assert result["state"]=="GRANT_VERIFIED"
    assert result["trading_session_started"] is False
    assert len(cur.queries)==5
    assert "WHERE privy_did=%s AND asset=%s" in cur.queries[-1][0]


def test_commit_rejects_changed_wallet_after_network_readback():
    rr=rows("BNB")
    rr[0]["wallet_address"]="0x"+"cd"*20
    cur=Cursor(rr)
    with pytest.raises(HTTPException):
        _commit_owner_grant_locked(
            cur,did=DID,asset="BNB",proof=prove("BNB"),now=NOW)


def test_endpoint_is_owner_authenticated_and_never_grants_signer():
    from backend.app import app
    endpoint=next(r for r in app.routes
        if r.path=="/api/live/evm/policy/{asset}/confirm-grant")
    assert endpoint.methods=={"POST"}
    src=inspect.getsource(endpoint.endpoint)
    assert "verify_privy_bearer" in src
    assert "confirm_owner_evm_privy_grant" in src
    from backend import shared_evm_privy_grant_confirm as mod
    source=inspect.getsource(mod)
    assert "client.post(" not in source
    assert ".addSigners(" not in source
    assert "post_claimed_privy_evm_rpc" not in source
