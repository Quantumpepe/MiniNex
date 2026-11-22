"""Central EVM owner policy published at most once, never wallet-delegated."""
import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException

from backend import shared_evm_wallet_policy_publish as pub
from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,verified_wallet_policy_digest,
)
from backend.shared_evm_wallet_policy_schema import WALLET_POLICY_SCHEMA
from backend.app import app

NOW=1800000000
DID="did:privy:owner12345"
WALLET="0x"+"a"*40
OWNER="owner_12345678"
SIGNER="signer_12345678"
WID="wallet_12345678"
POLICY_ID="policy_12345678"
OWNED={"address":WALLET,"wallet_id":WID,"delegated":False}


def scope():
    return dict(
        asset="QNT",stable="USDC",treasury="0x"+"b"*40,
        buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=None,
        owner_max_fee_atomic="1000000",
        expires_at=NOW+3600,
    )


def candidate():
    args=dict(
        wallet=WALLET,owner_id=OWNER,
        executor_id=SIGNER,scopes=[scope()],now=NOW,
    )
    policy=compose_scoped_evm_wallet_policy(**args)
    digest=verified_wallet_policy_digest(policy,**args)
    return policy,digest


def row():
    _,digest=candidate()
    return dict(
        wallet_id=WID,wallet_address=WALLET,
        owner_id=OWNER,signer_id=SIGNER,
        policy_id=None,policy_sha256=digest,
        scopes_json={"QNT":scope()},
        revisions_json={"QNT":3},
        legacy_reconciliation_required=False,
        status="DRAFT",
    )


class Cur:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.calls=[]
    def execute(self,sql,params):
        self.calls.append((sql,params))
    def fetchone(self):
        return next(self.rows)


def test_initial_owner_policy_claim_is_durable_and_one_time(monkeypatch):
    monkeypatch.setattr(pub,"_locked_selected_budget",
        lambda cur,**kwargs:(1000,3))
    cur=Cur([row(),{"status":"SUBMITTING"}])
    claimed=pub._claim_first_policy_locked(
        cur,did=DID,owned=OWNED,
        owner_id=OWNER,signer_id=SIGNER,now=NOW,
    )
    _,digest=candidate()
    assert claimed["policy_sha256"]==digest
    assert len(claimed["idempotency_key"])==64
    assert claimed["candidate"]["owner_id"]==OWNER
    assert len(claimed["candidate"]["rules"])==5
    assert claimed["budget_revision"]==3
    assert "FOR UPDATE" in cur.calls[0][0]
    assert "status='SUBMITTING'" in cur.calls[-1][0]
    assert "publication_idempotency_key" in cur.calls[-1][0]
    assert cur.calls[-1][1][0]==claimed["idempotency_key"]


@pytest.mark.parametrize("change",[
    {"status":"SUBMITTING"},
    {"status":"UNCERTAIN"},
    {"status":"GRANT_VERIFIED"},
    {"owner_id":"another_owner_123456"},
    {"signer_id":"another_signer_123456"},
    {"policy_sha256":"0"*64},
    {"legacy_reconciliation_required":True},
    {"policy_id":POLICY_ID},
])
def test_previous_or_changed_owner_claim_is_never_republished(monkeypatch,change):
    monkeypatch.setattr(pub,"_locked_selected_budget",
        lambda cur,**kwargs:(1000,3))
    stored=row();stored.update(change)
    cur=Cur([stored])
    with pytest.raises(HTTPException) as exc:
        pub._claim_first_policy_locked(
            cur,did=DID,owned=OWNED,
            owner_id=OWNER,signer_id=SIGNER,now=NOW,
        )
    assert exc.value.status_code==409
    assert len(cur.calls)==1


