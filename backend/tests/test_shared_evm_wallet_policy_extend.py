"""Existing one-signer Privy wallet: coin addition requires ONE original signature."""
from copy import deepcopy

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException

from backend import shared_evm_wallet_policy_extend as ext
from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,verified_wallet_policy_digest,
)
from backend.shared_evm_wallet_scope_transition import plan_wallet_policy_transition
from backend.shared_evm_wallet_policy_schema import WALLET_POLICY_SCHEMA
from backend.app import app

KEY=bytes.fromhex("01"*32)
WALLET=Account.from_key(KEY).address
DID="did:privy:owner12345"
WID="wallet_12345678"
OWNER="owner_12345678"
SIGNER="signer_12345678"
NOW=1800000000
NONCE="d"*64
TREASURY="0x"+"b"*40


def scope(asset="LINK"):
    return dict(
        asset=asset,stable="USDC",treasury=TREASURY,
        buy_source_atomic_cap="5000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=None,
        owner_max_fee_atomic="1000000",expires_at=NOW+3600,
    )


def grant():
    original=[scope()]
    kwargs=dict(
        wallet=WALLET.lower(),owner_id=OWNER,executor_id=SIGNER,
        scopes=original,now=NOW,
    )
    candidate=compose_scoped_evm_wallet_policy(**kwargs)
    digest=verified_wallet_policy_digest(candidate,**kwargs)
    return dict(
        wallet_id=WID,wallet_address=WALLET.lower(),
        owner_id=OWNER,signer_id=SIGNER,
        policy_id="policy_12345678",policy_sha256=digest,
        status="GRANT_VERIFIED",revision=3,
        legacy_reconciliation_required=False,
        original_owner_granted_at=object(),
        scopes_json={"LINK":original[0]},
        revisions_json={"LINK":5},
    )


class Cur:
    def __init__(self,rows):
        self.rows=iter(rows);self.calls=[]
    def execute(self,sql,params):
        self.calls.append((sql,params))
    def fetchone(self):
        return next(self.rows)


def quotes():
    return {
        "chainlink":{"usd":20,"last_updated_at":NOW},
        "quant-network":{"usd":100,"last_updated_at":NOW},
        "usd-coin":{"usd":1,"last_updated_at":NOW},
        "tether":{"usd":1,"last_updated_at":NOW},
    }


def test_stage_second_coin_keeps_first_and_reuses_one_signer(monkeypatch):
    current=grant()
    budget_checks=[]
    def locked(cur,*,did,asset,stable,expected_revision):
        budget_checks.append((asset,stable,expected_revision))
        return 1000,expected_revision
    monkeypatch.setattr(ext,"_locked_selected_budget",locked)
    monkeypatch.setattr(ext,"owner_fee_treasury",lambda **_:TREASURY)
    # Account row, wallet row, no conflicting legacy per-coin signer,
    # existing original wallet row, inserted challenge, attached snapshot.
    cur=Cur([
        {"privy_did":DID},current,None,
        current,{"status":"PENDING"},{"status":"PENDING"},
    ])
    out=ext._stage_wallet_coin_extension_locked(
        cur,did=DID,asset="QNT",stable="USDC",revision=9,
        record=current,scopes=[scope()],snapshot=quotes(),
        now=NOW,nonce=NONCE,
    )
    assert out["status"]=="PENDING"
    assert out["nonce"]==NONCE
    assert out["scope_coins"]==["LINK","QNT"]
    assert out["additional_privy_signer_required"] is False
    assert out["policy_updated_in_privy"] is False
    assert out["signer_grant_changed"] is False
    assert out["one_user_message_signature_required"] is True
    assert out["target_policy_sha256"]!=out["previous_policy_sha256"]
    assert "Shared execution signer ID: "+SIGNER in out["challenge"]
    assert budget_checks==[
        ("LINK","USDC",5),("QNT","USDC",9),
    ]
    assert "FOR UPDATE" in cur.calls[0][0]
    assert "source_grant_revision" in cur.calls[-1][0]
    assert cur.calls[-1][1][0].obj["LINK"]==scope()
    assert cur.calls[-1][1][0].obj["QNT"]["asset"]=="QNT"
    assert cur.calls[-1][1][1].obj=={"LINK":5,"QNT":9}


@pytest.mark.parametrize("update",[
    {"status":"UPDATE_PENDING"},
    {"status":"UNCERTAIN"},
    {"policy_sha256":"f"*64},
    {"signer_id":"other_signer_12345"},
    {"legacy_reconciliation_required":True},
])
def test_unverified_original_wallet_cannot_stage_second_coin(update):
    current=grant()
    current.update(update)
    cur=Cur([{"privy_did":DID},current])
    with pytest.raises(HTTPException) as exc:
        ext._stage_wallet_coin_extension_locked(
            cur,did=DID,asset="QNT",stable="USDC",revision=9,
            record=grant(),scopes=[scope()],snapshot=quotes(),
            now=NOW,nonce=NONCE,
        )
    assert exc.value.status_code==409
    assert len(cur.calls)==2


