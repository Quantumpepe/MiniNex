"""One-use original wallet signature journal for the central Privy signer."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException

from backend.storage import SCHEMA
from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,verified_wallet_policy_digest,
)
from backend.shared_evm_wallet_scope_transition import (
    plan_wallet_policy_transition,
)
from backend.shared_evm_wallet_policy_intent_ledger import (
    begin_locked_user_policy_update_intent,
    confirm_locked_user_policy_update_intent,
)

DID="did:privy:owner12345"
PRIVY_WALLET_ID="wallet_12345678"
OWNER_ID="owner_12345678"
SIGNER_ID="signer_12345678"
KEY=bytes.fromhex("01"*32)
WALLET=Account.from_key(KEY).address
NOW=1800000000
NONCE="b"*64


def scope(asset):
    return dict(
        asset=asset,stable="USDC",treasury="0x"+"ab"*20,
        buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=None,owner_max_fee_atomic="50000",
        expires_at=NOW+3600,
    )


def plan():
    original=[scope("LINK")]
    desired=[*original,scope("QNT")]
    data=dict(
        wallet=WALLET,owner_id=OWNER_ID,executor_id=SIGNER_ID,
        scopes=original,now=NOW,
    )
    policy=compose_scoped_evm_wallet_policy(**data)
    return plan_wallet_policy_transition(
        did=DID,wallet=WALLET,wallet_id=PRIVY_WALLET_ID,
        owner_id=OWNER_ID,executor_id=SIGNER_ID,
        existing_scopes=original,desired_scopes=desired,
        existing_policy=policy,now=NOW,nonce=NONCE,
    )


def grant(p):
    return dict(
        wallet_id=PRIVY_WALLET_ID,wallet_address=WALLET.lower(),
        owner_id=OWNER_ID,signer_id=SIGNER_ID,
        policy_id="privy_policy_12345678",
        policy_sha256=p["previous_policy_sha256"],
        status="GRANT_VERIFIED",legacy_reconciliation_required=False,
        original_owner_granted_at=datetime.fromtimestamp(NOW-300,timezone.utc),
        revision=1,
    )


def db_intent(p):
    return dict(
        wallet_id=PRIVY_WALLET_ID,wallet_address=WALLET.lower(),
        previous_policy_sha256=p["previous_policy_sha256"],
        target_policy_sha256=p["target_policy_sha256"],
        challenge_text=p["intent_challenge"],
        challenge_sha256=p["intent_challenge_sha256"],
        status="PENDING",expires_at_unix=NOW+300,
    )


class Cursor:
    def __init__(self, rows):
        self.rows=iter(rows)
        self.calls=[]
    def execute(self,sql,params):
        self.calls.append((sql,params))
    def fetchone(self):
        return next(self.rows)


def sign(p):
    return "0x"+Account.sign_message(
        encode_defunct(text=p["intent_challenge"]),private_key=KEY,
    ).signature.hex()


def test_locked_intent_start_is_a_non_authorizing_db_claim_only():
    p=plan()
    cur=Cursor([grant(p),{"status":"PENDING"}])
    r=begin_locked_user_policy_update_intent(cur,did=DID,plan=p,now=NOW)
    assert r["status"]=="PENDING"
    assert r["policy_updated_in_privy"] is False
    assert r["signer_grant_changed"] is False
    assert r["challenge"]==p["intent_challenge"]
    assert len(cur.calls)==2
    assert "FOR UPDATE" in cur.calls[0][0]
    assert "ON CONFLICT(privy_did,nonce) DO NOTHING" in cur.calls[1][0]
    assert cur.calls[1][1][0]==DID
    assert cur.calls[1][1][1]==NONCE
    assert cur.calls[1][1][4]==p["previous_policy_sha256"]
    assert cur.calls[1][1][5]==p["target_policy_sha256"]


def test_valid_eip191_signature_is_persisted_once_without_updating_privy():
    p=plan()
    cur=Cursor([grant(p),db_intent(p),{"status":"SIGNED_INTENT"}])
    r=confirm_locked_user_policy_update_intent(
        cur,did=DID,plan=p,signature=sign(p),now=NOW,
    )
    assert r["status"]=="SIGNED_INTENT"
    assert r["wallet_signature_verified"] is True
    assert r["policy_updated_in_privy"] is False
    assert r["signer_grant_changed"] is False
    assert len(cur.calls)==3
    assert "FOR UPDATE" in cur.calls[0][0]
    assert "FOR UPDATE" in cur.calls[1][0]
    assert "status='PENDING'" in cur.calls[2][0]


def test_same_nonce_cannot_be_staged_twice():
    p=plan()
    with pytest.raises(HTTPException) as e:
        begin_locked_user_policy_update_intent(
            Cursor([grant(p),None]),did=DID,plan=p,now=NOW,
        )
    assert e.value.status_code==409


def test_signed_or_consumed_nonce_cannot_be_signed_again():
    p=plan()
    for state in ("SIGNED_INTENT","CONSUMED","REVOKED"):
        existing=db_intent(p)
        existing["status"]=state
        cur=Cursor([grant(p),existing])
        with pytest.raises(HTTPException) as e:
            confirm_locked_user_policy_update_intent(
                cur,did=DID,plan=p,signature=sign(p),now=NOW,
            )
        assert e.value.status_code==409
        assert len(cur.calls)==2


@pytest.mark.parametrize("field,value",[
    ("status","DRAFT"),
    ("legacy_reconciliation_required",True),
    ("original_owner_granted_at",None),
    ("policy_id",None),
    ("signer_id","another_backend_signer"),
    ("policy_sha256","a"*64),
])
def test_missing_remote_owner_grant_and_changed_signer_are_rejected(field,value):
    p=plan()
    row=grant(p);row[field]=value
    cur=Cursor([row])
    with pytest.raises(HTTPException) as e:
        begin_locked_user_policy_update_intent(cur,did=DID,plan=p,now=NOW)
    assert e.value.status_code==409
    assert len(cur.calls)==1


def test_wrong_signature_cannot_change_consented_policy():
    p=plan()
    cur=Cursor([grant(p),db_intent(p)])
    wrong_sig="0x"+Account.sign_message(
        encode_defunct(text=p["intent_challenge"]),
        private_key=bytes.fromhex("02"*32),
    ).signature.hex()
    with pytest.raises(HTTPException) as e:
        confirm_locked_user_policy_update_intent(
            cur,did=DID,plan=p,signature=wrong_sig,now=NOW,
        )
    assert e.value.status_code==403
    assert len(cur.calls)==2


@pytest.mark.parametrize("mutation",[
    lambda r:r.update(target_policy_sha256="e"*64),
    lambda r:r.update(wallet_address="0x"+"f"*40),
    lambda r:r.update(challenge_text="unsafe grant"),
    lambda r:r.update(status="SIGNED_INTENT"),
    lambda r:r.update(expires_at_unix=NOW),
])
def test_replaced_db_challenge_or_expired_record_is_not_consent(mutation):
    p=plan()
    row=db_intent(p)
    mutation(row)
    cur=Cursor([grant(p),row])
    with pytest.raises(HTTPException) as e:
        confirm_locked_user_policy_update_intent(
            cur,did=DID,plan=p,signature=sign(p),now=NOW,
        )
    assert e.value.status_code==409
    assert len(cur.calls)==2


def test_scope_intent_journals_have_nonces_and_unique_unspent_request():
    all_sql="\n".join(SCHEMA)
    for token in (
        "CREATE TABLE IF NOT EXISTS mininex_live_evm_wallet_policy_grants",
        "legacy_reconciliation_required BOOLEAN NOT NULL DEFAULT TRUE",
        "CREATE TABLE IF NOT EXISTS mininex_live_evm_wallet_policy_intents",
        "PRIMARY KEY(privy_did,nonce)",
        "status IN ('PENDING','SIGNED_INTENT')",
        "expires_at <= issued_at + interval '5 minutes'",
        "owner_id <> signer_id",
    ):
        assert token in all_sql
