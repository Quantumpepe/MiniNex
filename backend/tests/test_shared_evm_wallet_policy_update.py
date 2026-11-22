"""Second EVM coin updates the original policy ONCE without new addSigners."""
import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException

from backend import shared_evm_wallet_policy_update as m
from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,verified_wallet_policy_digest,
)
from backend.shared_evm_wallet_policy_schema import WALLET_POLICY_SCHEMA
from backend.app import app

DID="did:privy:owner12345"
WALLET="0x"+"a"*40
WID="wallet_12345678"
OWNER="owner_12345678"
SIGNER="signer_12345678"
POLICY_ID="policy_12345678"
NONCE="c"*64
NOW=1800000000


def scope(asset):
    return dict(
        asset=asset,stable="USDC",treasury="0x"+"b"*40,
        buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=None,
        owner_max_fee_atomic="50000",expires_at=NOW+3600,
    )


def build(scopes):
    return compose_scoped_evm_wallet_policy(
        wallet=WALLET,owner_id=OWNER,executor_id=SIGNER,
        scopes=scopes,now=NOW,
    )


def digest(scopes):
    args=dict(
        wallet=WALLET,owner_id=OWNER,executor_id=SIGNER,
        scopes=scopes,now=NOW,
    )
    c=compose_scoped_evm_wallet_policy(**args)
    return verified_wallet_policy_digest(c,**args)


def source():
    previous=[scope("LINK")]
    target=[*previous,scope("QNT")]
    g=dict(
        wallet_id=WID,wallet_address=WALLET,
        owner_id=OWNER,signer_id=SIGNER,policy_id=POLICY_ID,
        policy_sha256=digest(previous),status="GRANT_VERIFIED",
        revision=2,legacy_reconciliation_required=False,
        scopes_json={"LINK":previous[0]},
        revisions_json={"LINK":3},
    )
    intent=dict(
        status="SIGNED_INTENT",signed_at=object(),
        update_idempotency_key=None,source_grant_revision=2,
        previous_policy_sha256=g["policy_sha256"],
        target_policy_sha256=digest(target),
        target_scopes_json={r["asset"]:r for r in target},
        target_revisions_json={"LINK":3,"QNT":4},
        expires_at_unix=NOW+300,
    )
    return g,intent


class Cur:
    def __init__(self,rows):
        self.rows=iter(rows);self.calls=[]
    def execute(self,sql,params):
        self.calls.append((sql,params))
    def fetchone(self):
        return next(self.rows)


def budget_stub(monkeypatch):
    def verified(cur,*,asset,expected_revision,**_):
        if expected_revision!={"LINK":3,"QNT":4}[asset]:
            raise HTTPException(409,"Stale user budget")
        return 1000,expected_revision
    monkeypatch.setattr(m,"_locked_selected_budget",verified)


def test_one_durable_claim_reuses_original_policy_and_signer(monkeypatch):
    budget_stub(monkeypatch)
    g,intent=source()
    cur=Cur([
        g,intent,{"status":"PATCH_SUBMITTING"},{"status":"UPDATING"},
    ])
    claim=m._claim_locked(
        cur,did=DID,nonce=NONCE,observed=g,
        old_scopes=[scope("LINK")],now=NOW,
    )
    assert claim["policy_id"]==POLICY_ID
    assert claim["signer_id"]==SIGNER
    assert claim["new_coin"]=="QNT"
    assert set(claim["target_scopes_json"])=={"LINK","QNT"}
    assert claim["target_digest"]==intent["target_policy_sha256"]
    assert len(claim["idempotency_key"])==64
    assert claim["old_digest"]!=claim["target_digest"]
    assert len(cur.calls)==4
    assert "FOR UPDATE" in cur.calls[0][0]
    assert "FOR UPDATE" in cur.calls[1][0]
    assert "status='PATCH_SUBMITTING'" in cur.calls[2][0]
    assert "status='UPDATING'" in cur.calls[3][0]
    assert all("additional_signer" not in sql for sql,_ in cur.calls)


