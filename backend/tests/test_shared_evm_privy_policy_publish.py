"""Explicit Privy policy publishing is one-shot and never delegates wallet."""
import asyncio
from datetime import datetime,timezone
import hashlib
import inspect
import json

import httpx
import pytest
from fastapi import HTTPException

from backend import shared_evm_privy_policy_publish as m
from backend.shared_evm_privy_user_policy import build_user_evm_policy_blueprint

DID="did:privy:owner"
WALLET="0x"+"ab"*20
OWNED={"wallet_id":"wallet_12345678","address":WALLET}
OWNER="owner_12345678"
SIGNER="signer_12345678"
SHA="a"*64
POLICYID="policy_123456789"
NOW=1800000000


class Cursor:
    def __init__(self,rows):self.rows=iter(rows);self.calls=[]
    def execute(self,q,args):self.calls.append((q,args))
    def fetchone(self):return next(self.rows)


def candidate(asset):
    stable="USDT" if asset=="BNB" else "USDC"
    policy=build_user_evm_policy_blueprint(
        asset=asset,stable=stable,wallet=WALLET,
        owner_id=OWNER,executor_id=SIGNER,
        buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=(
            "1000000000000000000" if asset in ("BNB","POL","AVAX") else None),
        expires_at=NOW+3600,now=NOW)
    sha=hashlib.sha256(json.dumps(
        policy,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return {
        "asset":asset,"stable":stable,
        "chain_id":{"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1}[asset],
        "wallet_id":OWNED["wallet_id"],"wallet_address":WALLET,
        "owner_id":OWNER,"signer_id":SIGNER,
        "status":"DRAFT","revoked_at":None,
        "policy_id":None,"policy_sha256":sha,
        "consent_sha256":SHA,
        "candidate_json":policy,"budget_revision":3,
        "expires_at":datetime.fromtimestamp(NOW+3600,timezone.utc),
    }


def rows(asset):
    p=candidate(asset)
    stable=p["stable"]
    return [
        {"budget_usd":1000,"stable":stable,"revision":3},p,
        {"status":"SIGNED_INTENT","wallet_id":OWNED["wallet_id"],
         "wallet_address":WALLET,"stable":stable,
         "chain_id":p["chain_id"],"budget_revision":3,
         "message_sha256":SHA,
         "confirmed_at":datetime.fromtimestamp(NOW,timezone.utc)},
        {"status":"SUBMITTING"},
    ]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_owner_signed_policy_one_submitting_reservation_per_coin(asset):
    cur=Cursor(rows(asset))
    result=m._claim_policy_publication_locked(
        cur,did=DID,asset=asset,owned=OWNED,
        owner_id=OWNER,signer_id=SIGNER,now=NOW)
    assert result["asset"]==asset
    assert len(result["idempotency_key"])==64
    assert result["policy_sha256"]==candidate(asset)["policy_sha256"]
    assert len(cur.calls)==4
    assert "ON CONFLICT(privy_did,asset) DO NOTHING" in cur.calls[-1][0]


@pytest.mark.parametrize("tamper",[
    lambda r:r[1].update(status="REVOKED"),
    lambda r:r[1].update(revoked_at=datetime.fromtimestamp(NOW,timezone.utc)),
    lambda r:r[1].update(wallet_address="0x"+"cd"*20),
    lambda r:r[1].update(candidate_json={"rules":[]}),
    lambda r:r[2].update(status="PENDING"),
    lambda r:r[2].update(message_sha256="b"*64),
    lambda r:r[0].update(revision=4),
])
def test_unsigned_stale_or_modified_policy_cannot_be_submitted(tamper):
    data=rows("LINK")
    tamper(data)
    cur=Cursor(data)
    with pytest.raises(HTTPException):
        m._claim_policy_publication_locked(
            cur,did=DID,asset="LINK",owned=OWNED,
            owner_id=OWNER,signer_id=SIGNER,now=NOW)
    assert not any("INSERT INTO mininex_live_evm_policy_publications" in q
                   for q,args in cur.calls)


def test_prior_uncertain_policy_never_retries_remote_post():
    data=rows("UNI")
    data[-1]=None
    cur=Cursor(data)
    with pytest.raises(HTTPException) as e:
        m._claim_policy_publication_locked(
            cur,did=DID,asset="UNI",owned=OWNED,
            owner_id=OWNER,signer_id=SIGNER,now=NOW)
    assert e.value.status_code==409


def claimed(asset="LINK"):
    c=candidate(asset)
    return {"asset":asset,"candidate":c["candidate_json"],
            "policy_sha256":c["policy_sha256"],
            "idempotency_key":"b"*64}


def remote_for(asset):
    source=claimed(asset)["candidate"]
    r={**source,"id":POLICYID}
    r["rules"]=[
        {**rule,"id":"rule_12345678"+str(i)}
        for i,rule in enumerate(source["rules"])]
    return r


def test_one_post_and_independent_get_strict_remote_match(monkeypatch):
    monkeypatch.setattr(m,"_auth",lambda:("app_123456","secret123"))
    requests=[]
    def handle(request):
        requests.append((request.method,request.url.path))
        if request.method=="POST":
            assert request.headers["privy-idempotency-key"]=="b"*64
            return httpx.Response(200,json={"id":POLICYID})
        return httpx.Response(200,json=remote_for("LINK"))
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle)) as client:
            return await m._post_policy_once(
                claimed=claimed("LINK"),client=client)
    out=asyncio.run(run())
    assert out["policy_id"]==POLICYID
    assert out["remote_exact_match"] is True
    assert requests==[
        ("POST","/v1/policies"),
        ("GET","/v1/policies/"+POLICYID),
    ]


def test_wrong_remote_extra_permission_never_passes_publish(monkeypatch):
    monkeypatch.setattr(m,"_auth",lambda:("app_123456","secret123"))
    def handle(request):
        if request.method=="POST":
            return httpx.Response(200,json={"id":POLICYID})
        data=remote_for("LINK")
        data["rules"][0]["conditions"].pop()
        return httpx.Response(200,json=data)
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle)) as client:
            return await m._post_policy_once(
                claimed=claimed("LINK"),client=client)
    with pytest.raises(ValueError):
        asyncio.run(run())


