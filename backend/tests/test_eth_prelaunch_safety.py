"""Stage 29/30 ETH read-only prelaunch audit: blocks all live sends."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_prelaunch_safety as review
from backend import eth_live_ledger_audit as exposure

DID="did:privy:prelaunch-stage29-user"


def enrollment():
    return {
        "mode":"ETH_PRIVY_USER_ENROLLMENT_REVIEW_ONLY",
        "asset":"ETH","chain_id":1,
        "wallet_identity_verified":True,
        "wallet_owned_by_authenticated_user":True,
        "wallet_chain_verified":True,
        "wallet_additional_signers_reviewed":True,
        "unrecognized_existing_signers":False,
        "policy_owner_independent":True,
        "user_live_limits_verified":True,
        "live_limits_nonzero":True,
        "trading_paused":True,
        "live_limits_revision":3,
        "execution_enabled":False,
    }


def policy(status="REMOTE_VERIFIED_UNATTACHED"):
    return {
        "mode":"ETH_PRIVY_POLICY_DURABLE_PREPARATION_ONLY",
        "asset":"ETH","chain_id":1,
        "status":status,
        "limits_revision":3,
        "live_limits_revision_matches":True,
        "policy_expired":False,
        "policy_expires_at_unix":1900000100,
        "remote_policy_recorded":True,
        "policy_id":"policy_test_xx0",
        "reconciliation_required":False,
        "execution_enabled":False,
    }


def ledger():
    return exposure.assess_eth_live_ledger({
        "planned_capital_usd":"5000",
        "max_order_usd":"1000",
        "max_daily_turnover_usd":"7000",
        "revision":3,
        "trading_paused":True,
    },{
        "pending_usd":"0",
        "confirmed_24h_usd":"0",
        "pending_count":0,
        "unreconciled_count":0,
        "stale_pending_count":0,
    })


def grant():
    return {
        "policy_verified":True,
        "wallet_owned_by_user":True,
        "user_privy_grant_verified":False,
        "grant_offer_enabled":False,
    }


def safety(*,e=None,p=None,l=None,g=None):
    return review.assess_eth_prelaunch_safety(
        enrollment=enrollment() if e is None else e,
        policy=policy() if p is None else p,
        ledger=ledger() if l is None else l,
        grant=grant() if g is None else g,
    )


def test_even_fully_verified_policy_grant_never_becomes_live_authorization():
    g=grant()
    g["user_privy_grant_verified"]=True
    report=safety(p=policy("GRANT_VERIFIED"),g=g)
    assert report["policy_status"]=="GRANT_VERIFIED"
    assert report["privy_signer_grant_verified"] is True
    assert report["execution_enabled"] is False
    assert report["can_submit"] is False
    assert report["can_sign"] is False
    assert report["real_trading_enabled"] is False
    assert report["read_only"] is True
    assert report["policy_created_by_this_check"] is False
    assert report["wallet_signer_modified_by_this_check"] is False
    assert report["automatic_privy_policy_retry_permitted"] is False
    assert "LIVE_EXECUTION_NOT_IMPLEMENTED" in report["blockers"]
    assert "DAILY_SPEND_EXECUTION_GATE_NOT_ACTIVATED" in report["blockers"]


@pytest.mark.parametrize("status,blocker",[
    ("NOT_PREPARED","POLICY_NOT_CREATED_AND_VERIFIED_REMOTELY"),
    ("PREPARED","POLICY_NOT_CREATED_AND_VERIFIED_REMOTELY"),
    ("SUBMITTING","POLICY_STATE_REQUIRES_MANUAL_RECONCILIATION"),
    ("UNCERTAIN","POLICY_STATE_REQUIRES_MANUAL_RECONCILIATION"),
    ("REVOKE_REQUESTED","POLICY_STATE_REQUIRES_MANUAL_RECONCILIATION"),
    ("REVOKED","USER_REVOKED_POLICY"),
])
def test_each_nonremote_policy_status_fails_closed(status,blocker):
    p=policy(status)
    p["remote_policy_recorded"]=False
    r=safety(p=p)
    assert blocker in r["blockers"]
    assert r["execution_enabled"] is False
    assert r["can_submit"] is False
    assert r["manual_policy_reconciliation_required"]==(
        status in ("SUBMITTING","UNCERTAIN","REVOKE_REQUESTED")
    )


def test_expired_policy_wrong_budget_revision_or_privy_readback_blocks():
    p=policy()
    p["policy_expired"]=True
    assert "PRIVY_POLICY_EXPIRED" in safety(p=p)["blockers"]
    p=policy()
    p["live_limits_revision_matches"]=False
    assert "POLICY_LIMITS_REVISION_MISMATCH" in safety(p=p)["blockers"]
    assert "REMOTE_PRIVY_POLICY_READBACK_NOT_VERIFIED" in safety(g={})["blockers"]
    e=enrollment()
    e["live_limits_revision"]=4
    assert "LIVE_LIMITS_REVISION_MISMATCH" in safety(e=e)["blockers"]


def test_prelaunch_report_lists_pending_reconciliation_without_cancelling():
    l=ledger()
    l["reconciliation_required_count"]=1
    l["older_than_30m_pending_count"]=1
    result=safety(l=l)
    assert "UNSETTLED_ORDER_RECONCILIATION_REQUIRED" in result["blockers"]
    assert "STALE_UNRESOLVED_ORDER" in result["blockers"]
    assert result["manual_policy_reconciliation_required"] is False


def test_unverified_additional_signer_not_preapproved():
    e=enrollment()
    e["unrecognized_existing_signers"]=True
    assert "UNVERIFIED_ADDITIONAL_SIGNERS" in safety(e=e)["blockers"]
    assert "UNVERIFIED_ADDITIONAL_SIGNERS" not in safety(
        e=e,g={**grant(),"user_privy_grant_verified":True},
    )["blockers"]


@pytest.mark.parametrize("section,patch",[
    ("enrollment",{"mode":"SOMETHING_ELSE"}),
    ("enrollment",{"chain_id":56}),
    ("enrollment",{"execution_enabled":True}),
    ("policy",{"mode":"EXECUTION_AUTHORIZED"}),
    ("policy",{"status":"LIVE_NOW"}),
    ("policy",{"asset":"BTC"}),
    ("ledger",{"execution_enabled":True}),
    ("ledger",{"mode":"PAPER_BUDGET"}),
    ("ledger",{"cumulative_spend_enforced_at_execution":True}),
    ("ledger",{"unresolved_order_count":None}),
])
def test_inconsistent_evidence_cannot_become_green(section,patch):
    parts={"enrollment":enrollment(),"policy":policy(),"ledger":ledger()}
    parts[section].update(patch)
    with pytest.raises(HTTPException) as err:
        safety(e=parts["enrollment"],p=parts["policy"],l=parts["ledger"])
    assert err.value.status_code==503


def test_remote_privy_error_reports_blocked_without_fake_grant(monkeypatch):
    async def fake_enrollment(did):
        assert did==DID
        return enrollment()
    def fake_policy(did):
        assert did==DID
        return policy()
    def fake_ledger(did):
        assert did==DID
        return ledger()
    async def fail_privy(did):
        raise HTTPException(503,"Privy read-back unavailable")
    monkeypatch.setattr(review,"get_eth_user_enrollment_preflight",fake_enrollment)
    monkeypatch.setattr(review,"get_eth_policy_ledger_status",fake_policy)
    monkeypatch.setattr(review,"get_eth_live_ledger_audit",fake_ledger)
    monkeypatch.setattr(review,"get_eth_user_grant_review",fail_privy)
    result=asyncio.run(review.get_eth_prelaunch_safety_report(DID))
    assert "REMOTE_PRIVY_POLICY_READBACK_NOT_VERIFIED" in result["blockers"]
    assert result["privy_signer_grant_verified"] is False
    assert result["execution_enabled"] is False


@pytest.fixture
def auth(monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    pub=key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"stage29-owner",
        "iat":now,"exp":now+300,
    },key,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_readiness_endpoint_authenticated_readonly_no_submission(auth,monkeypatch):
    async def result(did):
        assert did==DID
        return safety()
    monkeypatch.setattr(api,"get_eth_prelaunch_safety_report",result)
    client=TestClient(api.app)
    path="/api/live/eth/prelaunch-safety"
    assert client.get(path).status_code==401
    assert client.get(path,headers={"Authorization":"Bearer fake"}).status_code==401
    r=client.get(path,headers=auth,params={"force_live":"true"})
    assert r.status_code==200
    assert r.headers["cache-control"]=="private, no-store"
    assert r.json()["execution_enabled"] is False
    assert r.json()["read_only"] is True
    assert client.post(path,headers=auth).status_code==405
    assert "/api/live/eth/execute" not in [x.path for x in api.app.routes]