@pytest.mark.parametrize("mutation",[
    lambda g,i:g.update(status="UPDATING"),
    lambda g,i:g.update(signer_id="other_signer_12345"),
    lambda g,i:i.update(status="PATCH_SUBMITTING"),
    lambda g,i:i.update(signed_at=None),
    lambda g,i:i.update(update_idempotency_key="f"*64),
    lambda g,i:i.update(previous_policy_sha256="f"*64),
    lambda g,i:i.update(target_policy_sha256="f"*64),
    lambda g,i:i.update(expires_at_unix=NOW),
    lambda g,i:i["target_scopes_json"].pop("LINK"),
    lambda g,i:i["target_scopes_json"]["LINK"].update(stable="USDT"),
    lambda g,i:i["target_revisions_json"].update(QNT=7),
])
def test_invalid_or_reused_user_scope_cannot_trigger_privy_patch(
    monkeypatch,mutation,
):
    budget_stub(monkeypatch)
    g,i=source()
    observed=deepcopy(g)
    mutation(g,i)
    cur=Cur([g,i])
    with pytest.raises(HTTPException) as exc:
        m._claim_locked(
            cur,did=DID,nonce=NONCE,observed=observed,
            old_scopes=[scope("LINK")],now=NOW,
        )
    assert exc.value.status_code==409
    assert not any("status='PATCH_SUBMITTING'" in sql for sql,_ in cur.calls)


def test_owner_signed_remote_patch_is_one_call_plus_exact_get(monkeypatch):
    g,i=source()
    claim=dict(
        did=DID,policy_id=POLICY_ID,signer_id=SIGNER,wallet_id=WID,
        wallet_address=WALLET,idempotency_key="d"*64,
        target_digest=i["target_policy_sha256"],
        candidate=build([scope("LINK"),scope("QNT")]),
    )
    remote=deepcopy(claim["candidate"])
    remote["id"]=POLICY_ID
    remote["rules"]=[dict(rule,id="rule_"+str(index).zfill(8))
                     for index,rule in enumerate(remote["rules"])]
    checks=[]
    def sign(**kwargs):
        assert kwargs["owner_quorum_verified"] is True
        assert kwargs["candidate"]==claim["candidate"]
        checks.append("owner-signed")
        return {
            "url":m._BASE+POLICY_ID,
            "body":{"name":claim["candidate"]["name"],
                    "rules":claim["candidate"]["rules"]},
            "headers":{"privy-authorization-signature":"signed-by-owner"},
        }
    monkeypatch.setattr(m,"sign_exact_privy_owner_patch",sign)
    monkeypatch.setattr(m,"_auth",lambda:("app_test","secret_test"))
    async def fake_user(_):
        return {"id":DID}
    async def fake_wallet(_):
        return {"additional_signers":[{
            "signer_id":SIGNER,"override_policy_ids":[POLICY_ID],
        }]}
    monkeypatch.setattr(m,"fetch_privy_user",fake_user)
    monkeypatch.setattr(m,"fetch_privy_wallet_metadata",fake_wallet)
    monkeypatch.setattr(m,"_owned_eth_wallet",lambda user,did:{
        "address":WALLET,"wallet_id":WID,"delegated":True,
    })
    monkeypatch.setattr(m,"_scoped_signer",lambda *a,**kw:True)
    class Reply:
        content=b"{}"
        def __init__(self,body):self.body=body
        def raise_for_status(self):pass
        def json(self):return self.body
    class Client:
        async def patch(self,url,*,json,headers,auth):
            checks.append("PATCH")
            assert headers["privy-authorization-signature"]=="signed-by-owner"
            assert auth==("app_test","secret_test")
            return Reply({})
        async def get(self,url,*,headers,auth):
            checks.append("GET")
            return Reply(remote)
    result=asyncio.run(m._patch_once(claim=claim,client=Client()))
    assert checks==["owner-signed","PATCH","GET"]
    assert result["policy_id"]==POLICY_ID
    assert result["digest"]==claim["target_digest"]
    assert result["one_signer_verified"] is True