def test_uncertain_remote_policy_cannot_grant_wallet_permission():
    c=claimed()
    cur=Cursor([
        {"status":"SUBMITTING","policy_sha256":c["policy_sha256"],
         "idempotency_key":c["idempotency_key"]},
        {"status":"DRAFT","revoked_at":None,
         "policy_sha256":c["policy_sha256"]},
        {"status":"UNCERTAIN"},
    ])
    result=m._finish_policy_publication_locked(
        cur,did=DID,asset="LINK",claimed=c,remote=None)
    assert result["status"]=="UNCERTAIN"
    assert result["can_submit"] is False
    assert len(cur.calls)==3


def test_remote_verified_is_still_unattached():
    c=claimed()
    cur=Cursor([
        {"status":"SUBMITTING","policy_sha256":c["policy_sha256"],
         "idempotency_key":c["idempotency_key"]},
        {"status":"DRAFT","revoked_at":None,
         "policy_sha256":c["policy_sha256"]},
        {"status":"REMOTE_VERIFIED_UNATTACHED"},
        {"status":"CONSENT_VERIFIED"},
    ])
    r=m._finish_policy_publication_locked(
        cur,did=DID,asset="LINK",claimed=c,
        remote={"remote_exact_match":True,
                "policy_sha256":c["policy_sha256"],
                "policy_id":POLICYID})
    assert r["separate_privy_owner_add_signer_required"] is True
    assert r["can_submit"] is False
    assert len(cur.calls)==4


def test_policy_publish_is_explicit_privy_owner_action_not_background_task():
    from backend.app import app
    route=next(r for r in app.routes
        if r.path=="/api/live/evm/policy/{asset}/publish")
    assert route.methods=={"POST"}
    assert "verify_privy_bearer" in inspect.getsource(route.endpoint)
    assert "publish_user_evm_privy_policy" in inspect.getsource(route.endpoint)
    src=inspect.getsource(m)
    assert "addSigners" in src
    assert "post_claimed_privy_evm_rpc" not in src
