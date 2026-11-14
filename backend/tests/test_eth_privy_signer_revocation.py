"""Stage 22: test user-approved Privy removal and independent readback.

No real Privy external network calls. The only mutation is to MiniNex's
locally revoked ledger after a fresh server-verified EMPTY signer list.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_privy_signer_revocation as revoke

DID = "did:privy:stage22-test-user"
OTHER_DID = "did:privy:other-person"
WALLET_ID = "wallet_stage22_verified"
WALLET = "0x" + "a"*40
OTHER = "0x" + "b"*40
OWNER = "owner_stage22_quorum"
EXECUTOR = "signer_stage22_quorum"


def user(did=DID, address=WALLET, wallet_id=WALLET_ID):
    return {
        "id": did,
        "linked_accounts": [{
            "type": "wallet",
            "id": wallet_id,
            "wallet_client_type": "privy",
            "chain_type": "ethereum",
            "address": address,
            "delegated": True,
        }],
    }


def metadata(
    *, owner=DID, address=WALLET, wallet_id=WALLET_ID, signers=None,
):
    return {
        "id": wallet_id,
        "chain_type": "ethereum",
        "address": address,
        "owner": {"user_id": owner},
        "additional_signers": [] if signers is None else signers,
    }


def check(user_record=None, remote_wallet=None):
    return revoke.check_privy_wallet_signers_absent(
        did=DID,
        privy_user=user_record if user_record is not None else user(),
        wallet_metadata=remote_wallet if remote_wallet is not None else metadata(),
    )


def test_empty_privy_signer_list_verified_exact_owner_wallet():
    report = check()
    assert report["mode"] == "PRIVY_SIGNER_ABSENCE_REMOTE_READ_ONLY"
    assert report["verified_wallet_address"] == WALLET
    assert report["wallet_identity_verified"] is True
    assert report["wallet_owned_by_authenticated_user"] is True
    assert report["remote_wallet_metadata_verified"] is True
    assert report["additional_signers_field_verified"] is True
    assert report["signer_absence_verified"] is True
    assert report["privy_remote_signer_removed_by_backend"] is False
    assert report["privy_policy_deleted"] is False
    assert report["revocation_remains_continuously_enforced"] is False
    assert report["execution_enabled"] is False
    assert report["can_sign"] is False and report["can_submit"] is False


@pytest.mark.parametrize("remote", [
    metadata(owner=OTHER_DID),
    metadata(address=OTHER),
    metadata(wallet_id="other_wallet_stage22"),
    {**metadata(), "chain_type": "solana"},
    {**metadata(), "additional_signers": None},
    {**metadata(), "additional_signers": {}},
    {**metadata(), "additional_signers": "[]"},
    {**metadata(), "additional_signers": [{"signer_id": EXECUTOR}]},
    {**metadata(), "additional_signers": [{"signer_id": "unknown_signer"}]},
    {k: v for k, v in metadata().items() if k != "additional_signers"},
    {},
])
def test_invalid_remote_signer_state_fails_closed(remote):
    result = revoke.check_privy_wallet_signers_absent(
        did=DID, privy_user=user(), wallet_metadata=remote,
    )
    assert result["signer_absence_verified"] is False
    assert result["execution_enabled"] is False


@pytest.mark.parametrize("wrong_user", [
    user(did=OTHER_DID),
    user(address=OTHER),
    {"id":DID, "linked_accounts":[]},
    {"id":DID, "linked_accounts":[
        {**user()["linked_accounts"][0], "type":"external_wallet"}
    ]},
    {"id":DID, "linked_accounts":[
        *user()["linked_accounts"], *user()["linked_accounts"]
    ]},
    {},
])
def test_unauthorized_or_ambiguous_privy_wallet_cannot_confirm_absence(wrong_user):
    result = revoke.check_privy_wallet_signers_absent(
        did=DID, privy_user=wrong_user, wallet_metadata=metadata(),
    )
    assert result["signer_absence_verified"] is False
    assert result["wallet_identity_verified"] is False


class FakeCursor:
    def __init__(
        self, *, paused=True, consent_status="REVOKED", revoked_at=True,
        wallet=WALLET, executor=EXECUTOR, state="REVOKE_REQUESTED",
        update_success=True,
    ):
        self.paused=paused
        self.consent_status=consent_status
        self.revoked_at=revoked_at
        self.wallet=wallet
        self.executor=executor
        self.state=state
        self.update_success=update_success
        self.queries=[]
        self.result=None
    def execute(self, sql, params):
        query=" ".join(sql.split())
        self.queries.append((query,params))
        if query.startswith("SELECT trading_paused"):
            self.result={"trading_paused": self.paused}
        elif query.startswith("SELECT status,revoked_at"):
            self.result={
                "status":self.consent_status,
                "revoked_at":datetime.now(timezone.utc) if self.revoked_at else None,
            }
        elif query.startswith("SELECT wallet_address,execution_signer_id"):
            self.result={
                "wallet_address":self.wallet,
                "execution_signer_id":self.executor,
                "status":self.state,
            }
        elif query.startswith("UPDATE mininex_live_eth_policy_enrollments"):
            if self.update_success:
                self.state="REVOKED"
                self.result={"status":"REVOKED"}
            else:
                self.result=None
        else:
            raise AssertionError("Unexpected SQL: "+query)
    def fetchone(self):
        return self.result


def confirm(cur):
    return revoke._confirm_local_revocation_locked(
        cur, did=DID, verified_wallet=WALLET, expected_executor=EXECUTOR,
    )


def test_atomic_revoke_confirmation_after_remote_signer_absence():
    cur=FakeCursor()
    result=confirm(cur)
    assert [q.split(" ")[0] for q,_ in cur.queries] == [
        "SELECT","SELECT","SELECT","UPDATE",
    ]
    assert all("FOR UPDATE" in q for q,_ in cur.queries[:3])
    assert "status='REVOKE_REQUESTED'" in cur.queries[-1][0]
    assert cur.queries[-1][1]==(DID,WALLET)
    assert result["status"]=="REVOKED"
    assert result["local_consent_revoked"] is True
    assert result["signer_absence_verified_by_privy_get"] is True
    assert result["privy_remote_signer_removed_by_backend"] is False
    assert result["privy_policy_deleted"] is False
    assert result["revocation_remains_continuously_enforced"] is False
    assert result["execution_enabled"] is False


def test_duplicate_confirmation_read_only_when_already_revoked():
    cur=FakeCursor(state="REVOKED")
    result=confirm(cur)
    assert result["status"]=="REVOKED"
    assert not any(q.startswith("UPDATE") for q,_ in cur.queries)


@pytest.mark.parametrize("change", [
    {"paused":False},
    {"paused":None},
    {"consent_status":"SIGNED_INTENT"},
    {"consent_status":"PENDING"},
    {"revoked_at":False},
    {"wallet":OTHER},
    {"executor":OWNER},
    {"state":"PREPARED"},
    {"state":"SUBMITTING"},
    {"state":"UNCERTAIN"},
    {"state":"REMOTE_VERIFIED_UNATTACHED"},
    {"state":"INVALID"},
    {"update_success":False},
])
def test_stale_local_stop_consent_wallet_or_ledger_refuses_confirmation(change):
    cur=FakeCursor(**change)
    with pytest.raises(HTTPException) as error:
        confirm(cur)
    assert error.value.status_code==409
    assert cur.state != "REVOKED"


def test_remote_check_uses_authoritative_privy_wallet_id_not_frontend(monkeypatch):
    calls=[]
    async def server_user(did):
        assert did==DID
        calls.append(("user",did))
        return user()
    async def server_wallet(wallet_id):
        assert wallet_id==WALLET_ID
        calls.append(("wallet",wallet_id))
        return metadata()
    monkeypatch.setattr(revoke,"fetch_privy_user",server_user)
    monkeypatch.setattr(revoke,"fetch_privy_wallet_metadata",server_wallet)
    result=asyncio.run(revoke.get_privy_wallet_signer_absence(DID))
    assert result["signer_absence_verified"] is True
    assert calls==[("user",DID),("wallet",WALLET_ID)]


def test_remote_confirmation_never_writes_db_if_privy_has_any_signer(
    monkeypatch,
):
    async def server_user(did):
        return user()
    async def server_wallet(wallet_id):
        return metadata(signers=[{
            "signer_id":EXECUTOR,
            "override_policy_ids":["mock_policy"],
        }])
    def db_must_not_run(*args, **kwargs):
        raise AssertionError("No local completion while remote signer exists")
    monkeypatch.setenv("MININEX_PRIVY_ETH_POLICY_OWNER_ID",OWNER)
    monkeypatch.setenv("MININEX_PRIVY_ETH_SIGNER_ID",EXECUTOR)
    monkeypatch.setattr(revoke,"fetch_privy_user",server_user)
    monkeypatch.setattr(revoke,"fetch_privy_wallet_metadata",server_wallet)
    monkeypatch.setattr(revoke,"_persist_remote_absence_confirmation",db_must_not_run)
    with pytest.raises(HTTPException) as error:
        asyncio.run(revoke.confirm_privy_wallet_signer_revocation(DID))
    assert error.value.status_code==409


def test_remote_confirmation_calls_db_only_after_fresh_empty_privy_read(
    monkeypatch,
):
    calls=[]
    async def server_user(did):
        calls.append("user")
        return user()
    async def server_wallet(wallet_id):
        calls.append("wallet")
        return metadata()
    def persist(did,wallet,executor):
        assert did==DID and wallet==WALLET and executor==EXECUTOR
        calls.append("db")
        return {"status":"REVOKED","execution_enabled":False}
    monkeypatch.setenv("MININEX_PRIVY_ETH_POLICY_OWNER_ID",OWNER)
    monkeypatch.setenv("MININEX_PRIVY_ETH_SIGNER_ID",EXECUTOR)
    monkeypatch.setattr(revoke,"fetch_privy_user",server_user)
    monkeypatch.setattr(revoke,"fetch_privy_wallet_metadata",server_wallet)
    monkeypatch.setattr(revoke,"_persist_remote_absence_confirmation",persist)
    result=asyncio.run(revoke.confirm_privy_wallet_signer_revocation(DID))
    assert calls==["user","wallet","db"]
    assert result["status"]=="REVOKED"
    assert result["execution_enabled"] is False


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
        "iss":"privy.io",
        "aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"stage22-user-session",
        "iat":now,"exp":now+600,
    }, private, algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_http_routes_authenticated_no_wallet_id_user_input(auth,monkeypatch):
    async def check_absence(did):
        assert did==DID
        return check()
    async def confirm_removal(did):
        assert did==DID
        return {
            "status":"REVOKED","execution_enabled":False,
            "signer_absence_verified_by_privy_get":True,
        }
    monkeypatch.setattr(api,"get_privy_wallet_signer_absence",check_absence)
    monkeypatch.setattr(
        api,"confirm_privy_wallet_signer_revocation",confirm_removal,
    )
    client=TestClient(api.app)
    for path in (
        "/api/live/eth/policy/wallet-signer-absence",
        "/api/live/eth/policy/confirm-revocation",
    ):
        assert client.get(path).status_code==401 or path.endswith(
            "confirm-revocation",
        )
        assert client.post(path).status_code==401 or path.endswith(
            "wallet-signer-absence",
        )
    get=client.get(
        "/api/live/eth/policy/wallet-signer-absence",headers=auth,
    )
    assert get.status_code==200
    assert get.json()["signer_absence_verified"] is True
    assert get.headers["cache-control"]=="private, no-store"
    assert client.post(
        "/api/live/eth/policy/wallet-signer-absence",headers=auth,
    ).status_code==405
    post=client.post(
        "/api/live/eth/policy/confirm-revocation",headers=auth,
        json={"wallet":OTHER,"policy_id":"forged"},
    )
    assert post.status_code==200
    assert post.json()["execution_enabled"] is False
    assert client.get(
        "/api/live/eth/policy/confirm-revocation",headers=auth,
    ).status_code==405
    routes={route.path for route in api.app.routes}
    for forbidden in (
        "/api/live/eth/attach-signer", "/api/live/eth/delegate",
        "/api/live/eth/create-policy", "/api/live/eth/execute",
    ):
        assert forbidden not in routes
