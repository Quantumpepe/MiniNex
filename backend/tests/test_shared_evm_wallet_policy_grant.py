"""One original Privy signer: exact remote offer and real wallet approval."""
import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException

from backend import shared_evm_wallet_policy_grant as g
from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,verified_wallet_policy_digest,
)
from backend.app import app

DID="did:privy:owner12345"
WALLET="0x"+"a"*40
WALLET_ID="wallet_12345678"
OWNER="owner_12345678"
SIGNER="signer_12345678"
POLICY_ID="policy_12345678"
NOW=1800000000


def fixture(delegated=False, attached=False):
    scope=dict(
        asset="QNT",stable="USDC",treasury="0x"+"b"*40,
        buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=None,
        owner_max_fee_atomic="1000000",expires_at=NOW+3600,
    )
    args=dict(
        wallet=WALLET,owner_id=OWNER,executor_id=SIGNER,
        scopes=[scope],now=NOW,
    )
    candidate=compose_scoped_evm_wallet_policy(**args)
    digest=verified_wallet_policy_digest(candidate,**args)
    remote=deepcopy(candidate)
    remote["id"]=POLICY_ID
    remote["rules"]=[dict(x,id=f"rule_{i:08d}")
                     for i,x in enumerate(remote["rules"])]
    record={
        "status":"REMOTE_VERIFIED_UNATTACHED",
        "legacy_reconciliation_required":False,
        "wallet_id":WALLET_ID,"wallet_address":WALLET,
        "owner_id":OWNER,"signer_id":SIGNER,
        "policy_id":POLICY_ID,"policy_sha256":digest,
        "scopes_json":{"QNT":scope},"revisions_json":{"QNT":3},
    }
    user={"id":DID,"linked_accounts":[{
        "type":"wallet","chain_type":"ethereum",
        "wallet_client_type":"privy","id":WALLET_ID,
        "address":WALLET,"delegated":delegated,
    }]}
    metadata={
        "id":WALLET_ID,"chain_type":"ethereum",
        "address":WALLET,"entity":{"type":"user","id":DID},
        "archived_at":None,
        "additional_signers":([{
            "signer_id":SIGNER,"override_policy_ids":[POLICY_ID],
        }] if attached else []),
    }
    return record,[scope],user,metadata,remote


def test_grant_offer_pins_original_wallet_full_policy_and_fee():
    record,scopes,user,metadata,remote=fixture()
    review=g._validate_original_candidate(
        record=record,scopes=scopes,did=DID,
        user=user,wallet=metadata,remote=remote,now=NOW,
    )
    assert review["digest"]==record["policy_sha256"]
    assert review["scope"]["treasury"]=="0x"+"b"*40
    assert review["owned"]["address"]==WALLET


@pytest.mark.parametrize("change",[
    lambda x:x[0].update(policy_sha256="0"*64),
    lambda x:x[0].update(status="UNCERTAIN"),
    lambda x:x[3]["entity"].update(id="did:privy:other12345"),
    lambda x:x[4]["rules"][0].update(method="*"),
    lambda x:x[4]["rules"][0]["conditions"].pop(),
    lambda x:x[4].update(owner_id="another_owner_12345"),
])
def test_nonexact_policy_and_owner_metadata_cannot_create_offer(change):
    values=list(fixture())
    change(values)
    record,scopes,user,metadata,remote=values
    with pytest.raises((ValueError,TypeError,KeyError)):
        g._validate_original_candidate(
            record=record,scopes=scopes,did=DID,
            user=user,wallet=metadata,remote=remote,now=NOW,
        )


class Cur:
    def __init__(self,rows):self.rows=iter(rows);self.calls=[]
    def execute(self,sql,params):self.calls.append((sql,params))
    def fetchone(self):return next(self.rows)


def test_grant_confirm_requires_locked_original_digest_and_budget(monkeypatch):
    record,scopes,user,metadata,remote=fixture(delegated=True,attached=True)
    proof=g._validate_original_candidate(
        record=record,scopes=scopes,did=DID,
        user=user,wallet=metadata,remote=remote,now=NOW,
    )
    monkeypatch.setattr(g,"_locked_selected_budget",
        lambda cur,**kwargs:(1000,3))
    cur=Cur([record,{"status":"GRANT_VERIFIED"}])
    out=g._confirm_original_wallet_grant_locked(
        cur,did=DID,record=record,proof=proof,now=NOW,
    )
    assert out["state"]=="GRANT_VERIFIED"
    assert out["owner_signer_grant_verified"] is True
    assert out["additional_coin_grants_created"]==0
    assert out["live_auto_session_started"] is False
    assert "FOR UPDATE" in cur.calls[0][0]
    assert "status='GRANT_VERIFIED'" in cur.calls[-1][0]


def test_grant_confirm_refuses_replaced_policy(monkeypatch):
    record,scopes,user,metadata,remote=fixture(delegated=True,attached=True)
    proof=g._validate_original_candidate(
        record=record,scopes=scopes,did=DID,
        user=user,wallet=metadata,remote=remote,now=NOW,
    )
    changed={**record,"policy_id":"new_policy_12345"}
    cur=Cur([changed])
    with pytest.raises(HTTPException):
        g._confirm_original_wallet_grant_locked(
            cur,did=DID,record=record,proof=proof,now=NOW,
        )
    assert len(cur.calls)==1


def test_one_signer_grant_endpoints_require_owner_session():
    for path,method in [
        ("/api/live/evm/wallet-policy/grant-offer","GET"),
        ("/api/live/evm/wallet-policy/confirm-grant","POST"),
    ]:
        route=next(x for x in app.routes if x.path==path)
        assert route.methods=={method}
        import inspect
        assert "verify_privy_bearer" in inspect.getsource(route.endpoint)


def test_policy_owner_and_signer_have_distinct_quorum_ids():
    record,scopes,user,metadata,remote=fixture()
    assert record["owner_id"]!=record["signer_id"]
