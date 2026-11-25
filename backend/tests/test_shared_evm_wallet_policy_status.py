"""Central signer status: original-wallet verification, no grant or trade."""
import asyncio
import time
from copy import deepcopy

import pytest
from fastapi import HTTPException

from backend import shared_evm_wallet_policy_status as mod
from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,verified_wallet_policy_digest,
)
from backend.app import app

DID="did:privy:owner12345"
WALLET_ID="wallet_12345678"
WALLET="0x"+"a"*40
OWNER="owner_12345678"
SIGNER="signer_12345678"
POLICY="policy_12345678"


def setup_policy():
    now=int(time.time())
    scopes=[dict(
        asset="LINK", stable="USDC", treasury="0x"+"b"*40,
        buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=None,
        owner_max_fee_atomic="50000",expires_at=now+3600,
    ),dict(
        asset="QNT", stable="USDC", treasury="0x"+"b"*40,
        buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=None,
        owner_max_fee_atomic="50000",expires_at=now+3600,
    )]
    args=dict(
        wallet=WALLET,owner_id=OWNER,executor_id=SIGNER,
        scopes=scopes,now=now,
    )
    policy=compose_scoped_evm_wallet_policy(**args)
    digest=verified_wallet_policy_digest(policy,**args)
    remote=deepcopy(policy)
    remote["id"]=POLICY
    remote["rules"]=[dict(row,id=f"rule_{i:08d}")
                     for i,row in enumerate(remote["rules"])]
    record={
        "wallet_id":WALLET_ID,"wallet_address":WALLET,
        "owner_id":OWNER,"signer_id":SIGNER,
        "policy_id":POLICY,"policy_sha256":digest,
        "status":"GRANT_VERIFIED",
        "legacy_reconciliation_required":False,
        "scopes_json":{x["asset"]:x for x in scopes},
        "revisions_json":{"LINK":1,"QNT":2},
    }
    user={"id":DID,"linked_accounts":[{
        "type":"wallet","chain_type":"ethereum",
        "wallet_client_type":"privy","id":WALLET_ID,
        "address":WALLET,"delegated":True,
    }]}
    metadata={"id":WALLET_ID,"address":WALLET,
        "entity":{"type":"user","id":DID},
        "chain_type":"ethereum","archived_at":None,
        "additional_signers":[{
            "signer_id":SIGNER,"override_policy_ids":[POLICY],
        }],
    }
    return record,scopes,user,metadata,remote


def wire(monkeypatch,record,scopes,user,metadata,remote,quorum=True,owner_quorum=True):
    monkeypatch.setattr(mod,"_read_saved_and_current_preferences",
        lambda did:(record,scopes))
    monkeypatch.setenv("MININEX_PRIVY_ETH_SIGNER_ID",SIGNER)
    async def fake_user(_):return user
    async def fake_wallet(_):return metadata
    async def fake_policy(_):return remote
    async def fake_quorum(**kwargs):
        enabled=(quorum if kwargs.get("expected_quorum_id")==SIGNER
                 else owner_quorum)
        return {"remote_quorum_public_key_verified":enabled}
    monkeypatch.setattr(mod,"fetch_privy_user",fake_user)
    monkeypatch.setattr(mod,"fetch_privy_wallet_metadata",fake_wallet)
    monkeypatch.setattr(mod,"_fetch_policy",fake_policy)
    monkeypatch.setattr(mod,"check_privy_backend_quorum",fake_quorum)


def test_one_signer_valid_wallet_has_readonly_remote_status(monkeypatch):
    record,scopes,user,metadata,remote=setup_policy()
    wire(monkeypatch,record,scopes,user,metadata,remote)
    out=asyncio.run(mod.read_owner_central_wallet_policy_status(DID))
    assert out["status"]=="REMOTE_VERIFIED_READONLY"
    assert out["scope_coins"]==["LINK","QNT"]
    assert out["one_scoped_signer_verified"] is True
    assert out["remote_policy_exactly_verified"] is True
    assert out["backend_quorum_current"] is True
    assert out["policy_owner_quorum_current"] is True
    assert out["can_submit"] is False
    assert out["auto_execution_enabled"] is False


@pytest.mark.parametrize("change",[
    lambda d:d[0].update(status="DRAFT"),
    lambda d:d[0].update(legacy_reconciliation_required=True),
    lambda d:d[0].update(policy_sha256="a"*64),
    lambda d:d[3]["additional_signers"].append({
        "signer_id":"other_signer_12345","override_policy_ids":[],
    }),
    lambda d:d[3]["additional_signers"][0].update(override_policy_ids=[]),
    lambda d:d[4]["rules"][0].update(method="*"),
    lambda d:d[2]["linked_accounts"][0].update(delegated=False),
])
def test_invalid_or_unmigrated_original_grant_is_not_active(monkeypatch,change):
    payload=list(setup_policy())
    change(payload)
    wire(monkeypatch,*payload)
    out=asyncio.run(mod.read_owner_central_wallet_policy_status(DID))
    assert out["auto_execution_enabled"] is False
    assert out["can_submit"] is False
    assert out["status"]!="REMOTE_VERIFIED_READONLY"