def test_exact_remote_create_then_independent_get(monkeypatch):
    policy,digest=candidate()
    claimed={
        "candidate":policy,
        "policy_sha256":digest,
        "idempotency_key":"c"*64,
    }
    remote=deepcopy(policy)
    remote["id"]=POLICY_ID
    remote["rules"]=[dict(x,id=f"rule_{i+1:08d}")
                     for i,x in enumerate(remote["rules"])]
    monkeypatch.setattr(pub,"_auth",lambda:("app_test","secret_test"))
    calls=[]
    class Response:
        content=b"{}"
        def __init__(self,document):self.document=document
        def raise_for_status(self):pass
        def json(self):return self.document
    class Client:
        async def post(self,url,*,json,auth,headers):
            calls.append(("post",url,headers["privy-idempotency-key"]))
            assert auth==("app_test","secret_test")
            assert json==policy
            return Response({"id":POLICY_ID})
        async def get(self,url,*,auth,headers):
            calls.append(("get",url))
            return Response(remote)
    output=asyncio.run(pub._post_exact_initial_policy_once(
        claimed=claimed,client=Client(),
    ))
    assert output["remote_exact_match"] is True
    assert output["policy_id"]==POLICY_ID
    assert len(calls)==2
    assert calls[0][0]=="post"
    assert calls[1][0]=="get"


def test_mismatched_remote_policy_not_trusted(monkeypatch):
    policy,digest=candidate()
    remote=deepcopy(policy)
    remote["id"]=POLICY_ID
    remote["rules"]=[dict(x,id=f"rule_{i+1:08d}")
                     for i,x in enumerate(remote["rules"])]
    remote["rules"][0]["method"]="*"
    monkeypatch.setattr(pub,"_auth",lambda:("app_test","secret_test"))
    class Response:
        content=b"{}"
        def __init__(self,doc):self.doc=doc
        def raise_for_status(self):pass
        def json(self):return self.doc
    class Client:
        async def post(self,*a,**kw):return Response({"id":POLICY_ID})
        async def get(self,*a,**kw):return Response(remote)
    with pytest.raises(ValueError):
        asyncio.run(pub._post_exact_initial_policy_once(
            claimed={"candidate":policy,"idempotency_key":"a"*64},
            client=Client(),
        ))


def test_finish_remote_verified_policy_remains_unattached():
    _,digest=candidate()
    key="c"*64
    claim={
        "idempotency_key":key,"policy_sha256":digest,
        "wallet_id":WID,"signer_id":SIGNER,"asset":"QNT",
    }
    active={
        "status":"SUBMITTING","policy_id":None,
        "policy_sha256":digest,"publication_idempotency_key":key,
        "wallet_id":WID,"signer_id":SIGNER,
    }
    cur=Cur([active,{"status":"REMOTE_VERIFIED_UNATTACHED"}])
    result=pub._finish_first_policy_locked(
        cur,did=DID,claimed=claim,
        remote={"policy_id":POLICY_ID,"policy_sha256":digest,
                "remote_exact_match":True},
    )
    assert result["status"]=="REMOTE_VERIFIED_UNATTACHED"
    assert result["policy_id"]==POLICY_ID
    assert result["wallet_additional_signer_attached"] is False
    assert result["can_submit"] is False


def test_uncertain_post_can_never_claim_wallet_approval():
    _,digest=candidate()
    key="a"*64
    cur=Cur([
        {"status":"SUBMITTING","policy_id":None,
         "policy_sha256":digest,"publication_idempotency_key":key,
         "wallet_id":WID,"signer_id":SIGNER},
        {"status":"UNCERTAIN"},
    ])
    out=pub._finish_first_policy_locked(
        cur,did=DID,claimed={
            "idempotency_key":key,"policy_sha256":digest,
            "wallet_id":WID,"signer_id":SIGNER,
        },remote=None,
    )
    assert out["status"]=="UNCERTAIN"
    assert out["remote_publish_not_retried"] is True
    assert out["can_submit"] is False
    assert "status='UNCERTAIN'" in cur.calls[-1][0]


def test_original_publishing_schema_and_authenticated_route():
    sql="\n".join(WALLET_POLICY_SCHEMA)
    assert "publication_idempotency_key TEXT UNIQUE" in sql
    assert "last_attempt_at TIMESTAMPTZ" in sql
    route=next(r for r in app.routes if
        r.path=="/api/live/evm/wallet-policy/publish")
    assert route.methods=={"POST"}
