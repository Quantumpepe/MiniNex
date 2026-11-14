"""Stage 23+24: Privy one-shot creation and exact owner signer verification.

All tests are offline. No actual Privy policy POST, user delegation or trade.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_privy_policy_provisioner as provision
from backend import eth_privy_user_grant as grant
from backend import eth_privy_policy_transport as transport
from backend.eth_privy_user_policy_blueprint import build_eth_privy_user_policy_blueprint

DID = "did:privy:stage23-24-user"
WALLET="0x"+"a"*40
OTHER="0x"+"b"*40
OWNER="policy_owner_q_2324"
SIGNER="executor_q_2324"
POLICY_ID="policy_scope_user2324"
NOW=1900000000
CAPS = {
    "usdc_max_input_micro":"5000000000",
    "usdt_max_input_micro":"5000000000",
    "weth_max_input_wei":"1666666666666666666",
    "native_wrap_max_wei":"1666666666666666666",
}


def candidate():
    return build_eth_privy_user_policy_blueprint(
        wallet=WALLET, policy_owner_id=OWNER, executor_signer_id=SIGNER,
        expires_at=NOW+86400, now=NOW, **CAPS,
    )


def verified():
    return {
        "wallet_identity_verified":True,
        "wallet_owned_by_authenticated_user":True,
        "wallet_chain_verified":True,
        "wallet_additional_signers_reviewed":True,
        "unrecognized_existing_signers":False,
        "policy_owner_independent":True,
        "user_live_limits_verified":True,
        "live_limits_nonzero":True,
        "trading_paused":True,
        "verified_wallet_address":WALLET,
        "live_limits_revision":4,
        "live_capital_usd":"5000",
        "live_max_order_usd":"5000",
        "live_daily_turnover_usd":"15000",
    }


def validated():
    return transport._validate_candidate(
        candidate(),did=DID,wallet=WALLET,
        policy_owner_id=OWNER,executor_signer_id=SIGNER,
        limits_revision=4,expires_at=NOW+86400,now=NOW,**CAPS,
    )


class ClaimCursor:
    def __init__(self):
        self.status="PREPARED"
        self.attempts=0
        self.result=None
        self.rev=4
        self.consent="SIGNED_INTENT"
        self.queries=[]
    def execute(self,sql,params):
        q=" ".join(sql.split())
        self.queries.append((q,params))
        if q.startswith("SELECT planned_capital_usd,"):
            self.result={
                "planned_capital_usd":Decimal("5000"),
                "max_order_usd":Decimal("5000"),
                "max_daily_turnover_usd":Decimal("15000"),
                "trading_paused":True,"revision":self.rev,
            }
        elif "FROM mininex_live_eth_consent_intents" in q:
            self.result={
                "wallet_address":WALLET,"limits_revision":4,
                "policy_owner_id":OWNER,"execution_signer_id":SIGNER,
                "status":self.consent,
                "confirmed_at":datetime.fromtimestamp(NOW,timezone.utc),
                "revoked_at":None,"message_sha256":"a"*64,
            }
        elif "FROM mininex_live_eth_policy_enrollments" in q:
            v=validated()
            self.result={
                "wallet_address":WALLET,"limits_revision":4,
                "policy_owner_id":OWNER,"execution_signer_id":SIGNER,
                "candidate_json":candidate(),
                "candidate_sha256":v["candidate_sha256"],
                "idempotency_key":v["idempotency_key"],
                "status":self.status,"policy_id":None,
                "expires_at_unix":NOW+86400,
                "created_at":datetime.fromtimestamp(NOW-20,timezone.utc),
            }
        elif q.startswith("UPDATE mininex_live_eth_policy_enrollments"):
            self.attempts+=1
            if self.status!="PREPARED":
                self.result=None
            else:
                self.status="SUBMITTING"
                self.result={"status":"SUBMITTING"}
        else:
            raise AssertionError(q)
    def fetchone(self):
        return self.result


def test_per_user_caps_read_from_exact_abi_rules_not_client():
    assert provision._extract_candidate_caps(candidate()) == CAPS
    for mutate in [
        lambda p:p["rules"][0]["conditions"][-1].update(value="0"),
        lambda p:p["rules"][3]["conditions"][-2].update(value="0"),
    ]:
        # May change only unrelated condition; exact full blueprint validator
        # must still catch every altered security field before the POST.
        p=candidate()
        mutate(p)
        if p!=candidate():
            with pytest.raises(ValueError):
                transport._validate_candidate(
                    p,did=DID,wallet=WALLET,
                    policy_owner_id=OWNER,executor_signer_id=SIGNER,
                    limits_revision=4,expires_at=NOW+86400,now=NOW,**CAPS,
                )


def test_durable_prepost_reservation_once_even_after_replay():
    cur=ClaimCursor()
    first=provision._claim_prepared_locked(
        cur,did=DID,evidence=verified(),owner=OWNER,
        executor=SIGNER,current_caps=CAPS,now=NOW,
    )
    assert first==validated()
    assert cur.attempts==1
    assert all("FOR UPDATE" in query for query,_ in cur.queries[:3])
    with pytest.raises(HTTPException) as error:
        provision._claim_prepared_locked(
            cur,did=DID,evidence=verified(),owner=OWNER,
            executor=SIGNER,current_caps=CAPS,now=NOW,
        )
    assert error.value.status_code==409
    assert cur.attempts==1


@pytest.mark.parametrize("change",[
    ("status","SUBMITTING"),
    ("status","UNCERTAIN"),
    ("status","REVOKE_REQUESTED"),
    ("status","REMOTE_VERIFIED_UNATTACHED"),
    ("rev",5),
    ("consent","REVOKED"),
])
def test_no_privy_post_claim_after_revoke_replay_or_limits_change(change):
    cur=ClaimCursor()
    setattr(cur,change[0],change[1])
    with pytest.raises(HTTPException):
        provision._claim_prepared_locked(
            cur,did=DID,evidence=verified(),owner=OWNER,executor=SIGNER,
            current_caps=CAPS,now=NOW,
        )
    assert cur.attempts==0


def test_updated_price_that_would_raise_usd_order_cap_blocks_post():
    cur=ClaimCursor()
    tighter=dict(CAPS,usdc_max_input_micro="4900000000")
    with pytest.raises(HTTPException):
        provision._claim_prepared_locked(
            cur,did=DID,evidence=verified(),owner=OWNER,
            executor=SIGNER,current_caps=tighter,now=NOW,
        )
    assert cur.attempts==0


class FinishCursor:
    def __init__(self,status="SUBMITTING"):
        self.status=status
        self.saved=None
        self.result=None
        self.queries=[]
    def execute(self,sql,params):
        q=" ".join(sql.split())
        self.queries.append((q,params))
        if q.startswith("SELECT wallet_address,"):
            v=validated()
            self.result={
                "candidate_sha256":v["candidate_sha256"],
                "idempotency_key":v["idempotency_key"],
                "status":self.status,
            }
        elif "status='UNCERTAIN'" in q:
            self.status="UNCERTAIN"
            self.result={"status":self.status}
        elif "status='REMOTE_VERIFIED_UNATTACHED'" in q:
            self.status="REMOTE_VERIFIED_UNATTACHED"
            self.saved=params
            self.result={"status":self.status}
        else:
            raise AssertionError(q)
    def fetchone(self):
        return self.result


def test_successful_remote_readback_records_unattached_policy_only():
    cur=FinishCursor()
    data=validated()
    result=provision._finish_locked(
        cur,did=DID,validated=data,result={
            "remote_exact_match":True,
            "policy_sha256":data["candidate_sha256"],
            "policy_id":POLICY_ID,
        },
    )
    assert cur.status=="REMOTE_VERIFIED_UNATTACHED"
    assert result["wallet_signer_attached"] is False
    assert result["can_submit"] is False
    assert result["execution_enabled"] is False


def test_failed_or_uncertain_post_is_never_retried():
    cur=FinishCursor()
    result=provision._finish_locked(
        cur,did=DID,validated=validated(),result=None,
    )
    assert cur.status=="UNCERTAIN"
    assert result["can_grant_permission"] is False
    assert result["execution_enabled"] is False
    with pytest.raises(HTTPException):
        provision._finish_locked(
            cur,did=DID,validated=validated(),result=None,
        )


@pytest.mark.parametrize("status",["REVOKE_REQUESTED","UNCERTAIN","REVOKED"])
def test_racing_user_revoke_does_not_revive_signed_permission(status):
    cur=FinishCursor(status=status)
    with pytest.raises(HTTPException):
        provision._finish_locked(
            cur,did=DID,validated=validated(),result={
                "remote_exact_match":True,
                "policy_sha256":validated()["candidate_sha256"],
                "policy_id":POLICY_ID,
            },
        )
    assert cur.status==status


def wallet_user():
    return {
        "id":DID,
        "linked_accounts":[{
            "type":"wallet","id":"wallet_2324_example",
            "chain_type":"ethereum","wallet_client_type":"privy",
            "address":WALLET,"delegated":True,
        }],
    }


def wallet_metadata(signers=None):
    return {
        "id":"wallet_2324_example","chain_type":"ethereum",
        "address":WALLET,"owner":{"user_id":DID},
        "additional_signers":[] if signers is None else signers,
    }


def remote_policy():
    p=deepcopy(candidate())
    p["id"]=POLICY_ID
    for index,r in enumerate(p["rules"]):
        r["id"]="rule_id_2324_"+str(index)
    return p


def stored_policy():
    return {
        "policy_id":POLICY_ID,
        "wallet_address":WALLET,
        "status":"REMOTE_VERIFIED_UNATTACHED",
        "policy_owner_id":OWNER,
        "execution_signer_id":SIGNER,
        "candidate_json":candidate(),
        "candidate_sha256":transport._digest(candidate()),
        "remote_policy_sha256":transport._digest(candidate()),
    }


def assess(meta=None,st=None,remote=None):
    return grant.assess_privy_user_grant(
        did=DID,user=wallet_user(),metadata=meta or wallet_metadata(),
        stored=st or stored_policy(),remote=remote or remote_policy(),
        evidence=verified(),owner=OWNER,signer=SIGNER,
    )


def test_new_policy_is_not_permission_without_independent_wallet_owner_click(monkeypatch):
    monkeypatch.delenv("MININEX_PRIVY_ETH_GRANT_ENABLED",raising=False)
    offer=assess()
    assert offer["wallet_owned_by_user"] is True
    assert offer["policy_verified"] is True
    assert offer["policy_id"]==POLICY_ID
    assert offer["execution_signer_id"]==SIGNER
    assert offer["can_request_user_grant"] is False
    assert offer["user_privy_grant_verified"] is False
    assert offer["execution_enabled"] is False
    assert offer["cumulative_spend_enforced_by_policy"] is False


def test_grant_offer_only_after_explicit_operator_audit_flag(monkeypatch):
    monkeypatch.setenv("MININEX_PRIVY_ETH_GRANT_ENABLED","enabled-after-audit")
    offer=assess()
    assert offer["grant_offer_enabled"] is True
    assert offer["can_request_user_grant"] is True
    assert offer["execution_enabled"] is False
    monkeypatch.setenv("MININEX_PRIVY_ETH_GRANT_ENABLED","true")
    assert assess()["can_request_user_grant"] is False


def test_wrong_policy_owner_extra_rules_or_another_wallet_denies_offer(monkeypatch):
    monkeypatch.setenv("MININEX_PRIVY_ETH_GRANT_ENABLED","enabled-after-audit")
    altered=remote_policy()
    altered["rules"].append(deepcopy(altered["rules"][0]))
    assert assess(remote=altered)["can_request_user_grant"] is False
    altered=remote_policy()
    altered["owner_id"]=SIGNER
    assert assess(remote=altered)["policy_verified"] is False
    altered=wallet_metadata()
    altered["owner"]={"user_id":"did:privy:other"}
    assert assess(meta=altered)["wallet_owned_by_user"] is False
    other=wallet_metadata([{"signer_id":"untrusted_other"}])
    assert assess(meta=other)["can_request_user_grant"] is False


def test_after_owner_grant_exact_one_privy_signer_and_policy_is_required():
    good=wallet_metadata([{
        "signer_id":SIGNER,"override_policy_ids":[POLICY_ID],
    }])
    result=assess(meta=good)
    assert result["user_privy_grant_verified"] is True
    assert result["can_request_user_grant"] is False
    for signers in [
        [{"signer_id":SIGNER,"override_policy_ids":[]}],
        [{"signer_id":SIGNER,"override_policy_ids":[POLICY_ID,"other_id"]}],
        [{"signer_id":OWNER,"override_policy_ids":[POLICY_ID]}],
        good["additional_signers"]+[{"signer_id":"rogue"}],
    ]:
        assert assess(meta=wallet_metadata(signers))["user_privy_grant_verified"] is False


class GrantCursor:
    def __init__(self,*,paused=True,revision=4,consent="SIGNED_INTENT",
                 state="REMOTE_VERIFIED_UNATTACHED"):
        self.paused=paused
        self.revision=revision
        self.consent=consent
        self.state=state
        self.writes=0
        self.result=None
    def execute(self,sql,params):
        q=" ".join(sql.split())
        if q.startswith("SELECT trading_paused"):
            self.result={"trading_paused":self.paused,"revision":self.revision}
        elif q.startswith("SELECT status,limits_revision,"):
            self.result={
                "status":self.consent, "confirmed_at":datetime.fromtimestamp(NOW,timezone.utc),
                "revoked_at":None,"wallet_address":WALLET,"limits_revision":4,
                "policy_owner_id":OWNER,"execution_signer_id":SIGNER,
            }
        elif q.startswith("SELECT policy_id,status,"):
            self.result={
                "status":self.state,
                "policy_id":POLICY_ID,"wallet_address":WALLET,
                "limits_revision":4,"policy_owner_id":OWNER,"execution_signer_id":SIGNER,
                "candidate_sha256":validated()["candidate_sha256"],
                "remote_policy_sha256":validated()["candidate_sha256"],
            }
        elif q.startswith("UPDATE mininex_live_eth_policy_enrollments"):
            self.writes+=1
            self.state="GRANT_VERIFIED"
            self.result={"status":self.state}
        else:
            raise AssertionError(q)
    def fetchone(self):
        return self.result


def expected():
    return {
        "policy_id":POLICY_ID, "wallet_address":WALLET,
        "limits_revision":4, "policy_owner_id":OWNER,
        "execution_signer_id":SIGNER,
        "candidate_sha256":validated()["candidate_sha256"],
    }


def test_local_grant_confirm_still_disables_trading():
    cur=GrantCursor()
    result=grant._confirm_grant_locked(cur,did=DID,expected=expected())
    assert cur.writes==1
    assert result["status"]=="GRANT_VERIFIED"
    assert result["execution_enabled"] is False


@pytest.mark.parametrize("bad",[
    {"paused":False},{"revision":5},
    {"consent":"REVOKED"},{"consent":"PENDING"},
    {"state":"REVOKE_REQUESTED"},{"state":"UNCERTAIN"},
    {"state":"PREPARED"},
])
def test_local_grant_confirmation_denies_stale_or_revoked_intent(bad):
    cur=GrantCursor(**bad)
    with pytest.raises(HTTPException):
        grant._confirm_grant_locked(cur,did=DID,expected=expected())
    assert cur.writes==0


@pytest.fixture
def auth(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    public=private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    now=int(time.time())
    jwt_token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"stage2324_owner_session",
        "iat":now,"exp":now+600,
    },private,algorithm="ES256")
    return {"Authorization":"Bearer "+jwt_token}


def test_user_bound_policy_post_and_grant_routes_are_authenticated(auth,monkeypatch):
    async def publish(did):
        assert did==DID
        return {"status":"REMOTE_VERIFIED_UNATTACHED","execution_enabled":False}
    async def review(did):
        assert did==DID
        return grant._inert_grant_result()
    async def confirm(did):
        assert did==DID
        return {"status":"GRANT_VERIFIED","execution_enabled":False}
    monkeypatch.setattr(api,"publish_user_eth_privy_policy",publish)
    monkeypatch.setattr(api,"get_eth_user_grant_review",review)
    monkeypatch.setattr(api,"confirm_eth_user_grant",confirm)
    client=TestClient(api.app)
    for method,path in [
        ("post","/api/live/eth/policy/create"),
        ("get","/api/live/eth/policy/grant-review"),
        ("post","/api/live/eth/policy/confirm-grant"),
    ]:
        call=getattr(client,method)
        assert call(path).status_code==401
        assert call(path,headers={"Authorization":"Bearer bad"}).status_code==401
        resp=call(path,headers=auth,json={"wallet_address":OTHER,"signer_id":"evil"} if method=="post" else None)
        assert resp.status_code==200
        assert resp.json()["execution_enabled"] is False
    assert client.get("/api/live/eth/policy/create",headers=auth).status_code==405
    assert client.post("/api/live/eth/policy/grant-review",headers=auth).status_code==405
    assert client.get("/api/live/eth/policy/confirm-grant",headers=auth).status_code==405
    assert "/api/live/eth/execute" not in [r.path for r in api.app.routes]