def signed_fixture():
    g=grant()
    desired=[scope(),scope("QNT")]
    old=compose_scoped_evm_wallet_policy(
        wallet=WALLET.lower(),owner_id=OWNER,executor_id=SIGNER,
        scopes=[scope()],now=NOW,
    )
    plan=plan_wallet_policy_transition(
        did=DID,wallet=WALLET.lower(),wallet_id=WID,
        owner_id=OWNER,executor_id=SIGNER,
        existing_scopes=[scope()],desired_scopes=desired,
        existing_policy=old,now=NOW,nonce=NONCE,
    )
    consent={
        "source_grant_revision":3,
        "previous_policy_sha256":g["policy_sha256"],
        "target_policy_sha256":plan["target_policy_sha256"],
        "challenge_text":plan["intent_challenge"],
        "challenge_sha256":plan["intent_challenge_sha256"],
        "status":"PENDING",
        "expires_at_unix":NOW+300,
        "target_scopes_json":{s["asset"]:s for s in desired},
        "target_revisions_json":{"LINK":5,"QNT":9},
    }
    journal={
        "wallet_id":WID,"wallet_address":WALLET.lower(),
        "previous_policy_sha256":g["policy_sha256"],
        "target_policy_sha256":plan["target_policy_sha256"],
        "challenge_text":plan["intent_challenge"],
        "challenge_sha256":plan["intent_challenge_sha256"],
        "status":"PENDING","expires_at_unix":NOW+300,
    }
    signature="0x"+Account.sign_message(
        encode_defunct(text=plan["intent_challenge"]),private_key=KEY,
    ).signature.hex()
    return g,consent,journal,plan,signature


def test_one_original_wallet_signature_confirms_only_intent(monkeypatch):
    g,consent,journal,plan,signature=signed_fixture()
    monkeypatch.setattr(ext,"_locked_selected_budget",
        lambda cur,**kwargs:(1000,kwargs["expected_revision"]))
    cur=Cur([g,consent,g,journal,{"status":"SIGNED_INTENT"}])
    out=ext._confirm_wallet_coin_extension_locked(
        cur,did=DID,nonce=NONCE,signature=signature,now=NOW+50,
    )
    assert out["status"]=="SIGNED_INTENT"
    assert out["wallet_signature_verified"] is True
    assert out["policy_updated_in_privy"] is False
    assert out["signer_grant_changed"] is False
    assert len(cur.calls)==5
    assert "status='PENDING'" in cur.calls[-1][0]


@pytest.mark.parametrize("fault",[
    lambda c:c.update(target_policy_sha256="f"*64),
    lambda c:c["target_revisions_json"].update(QNT=10),
    lambda c:c["target_scopes_json"]["LINK"].update(stable="USDT"),
    lambda c:c.update(status="SIGNED_INTENT"),
    lambda c:c.update(expires_at_unix=NOW+1),
    lambda c:c.update(source_grant_revision=7),
])
def test_wrong_target_expired_or_replayed_intent_rejected(monkeypatch,fault):
    g,consent,_,_,signature=signed_fixture()
    fault(consent)
    def verified_budget(cur,*,asset,expected_revision,**_):
        correct={"LINK":5,"QNT":9}.get(asset)
        if expected_revision!=correct:
            raise HTTPException(409,"Original coin revision changed")
        return 1000,expected_revision
    monkeypatch.setattr(ext,"_locked_selected_budget",verified_budget)
    cur=Cur([g,consent])
    with pytest.raises(HTTPException) as exc:
        ext._confirm_wallet_coin_extension_locked(
            cur,did=DID,nonce=NONCE,signature=signature,now=NOW+50,
        )
    assert exc.value.status_code==409
    assert len(cur.calls)<=4


def test_other_wallet_signature_cannot_confirm_extension(monkeypatch):
    g,consent,journal,_,_=signed_fixture()
    wrong_sig="0x"+Account.sign_message(
        encode_defunct(text=consent["challenge_text"]),
        private_key=bytes.fromhex("02"*32),
    ).signature.hex()
    monkeypatch.setattr(ext,"_locked_selected_budget",
        lambda cur,**kwargs:(1000,kwargs["expected_revision"]))
    cur=Cur([g,consent,g,journal])
    with pytest.raises(HTTPException) as exc:
        ext._confirm_wallet_coin_extension_locked(
            cur,did=DID,nonce=NONCE,signature=wrong_sig,now=NOW+50,
        )
    assert exc.value.status_code==403
    assert len(cur.calls)==4


def test_new_intent_columns_and_routes_are_owner_authenticated():
    sql="\n".join(WALLET_POLICY_SCHEMA)
    for fragment in (
        "target_scopes_json JSONB NOT NULL",
        "target_revisions_json JSONB NOT NULL",
        "source_grant_revision BIGINT",
        "WHERE status IN ('PENDING','SIGNED_INTENT')",
    ):
        assert fragment in sql
    for path,method in [
        ("/api/live/evm/wallet-policy/extend/{asset}/prepare","POST"),
        ("/api/live/evm/wallet-policy/extend/confirm","POST"),
    ]:
        route=next(r for r in app.routes if r.path==path)
        assert route.methods=={method}
        import inspect
        assert "verify_privy_bearer" in inspect.getsource(route.endpoint)
