"""Stages 41-48: Privy incident/recovery read-only, no remote writes or retry."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_privy_policy_incident as incident
from backend.eth_privy_policy_transport import _digest
from backend.eth_privy_user_policy_blueprint import build_eth_privy_user_policy_blueprint

DID="did:privy:stage41-48-test"
NOW=1900000000
WALLET="0x"+"a"*40
OWNER="independent_policy_owner_41"
SIGNER="restricted_executor_signer_41"
POLICY_ID="policy_recovery_41_individual"
CAPS={
    "usdc_max_input_micro":"2000000000",
    "usdt_max_input_micro":"2000000000",
    "weth_max_input_wei":"660000000000000000",
    "native_wrap_max_wei":"660000000000000000",
}


def policy():
    return build_eth_privy_user_policy_blueprint(
        wallet=WALLET,policy_owner_id=OWNER,
        executor_signer_id=SIGNER,
        now=NOW,expires_at=NOW+86400,**CAPS,
    )


def row(status="UNCERTAIN"):
    p=policy()
    return {
        "wallet_address":WALLET,"limits_revision":11,"live_revision":11,
        "policy_owner_id":OWNER,"execution_signer_id":SIGNER,
        "status":status,"policy_id":None,
        "candidate_json":p,"candidate_sha256":_digest(p),
        "remote_policy_sha256":None,
        "created_at":datetime.fromtimestamp(NOW-300,timezone.utc),
        "last_attempt_at":datetime.fromtimestamp(NOW-120,timezone.utc),
        "expires_at_unix":NOW+86400,
        "trading_paused":True,"consent_status":"SIGNED_INTENT",
    }


def remote_policy():
    r=deepcopy(policy())
    r["id"]=POLICY_ID
    for index,rule in enumerate(r["rules"]):
        rule["id"]="remote_rule_recovery_"+str(index)
    return r


def signer_report(*,absent=True,wallet=WALLET,verified=True):
    return {
        "mode":"PRIVY_SIGNER_ABSENCE_REMOTE_READ_ONLY",
        "asset":"ETH","chain_id":1,
        "verified_wallet_address":wallet,
        "wallet_identity_verified":verified,
        "wallet_owned_by_authenticated_user":verified,
        "remote_wallet_metadata_verified":verified,
        "additional_signers_field_verified":verified,
        "signer_absence_verified":absent,
        "execution_enabled":False,
    }


def classify(status="UNCERTAIN",*,now=NOW,remote=None,signers=None,**updates):
    r=row(status)
    r.update(updates)
    return incident.assess_privy_policy_incident(
        row=r,now=now,remote=remote,signer_absence=signers,
    )


def assert_inert(report):
    assert report["mode"]=="ETH_PRIVY_INCIDENT_REVIEW_READ_ONLY"
    assert report["asset"]=="ETH" and report["chain_id"]==1
    assert report["automatic_retry_allowed"] is False
    assert report["safe_to_recreate_policy"] is False
    assert report["remote_policy_created_by_review"] is False
    assert report["remote_policy_deleted"] is False
    assert report["privy_signer_changed_by_review"] is False
    assert report["execution_enabled"] is False
    assert report["can_sign"] is False and report["can_submit"] is False
    assert "ETH_LIVE_EXECUTION_DISABLED" in report["blockers"]
    assert "NO_AUTOMATIC_PRIVY_RETRY" in report["blockers"]


@pytest.mark.parametrize("age,reason",[
    (5,"RECENT_POLICY_POST_OUTCOME_UNKNOWN"),
    (90,"RECENT_POLICY_POST_OUTCOME_UNKNOWN"),
    (91,"STALE_OR_UNKNOWN_POLICY_POST_OUTCOME"),
    (3600,"STALE_OR_UNKNOWN_POLICY_POST_OUTCOME"),
])
def test_privy_submitting_never_auto_retries_even_if_recent(age,reason):
    attempted=datetime.fromtimestamp(NOW-age,timezone.utc)
    result=classify("SUBMITTING",last_attempt_at=attempted)
    assert result["last_remote_attempt_age_seconds"]==age
    assert reason in result["blockers"]
    assert result["action_code"]=="MANUAL_RECONCILIATION_REQUIRED"
    assert_inert(result)


@pytest.mark.parametrize("timestamp",[
    None,
    datetime(2030,1,1),
    datetime.fromtimestamp(NOW+5,timezone.utc),
])
def test_unknown_future_or_naive_attempt_time_cannot_claim_recent(timestamp):
    result=classify("SUBMITTING",last_attempt_at=timestamp)
    assert result["last_remote_attempt_age_seconds"] is None
    assert "STALE_OR_UNKNOWN_POLICY_POST_OUTCOME" in result["blockers"]
    assert_inert(result)


def test_uncertain_missing_policy_id_stays_manual_and_never_means_deleted():
    result=classify("UNCERTAIN")
    assert result["policy_id"] is None
    assert result["remote_policy_exactly_verified_now"] is False
    assert "POLICY_POST_RESULT_UNCERTAIN" in result["blockers"]
    assert result["action_code"]=="MANUAL_RECONCILIATION_REQUIRED"
    assert_inert(result)


def test_known_remote_policy_requires_exact_server_readback():
    stored=row("REMOTE_VERIFIED_UNATTACHED")
    stored["policy_id"]=POLICY_ID
    stored["remote_policy_sha256"]=stored["candidate_sha256"]
    good=incident.assess_privy_policy_incident(
        row=stored,now=NOW,remote=remote_policy(),signer_absence=signer_report(),
    )
    assert good["remote_policy_exactly_verified_now"] is True
    assert good["privy_additional_signers_absent_now"] is True
    assert good["action_code"]=="REMOTE_POLICY_REVIEW"
    assert "USER_SIGNER_GRANT_NOT_VERIFIED" in good["blockers"]
    assert_inert(good)
    wrong=remote_policy()
    wrong["rules"].append(deepcopy(wrong["rules"][-1]))
    result=incident.assess_privy_policy_incident(
        row=stored,now=NOW,remote=wrong,signer_absence=signer_report(),
    )
    assert result["remote_policy_exactly_verified_now"] is False
    assert result["action_code"]=="MANUAL_RECONCILIATION_REQUIRED"
    assert "REMOTE_POLICY_NOT_INDEPENDENTLY_VERIFIED" in result["blockers"]
    assert_inert(result)


@pytest.mark.parametrize("mutation",[
    lambda p:p.update(owner_id="wrong_policy_owner"),
    lambda p:p["rules"][0]["conditions"].clear(),
    lambda p:p["rules"][0].update(method="eth_sign"),
    lambda p:p["rules"][0].update(action="ALLOW_ALL"),
    lambda p:p.update(id="policy_different"),
    lambda p:p["rules"][1].update(id=p["rules"][0]["id"]),
])
def test_modified_remote_policy_never_matches_onchain_permissions(mutation):
    candidate=row("GRANT_VERIFIED")
    candidate["policy_id"]=POLICY_ID
    remote=remote_policy()
    mutation(remote)
    result=incident.assess_privy_policy_incident(
        row=candidate,now=NOW,remote=remote,
        signer_absence=signer_report(absent=False),
    )
    assert result["remote_policy_exactly_verified_now"] is False
    assert result["action_code"]=="MANUAL_RECONCILIATION_REQUIRED"
    assert_inert(result)


def test_revoke_confirm_requires_same_wallet_from_independent_privy_get():
    stored=row("REVOKE_REQUESTED")
    stored["policy_id"]=POLICY_ID
    for report in (
        signer_report(wallet="0x"+"b"*40),
        signer_report(verified=False),
        signer_report(absent=False),
        None,
    ):
        r=incident.assess_privy_policy_incident(
            row=stored,now=NOW,remote=remote_policy(),signer_absence=report,
        )
        assert r["action_code"]!="CONFIRM_LOCAL_REVOKE_AFTER_OWNER_CHECK"
        assert_inert(r)
    confirmed=incident.assess_privy_policy_incident(
        row=stored,now=NOW,remote=remote_policy(),
        signer_absence=signer_report(absent=True),
    )
    assert confirmed["action_code"]=="CONFIRM_LOCAL_REVOKE_AFTER_OWNER_CHECK"
    assert confirmed["privy_additional_signers_absent_now"] is True
    assert confirmed["remote_policy_deleted"] is False
    assert_inert(confirmed)


def test_policy_id_unknown_on_revoke_is_never_assumed_absent():
    r=classify(
        "REVOKE_REQUESTED",signers=signer_report(absent=True),
    )
    assert "REMOTE_POLICY_ID_UNKNOWN" in r["blockers"]
    assert r["action_code"]=="MANUAL_RECONCILIATION_REQUIRED"
    assert_inert(r)


def test_stored_grant_with_absent_privy_signer_reports_stale():
    result=classify(
        "GRANT_VERIFIED",policy_id=POLICY_ID,
        remote=remote_policy(),signers=signer_report(absent=True),
    )
    assert result["privy_wallet_signers_verified_now"] is True
    assert "STORED_GRANT_NO_LONGER_ATTACHED" in result["blockers"]
    assert result["action_code"]=="RECHECK_WALLET_AUTHORITY"
    assert_inert(result)


def test_expired_prepared_policy_is_not_reused_without_human_review():
    result=classify("PREPARED",now=NOW+86401)
    assert result["policy_expired"] is True
    assert result["action_code"]=="MANUAL_RECONCILIATION_REQUIRED"
    assert "PREPARED_POLICY_EXPIRED" in result["blockers"]
    assert_inert(result)


def test_no_policy_yet_is_not_repeated_privy_create():
    r=row("PREPARED")
    for field in (
        "wallet_address","status","policy_id","candidate_json",
        "candidate_sha256","expires_at_unix","limits_revision",
    ):r[field]=None
    result=incident.assess_privy_policy_incident(row=r,now=NOW)
    assert result["status"]=="NOT_PREPARED"
    assert result["action_code"]=="NO_POLICY_PREPARED"
    assert_inert(result)


@pytest.mark.parametrize("corrupt",[
    {"trading_paused":"yes"},
    {"live_revision":True},
    {"live_revision":-1},
    {"status":"AUTO_TRADE"},
    {"wallet_address":"0xnot_wallet"},
    {"limits_revision":True},
    {"policy_id":"with/slash"},
    {"expires_at_unix":"forever"},
])
def test_malformed_local_incident_evidence_fails_closed(corrupt):
    with pytest.raises(HTTPException) as ex:
        classify(**corrupt)
    assert ex.value.status_code==503


def test_independent_readback_failure_does_not_claim_remote_deletion(monkeypatch):
    calls=[]
    def local(did):
        assert did==DID
        calls.append("db")
        return row("REVOKE_REQUESTED")|{"policy_id":POLICY_ID}
    async def unavailable_policy(policy_id):
        calls.append("privy_policy_get")
        raise HTTPException(503,"Privy unavailable")
    async def unavailable_wallet(did):
        calls.append("privy_wallet_get")
        raise HTTPException(503,"Privy unavailable")
    monkeypatch.setattr(incident,"_stored_policy_for_incident",local)
    monkeypatch.setattr(incident,"_fetch_policy",unavailable_policy)
    monkeypatch.setattr(incident,"get_privy_wallet_signer_absence",unavailable_wallet)
    monkeypatch.setattr(incident.time,"time",lambda:NOW)
    review=asyncio.run(incident.get_eth_privy_policy_incident_review(DID))
    assert calls==["db","privy_policy_get","privy_wallet_get"]
    assert review["remote_policy_deleted"] is False
    assert review["automatic_retry_allowed"] is False
    assert review["privy_wallet_signers_verified_now"] is False
    assert "REMOTE_POLICY_NOT_INDEPENDENTLY_VERIFIED" in review["blockers"]
    assert_inert(review)


@pytest.fixture
def auth(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    public=private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"recovery_review_41",
        "iat":now,"exp":now+500,
    },private,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_incident_route_user_auth_readonly_and_no_client_wallet_override(auth,monkeypatch):
    seen=[]
    async def fake(did):
        seen.append(did)
        return classify("UNCERTAIN")
    monkeypatch.setattr(api,"get_eth_privy_policy_incident_review",fake)
    client=TestClient(api.app)
    path="/api/live/eth/policy/incident-review"
    assert client.get(path).status_code==401
    assert client.get(path,headers={"Authorization":"Bearer bad"}).status_code==401
    result=client.get(path,headers=auth,params={
        "wallet_address":"0x"+"b"*40,"allow_retry":"true",
    })
    assert result.status_code==200
    assert result.headers["cache-control"]=="private, no-store"
    assert result.json()["automatic_retry_allowed"] is False
    assert result.json()["execution_enabled"] is False
    assert seen==[DID]
    assert client.post(path,headers=auth).status_code==405
    assert "/api/live/eth/execute" not in [route.path for route in api.app.routes]