def test_unregistered_key_quorum_denies_verified_status(monkeypatch):
    record,scopes,user,metadata,remote=setup_policy()
    wire(monkeypatch,record,scopes,user,metadata,remote,quorum=False)
    out=asyncio.run(mod.read_owner_central_wallet_policy_status(DID))
    assert out["status"]=="REQUIRES_RECONCILIATION"
    assert out["backend_quorum_current"] is False


def test_policy_owner_key_is_independent_of_backend_signer(monkeypatch):
    record,scopes,user,metadata,remote=setup_policy()
    wire(monkeypatch,record,scopes,user,metadata,remote,
         owner_quorum=False)
    out=asyncio.run(mod.read_owner_central_wallet_policy_status(DID))
    assert out["status"]=="REMOTE_VERIFIED_READONLY"
    assert out["backend_quorum_current"] is True
    assert out["policy_owner_quorum_current"] is False
    assert out["auto_execution_enabled"] is False




def test_policy_owner_quorum_read_error_does_not_mask_valid_existing_grant(monkeypatch):
    record,scopes,user,metadata,remote=setup_policy()
    wire(monkeypatch,record,scopes,user,metadata,remote)
    async def quorum(**kwargs):
        if kwargs.get("expected_quorum_id")==OWNER:
            raise OSError("Policy owner quorum read temporarily unavailable")
        return {"remote_quorum_public_key_verified":True}
    monkeypatch.setattr(mod,"check_privy_backend_quorum",quorum)
    out=asyncio.run(mod.read_owner_central_wallet_policy_status(DID))
    assert out["status"]=="REMOTE_VERIFIED_READONLY"
    assert out["one_scoped_signer_verified"] is True
    assert out["remote_policy_exactly_verified"] is True
    assert out["backend_quorum_current"] is True
    assert out["policy_owner_quorum_current"] is False


def test_original_coin_audit_needs_existing_owner_grant_not_owner_patch_quorum(monkeypatch):
    from backend import shared_evm_central_runtime as runtime
    record,scopes,user,metadata,remote=setup_policy()
    wire(monkeypatch,record,scopes,user,metadata,remote,owner_quorum=False)
    monkeypatch.setattr(runtime,"original_evm_coin_record",
        lambda did,asset: {
            "policy_sha256":record["policy_sha256"],
            "wallet_id":record["wallet_id"],
            "signer_id":record["signer_id"],
            "budget_revision":record["revisions_json"][asset],
            "chain_id":1,
        })
    audit=asyncio.run(runtime.audit_original_evm_coin(DID,"LINK"))
    assert audit["grant_source"]=="PRIVY_CENTRAL_ONE_SIGNER"
    assert audit["backend_quorum_current"] is True
    assert audit["remote_verified_policy_sha256"]==record["policy_sha256"]

def test_missing_policy_never_infers_authorization(monkeypatch):
    monkeypatch.setattr(mod,"_read_saved_and_current_preferences",
        lambda did:(None,[]))
    out=asyncio.run(mod.read_owner_central_wallet_policy_status(DID))
    assert out["status"]=="NOT_CONFIGURED"
    assert out["scope_coins"]==[]
    assert out["can_submit"] is False


def test_database_failure_returns_inert_diagnostic(monkeypatch):
    def fail(_):raise ValueError("stale budget revisions")
    monkeypatch.setattr(mod,"_read_saved_and_current_preferences",fail)
    out=asyncio.run(mod.read_owner_central_wallet_policy_status(DID))
    assert out["status"]=="REQUIRES_RECONCILIATION"
    assert out["one_scoped_signer_verified"] is False


def test_authenticated_status_route_cannot_change_wallet(monkeypatch):
    path="/api/live/evm/wallet-policy/status"
    route=next(x for x in app.routes if x.path==path)
    assert route.methods=={"GET"}
    from backend import app as api
    seen=[]
    monkeypatch.setattr(api,"verify_privy_bearer",
        lambda bearer:{"user_id":DID} if bearer=="Bearer valid" else
        (_ for _ in ()).throw(HTTPException(401,"Sign in required")))
    async def report(did):
        seen.append(did)
        return mod._inert_response()
    monkeypatch.setattr(api,"read_owner_central_wallet_policy_status",report)
    response=asyncio.run(route.endpoint(authorization="Bearer valid"))
    assert response.status_code==200
    assert seen==[DID]
    with pytest.raises(HTTPException) as err:
        asyncio.run(route.endpoint(authorization="Bearer other"))
    assert err.value.status_code==401


def test_stale_coin_does_not_invalidate_other_current_remote_grants(monkeypatch):
    record,scopes,user,metadata,remote=setup_policy()
    record["stale_budget_coins"]=["LINK"]
    wire(monkeypatch,record,scopes,user,metadata,remote)
    out=asyncio.run(mod.read_owner_central_wallet_policy_status(DID))
    assert out["status"]=="REMOTE_VERIFIED_READONLY"
    assert out["current_user_budgets_verified"] is False
    assert out["current_scope_coins"]==["QNT"]
    assert out["renewable_scope_coins"]==["LINK"]