def claim_for_finish():
    g,i=source()
    claim=dict(
        did=DID,nonce=NONCE,policy_id=POLICY_ID,wallet_id=WID,
        signer_id=SIGNER,old_digest=g["policy_sha256"],
        old_revision=2,target_digest=i["target_policy_sha256"],
        target_scopes_json=i["target_scopes_json"],
        target_revisions_json=i["target_revisions_json"],
        idempotency_key="c"*64,new_coin="QNT",
    )
    stored_grant=dict(
        wallet_id=WID,signer_id=SIGNER,policy_id=POLICY_ID,
        policy_sha256=g["policy_sha256"],status="UPDATING",revision=2,
    )
    stored_intent=dict(
        status="PATCH_SUBMITTING",source_grant_revision=2,
        previous_policy_sha256=g["policy_sha256"],
        target_policy_sha256=i["target_policy_sha256"],
        target_scopes_json=i["target_scopes_json"],
        target_revisions_json=i["target_revisions_json"],
        update_idempotency_key="c"*64,
    )
    proof=dict(
        one_signer_verified=True,remote_rules_verified=True,
        policy_id=POLICY_ID,digest=i["target_policy_sha256"],
    )
    return claim,stored_grant,stored_intent,proof


def test_exact_remote_proof_finalizes_with_original_signer_and_all_budgets(
    monkeypatch,
):
    budget_stub(monkeypatch)
    claim,g,i,proof=claim_for_finish()
    cur=Cur([g,i,{"status":"GRANT_VERIFIED"},{"status":"PATCH_VERIFIED"}])
    out=m._finish_locked(cur,did=DID,claim=claim,remote=proof)
    assert out["status"]=="GRANT_VERIFIED"
    assert out["scope_coins"]==["LINK","QNT"]
    assert out["additional_signer_created"] is False
    assert out["new_policy_created"] is False
    assert out["can_submit"] is False
    assert cur.calls[2][1][1].obj["LINK"]==scope("LINK")
    assert cur.calls[2][1][1].obj["QNT"]==scope("QNT")
    assert "status='PATCH_VERIFIED'" in cur.calls[-1][0]


@pytest.mark.parametrize("bad",[
    None,{},{"policy_id":"different","digest":"0"*64},
])
def test_uncertain_privy_network_never_automatically_retries(monkeypatch,bad):
    budget_stub(monkeypatch)
    claim,g,i,_=claim_for_finish()
    cur=Cur([g,i,{"status":"PATCH_UNCERTAIN"},{"status":"UPDATE_UNCERTAIN"}])
    out=m._finish_locked(cur,did=DID,claim=claim,remote=bad)
    assert out["status"]=="UPDATE_UNCERTAIN"
    assert out["retry_forbidden"] is True
    assert out["can_submit"] is False
    assert "status='PATCH_UNCERTAIN'" in cur.calls[2][0]
    assert "status='UPDATE_UNCERTAIN'" in cur.calls[3][0]


def test_schema_and_endpoints_hold_duplicate_or_uncertain_authorizations():
    sql="\n".join(WALLET_POLICY_SCHEMA)
    assert "PATCH_SUBMITTING" in sql
    assert "PATCH_UNCERTAIN" in sql
    assert "PATCH_VERIFIED" in sql
    assert "mininex_live_evm_wallet_policy_inflight_once" in sql
    route=next(r for r in app.routes if
        r.path=="/api/live/evm/wallet-policy/extend/apply")
    assert route.methods=={"POST"}
    import inspect
    assert "verify_privy_bearer" in inspect.getsource(route.endpoint)
