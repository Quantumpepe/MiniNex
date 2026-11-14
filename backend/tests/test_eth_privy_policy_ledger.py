"""Stage 21 durable Privy policy enrollment ledger -- no remote writes.

SQL transition tests use a deterministic fake cursor; API tests authenticate
real signed JWTs and stub only the isolated storage/network dependencies.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_privy_policy_ledger as ledger
from backend import eth_privy_policy_plan as planner
from backend import eth_user_consent as consent
from backend import eth_privy_policy_transport as transport

DID = "did:privy:stage21-consenting-user"
WALLET = "0x" + "a"*40
OTHER = "0x" + "b"*40
WALLET_ID = "wallet_stage21_verified"
OWNER = "policy_owner_stage21_quorum"
SIGNER = "executor_stage21_quorum"
NOW = 1_900_000_000
REVISION = 7
CAPS = {
    "capital": "5000", "order": "5000", "daily": "15000",
}


def evidence(*, wallet=WALLET, revision=REVISION, **change):
    values = {
        "wallet_identity_verified": True,
        "wallet_owned_by_authenticated_user": True,
        "wallet_chain_verified": True,
        "wallet_additional_signers_reviewed": True,
        "unrecognized_existing_signers": False,
        "policy_owner_independent": True,
        "user_live_limits_verified": True,
        "live_limits_nonzero": True,
        "trading_paused": True,
        "verified_wallet_address": wallet,
        "live_limits_revision": revision,
        "live_capital_usd": CAPS["capital"],
        "live_max_order_usd": CAPS["order"],
        "live_daily_turnover_usd": CAPS["daily"],
    }
    values.update(change)
    return values


def candidate(ev=None):
    if ev is None:
        ev = evidence()
    prices = {
        "ethereum": {"usd": 3000, "last_updated_at": NOW},
        "usd-coin": {"usd": 1, "last_updated_at": NOW},
        "tether": {"usd": 1, "last_updated_at": NOW},
    }
    return planner.build_verified_eth_policy_candidate(
        evidence=ev, snapshot=prices, owner=OWNER,
        executor=SIGNER, now=NOW,
    )


class Cursor:
    """Fake transactional cursor that keeps an immutable PREPARED row."""
    def __init__(self):
        self.existing = None
        self.insert_count = 0
        self.queries = []
        self.limits_rev = REVISION
        self.consent_status = "SIGNED_INTENT"
        self.consent_owner = OWNER
        self.signer = SIGNER
        self._fetched = None

    def execute(self, sql, params):
        query = " ".join(sql.split())
        self.queries.append((query, params))
        if query.startswith("SELECT planned_capital_usd,"):
            self._fetched = {
                "planned_capital_usd": Decimal(CAPS["capital"]),
                "max_order_usd": Decimal(CAPS["order"]),
                "max_daily_turnover_usd": Decimal(CAPS["daily"]),
                "trading_paused": True,
                "revision": self.limits_rev,
            }
        elif "FROM mininex_live_eth_consent_intents" in query:
            self._fetched = {
                "wallet_address": WALLET,
                "limits_revision": REVISION,
                "policy_owner_id": self.consent_owner,
                "execution_signer_id": self.signer,
                "status": self.consent_status,
                "confirmed_at": datetime.fromtimestamp(NOW, timezone.utc),
                "revoked_at": None,
                "message_sha256": "a"*64,
            }
        elif "FROM mininex_live_eth_policy_enrollments" in query:
            self._fetched = self.existing
        elif query.startswith("UPDATE mininex_live_eth_policy_enrollments"):
            if "status='SUBMITTING'" not in query or self.existing is None:
                raise AssertionError("Unexpected submission claim")
            if self.existing["status"] != "PREPARED":
                self._fetched = None
            else:
                self.existing["status"] = "SUBMITTING"
                self._fetched = {"status": "SUBMITTING"}
        elif query.startswith("INSERT INTO mininex_live_eth_policy_enrollments"):
            assert len(params) == 9
            (did, wallet, rev, owner, signer, digest, key, candidate_json,
             expires) = params
            assert did == DID and wallet == WALLET
            assert rev == REVISION and owner == OWNER and signer == SIGNER
            assert len(digest) == 64 and len(key) == 64
            assert '"owner_id": "'+OWNER+'"' in candidate_json
            self.insert_count += 1
            self.existing = {
                "wallet_address": wallet, "limits_revision": rev,
                "policy_owner_id": owner, "execution_signer_id": signer,
                "candidate_sha256": digest, "idempotency_key": key,
                "expires_at_unix": expires,
                "status": "PREPARED", "policy_id": None,
            }
            self._fetched = None
        else:
            raise AssertionError("Unexpected query: "+query)

    def fetchone(self):
        return self._fetched


def stage(cur, ev=None, policy=None):
    if ev is None:
        ev = evidence()
    if policy is None:
        policy = candidate(ev)
    return ledger._stage_locked(
        cur, did=DID, evidence=ev, plan=policy,
        owner=OWNER, executor=SIGNER, now=NOW,
    )


def test_signed_intent_prepares_only_one_immutable_policy_under_locks():
    cursor = Cursor()
    first = stage(cursor)
    second = stage(cursor)
    assert first == second
    assert cursor.insert_count == 1
    assert [q[0].split(" ")[0] for q in cursor.queries] == [
        "SELECT", "SELECT", "SELECT", "INSERT",
        "SELECT", "SELECT", "SELECT",
    ]
    assert all("FOR UPDATE" in statement for statement, _ in (
        cursor.queries[:3]
    ))
    assert first["status"] == "PREPARED"
    assert first["wallet_address"] == WALLET
    assert len(first["idempotency_key"]) == 64
    assert len(first["candidate_sha256"]) == 64
    assert first["policy_created_in_privy"] is False
    assert first["privy_signer_attached"] is False
    assert first["execution_enabled"] is False
    assert first["can_sign"] is False
    assert first["can_submit"] is False


@pytest.mark.parametrize("status", [
    "SUBMITTING", "UNCERTAIN", "REMOTE_VERIFIED_UNATTACHED",
    "REVOKE_REQUESTED", "REVOKED",
])
def test_prepared_policy_cannot_be_duplicated_after_any_remote_state(status):
    cursor = Cursor()
    stage(cursor)
    cursor.existing["status"] = status
    with pytest.raises(HTTPException) as error:
        stage(cursor)
    assert error.value.status_code == 409
    assert cursor.insert_count == 1


def test_changed_wallet_caps_revision_or_candidate_does_not_overwrite():
    cursor = Cursor()
    stage(cursor)
    changed = (
        ("wallet_address", OTHER),
        ("limits_revision", REVISION+1),
        ("candidate_sha256", "b"*64),
        ("idempotency_key", "c"*64),
        ("policy_owner_id", SIGNER),
        ("execution_signer_id", OWNER),
        ("expires_at_unix", NOW+10),
    )
    original = deepcopy(cursor.existing)
    for key, value in changed:
        cursor.existing = deepcopy(original)
        cursor.existing[key] = value
        with pytest.raises(HTTPException) as error:
            stage(cursor)
        assert error.value.status_code == 409
    assert cursor.insert_count == 1


@pytest.mark.parametrize("bad", [
    {"consent_status": "PENDING"},
    {"consent_status": "REVOKED"},
    {"consent_status": "UNKNOWN"},
    {"consent_owner": SIGNER},
    {"signer": OWNER},
    {"limits_rev": REVISION + 1},
])
def test_missing_or_stale_signed_consent_and_locked_live_limits_fail_closed(bad):
    cursor = Cursor()
    for key, value in bad.items():
        setattr(cursor, key, value)
    with pytest.raises(HTTPException) as error:
        stage(cursor)
    assert error.value.status_code == 409
    assert cursor.insert_count == 0


@pytest.mark.parametrize("change", [
    lambda p: p["policy"].update(owner_id=SIGNER),
    lambda p: p["policy"]["rules"].append(deepcopy(p["policy"]["rules"][0])),
    lambda p: p["policy"]["rules"][0]["conditions"].pop(0),
    lambda p: p["snapshot"].update(wallet=OTHER),
    lambda p: p["snapshot"].update(limits_revision=100),
    lambda p: p["snapshot"].update(max_order_usd="99999999"),
    lambda p: p["snapshot"].update(sha256="a"*64),
])
def test_tampered_candidate_is_refused_before_any_ledger_insert(change):
    cursor = Cursor()
    plan = candidate()
    change(plan)
    with pytest.raises((HTTPException, ValueError, TypeError)):
        stage(cursor, policy=plan)
    assert cursor.insert_count == 0


def user(did=DID, wallet=WALLET):
    return {
        "id": did,
        "linked_accounts": [{
            "id": WALLET_ID, "type": "wallet",
            "chain_type": "ethereum",
            "wallet_client_type": "privy",
            "address": wallet, "delegated": True,
        }],
    }


def metadata(*, signers=None, owner=DID, address=WALLET):
    return {
        "id": WALLET_ID,
        "chain_type": "ethereum",
        "address": address,
        "owner": {"user_id": owner},
        "additional_signers": signers if signers is not None else [],
    }


def remote_review(**updates):
    row = {"wallet_address": WALLET, "status": "REVOKE_REQUESTED"}
    row.update(updates)
    return ledger.verify_revocation_remote_absence(
        did=DID, row=row, privy_user=user(), wallet_metadata=metadata(),
    )


def test_zero_signers_independently_verified_but_never_claim_remote_action():
    report = remote_review()
    assert report["wallet_identity_verified"] is True
    assert report["remote_wallet_metadata_verified"] is True
    assert report["additional_signers_field_verified"] is True
    assert report["signer_absence_verified"] is True
    assert report["privy_policy_deleted"] is False
    assert report["remote_revocation_executed"] is False
    assert report["remote_revocation_complete"] is False
    assert report["can_sign"] is False


@pytest.mark.parametrize("tamper", [
    ("status", "PREPARED"),
    ("status", "UNCERTAIN"),
    ("wallet_address", OTHER),
    ("wallet_address", None),
])
def test_remote_absence_must_be_requested_on_verified_wallet(tamper):
    assert ledger.verify_revocation_remote_absence(
        did=DID, row={"wallet_address": WALLET, "status":"REVOKE_REQUESTED", tamper[0]:tamper[1]},
        privy_user=user(), wallet_metadata=metadata(),
    )["signer_absence_verified"] is False


@pytest.mark.parametrize("wrong", [
    metadata(signers=[{"signer_id": SIGNER,"override_policy_ids": []}]),
    metadata(signers=[{"signer_id": "unrecognized"}]),
    metadata(owner="did:privy:other-user"),
    metadata(address=OTHER),
    {**metadata(), "additional_signers": None},
    {**metadata(), "additional_signers": "[]"},
    {**metadata(), "additional_signers": [] , "id": "wrong_wallet_id"},
    {**metadata(), "additional_signers": [], "chain_type": "solana"},
    {},
    None,
])
def test_remote_revocation_refuses_signers_missing_data_or_mismatched_owner(wrong):
    review = ledger.verify_revocation_remote_absence(
        did=DID,
        row={"wallet_address": WALLET, "status":"REVOKE_REQUESTED"},
        privy_user=user(), wallet_metadata=wrong,
    )
    assert review["signer_absence_verified"] is False
    assert review["remote_revocation_complete"] is False


def test_local_record_never_converts_into_policy_or_delegation_rights():
    for status in (
        "PREPARED", "SUBMITTING", "UNCERTAIN", "REMOTE_VERIFIED_UNATTACHED",
        "REVOKE_REQUESTED", "REVOKED",
    ):
        row = {
            "status": status, "wallet_address": WALLET,
            "limits_revision": REVISION, "policy_id": "mock_policy_stage21",
        }
        snapshot = ledger._snapshot(row, current_revision=REVISION)
        assert snapshot["execution_enabled"] is False
        assert snapshot["policy_created_in_privy"] is False
        assert snapshot["user_privy_delegation_verified"] is False
        assert snapshot["can_sign"] is False
        assert snapshot["can_submit"] is False


def test_no_implicit_recovery_of_uncertain_remote_creations():
    states = ["SUBMITTING", "UNCERTAIN", "REVOKE_REQUESTED"]
    for status in states:
        assert ledger._snapshot({"status": status})["reconciliation_required"] is True
    assert transport._REMOTE_PROVISIONING_ACTIVATED is False
    with pytest.raises(HTTPException) as error:
        transport.provision_eth_privy_policy(did=DID)
    assert error.value.status_code == 503


@pytest.fixture
def auth(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "stage21-signed-session", "iat": now,
        "exp": now+600,
    }, private, algorithm="ES256")
    return {"Authorization": "Bearer "+token}


def test_prepare_route_only_authenticates_did_and_never_signs(auth, monkeypatch):
    async def fake_prepare(did):
        assert did == DID
        return ledger._snapshot({"status":"PREPARED", "wallet_address":WALLET})
    monkeypatch.setattr(api, "prepare_eth_policy_record", fake_prepare)
    client = TestClient(api.app)
    path = "/api/live/eth/policy/prepare"
    assert client.post(path).status_code == 401
    assert client.post(path,headers={"Authorization":"Bearer invalid"}).status_code == 401
    response = client.post(path,headers=auth)
    assert response.status_code == 200
    assert response.json()["execution_enabled"] is False
    assert response.json()["policy_created_in_privy"] is False
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get(path,headers=auth).status_code == 405
    assert client.post(path,headers=auth,json={"wallet_address":OTHER}).status_code == 200


def test_status_and_remote_review_routes_do_not_mutate_permissions(auth, monkeypatch):
    def fake_status(did):
        assert did == DID
        return ledger._snapshot({
            "wallet_address": WALLET,
            "status": "REVOKE_REQUESTED",
        }, current_revision=7)
    async def fake_review(did):
        assert did == DID
        return remote_review()
    monkeypatch.setattr(api, "get_eth_policy_ledger_status", fake_status)
    monkeypatch.setattr(api, "get_eth_policy_remote_revocation_review", fake_review)
    client = TestClient(api.app)
    for route in ("/api/live/eth/policy/lifecycle",
                  "/api/live/eth/policy/revocation-review"):
        assert client.get(route).status_code == 401
        response = client.get(route,headers=auth)
        assert response.status_code == 200
        assert response.json()["execution_enabled"] is False
        assert response.headers["cache-control"] == "private, no-store"
        assert client.post(route,headers=auth).status_code == 405


def test_privy_consent_revoke_is_atomic_local_only_and_marks_remote_pending(
    monkeypatch,
):
    class Cur:
        def __init__(self):
            self.queries = []
            self.last = None
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, sql, params):
            q = " ".join(sql.split())
            self.queries.append((q, params))
            if q.startswith("UPDATE mininex_live_eth_limits"):
                self.last = {"revision": REVISION+1}
            elif q.startswith("UPDATE mininex_live_eth_consent_intents"):
                self.last = None
            elif q.startswith("UPDATE mininex_live_eth_policy_enrollments"):
                assert "status='REVOKE_REQUESTED'" in q
                self.last = {"status": "REVOKE_REQUESTED"}
            else:
                raise AssertionError("Unexpected local revoke SQL")
        def fetchone(self):
            return self.last
    cur = Cur()
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def cursor(self, **kwargs):
            return cur
    monkeypatch.setattr(consent, "_db_url", lambda: "postgresql://test/never-used")
    monkeypatch.setattr(consent, "_ensure_schema", lambda _url: None)
    monkeypatch.setattr(consent, "_prepare_rows", lambda *_args: None)
    monkeypatch.setattr(
        consent.psycopg, "connect",
        lambda url, connect_timeout=6: Connection(),
    )
    result = consent.revoke_eth_consent_intent(DID)
    assert result["mininex_live_sends_paused"] is True
    assert result["local_consent_intent_revoked"] is True
    assert result["privy_policy_ledger_revoke_requested"] is True
    assert result["privy_remote_signer_removed"] is False
    assert len(cur.queries) == 3
    assert all(params == (DID,) for _,params in cur.queries)
    # Prove no remote wallet/policy DELETE or wallet grant handler is exposed.
    paths = {route.path for route in api.app.routes}
    assert "/api/live/eth/policy/prepare" in paths
    assert "/api/live/eth/policy/revocation-review" in paths
    assert "/api/live/eth/create-policy" not in paths
    assert "/api/live/eth/attach-signer" not in paths
    assert "/api/live/eth/delegate" not in paths
    assert "/api/live/eth/execute" not in paths


def test_future_worker_single_attempt_reservation_cannot_repeat_after_crash():
    cursor = Cursor()
    stage(cursor)
    key = cursor.existing["idempotency_key"]
    claim = ledger._reserve_one_remote_attempt_locked(
        cursor, did=DID, evidence=evidence(), owner=OWNER,
        executor=SIGNER, idempotency_key=key, now=NOW,
    )
    assert claim["status"] == "SUBMITTING"
    assert claim["remote_post_performed"] is False
    assert claim["remote_post_must_not_retry_after_uncertainty"] is True
    assert cursor.existing["status"] == "SUBMITTING"
    assert claim["execution_enabled"] is False
    with pytest.raises(HTTPException) as error:
        ledger._reserve_one_remote_attempt_locked(
            cursor, did=DID, evidence=evidence(), owner=OWNER,
            executor=SIGNER, idempotency_key=key, now=NOW,
        )
    assert error.value.status_code == 409
    assert cursor.insert_count == 1
    assert len([
        q for q,_ in cursor.queries
        if q.startswith("UPDATE mininex_live_eth_policy_enrollments")
    ]) == 1


@pytest.mark.parametrize("tamper", [
    ("key", "c"*64), ("expires_at_unix", NOW),
    ("status", "UNCERTAIN"), ("status", "REVOKE_REQUESTED"),
    ("limits_revision", REVISION+1),
    ("wallet_address", OTHER), ("policy_owner_id", SIGNER),
    ("execution_signer_id", OWNER),
])
def test_future_remote_attempt_requires_fresh_identical_prepared_record(tamper):
    cursor = Cursor()
    stage(cursor)
    key = cursor.existing["idempotency_key"]
    if tamper[0] == "key":
        key = tamper[1]
    else:
        cursor.existing[tamper[0]] = tamper[1]
    with pytest.raises(HTTPException) as error:
        ledger._reserve_one_remote_attempt_locked(
            cursor, did=DID, evidence=evidence(), owner=OWNER,
            executor=SIGNER, idempotency_key=key, now=NOW,
        )
    assert error.value.status_code == 409
    assert not any(
        q.startswith("UPDATE mininex_live_eth_policy_enrollments")
        for q,_ in cursor.queries
    )


def test_repeated_prepare_reuses_verified_original_without_new_price_snapshot():
    cursor = Cursor()
    original = stage(cursor)
    reused = ledger._reuse_prepared_locked(
        cursor, did=DID, evidence=evidence(),
        owner=OWNER, executor=SIGNER, now=NOW+15,
    )
    assert reused == original
    assert cursor.insert_count == 1
    assert all(
        "FOR UPDATE" in q for q,_ in cursor.queries[-3:]
    )


@pytest.mark.parametrize("change", [
    ("status", "SUBMITTING"),
    ("status", "UNCERTAIN"),
    ("status", "REVOKE_REQUESTED"),
    ("policy_id", "privy_mock_policy"),
    ("wallet_address", OTHER),
    ("limits_revision", REVISION+1),
    ("policy_owner_id", SIGNER),
    ("execution_signer_id", OWNER),
    ("expires_at_unix", NOW),
    ("candidate_sha256", "wrong"),
    ("idempotency_key", "wrong"),
])
def test_reuse_rejects_stale_expired_conflicted_or_remote_policy(change):
    cursor = Cursor()
    stage(cursor)
    cursor.existing[change[0]] = change[1]
    with pytest.raises(HTTPException) as error:
        ledger._reuse_prepared_locked(
            cursor, did=DID, evidence=evidence(),
            owner=OWNER, executor=SIGNER, now=NOW+20,
        )
    assert error.value.status_code == 409
    assert cursor.insert_count == 1


def test_new_price_is_not_fetched_for_already_stored_consent_gated_record(
    monkeypatch,
):
    row = ledger._snapshot({
        "status": "PREPARED", "wallet_address": WALLET,
        "limits_revision": REVISION, "idempotency_key": "a"*64,
        "candidate_sha256": "b"*64,
    }, current_revision=REVISION)
    calls = []
    async def mock_preflight(did):
        assert did == DID
        calls.append("wallet")
        return evidence()
    def mock_reuse(did, preflight, owner, executor, now):
        assert did == DID and preflight["verified_wallet_address"] == WALLET
        assert owner == OWNER and executor == SIGNER
        calls.append("db_locked_reuse")
        return row
    async def forbid_price():
        raise AssertionError("Existing stored policy must not depend on price churn")
    monkeypatch.setattr(ledger, "get_eth_user_enrollment_preflight", mock_preflight)
    monkeypatch.setattr(ledger, "_require_policy_roles", lambda: (OWNER, SIGNER))
    monkeypatch.setattr(ledger, "_load_existing_prepared", mock_reuse)
    monkeypatch.setattr(ledger, "fetch_reference_prices", forbid_price)
    assert asyncio.run(ledger.prepare_eth_policy_record(DID)) == row
    assert calls == ["wallet", "db_locked_reuse"]


def test_expired_privy_policy_is_reported_inert_without_grant_permission(monkeypatch):
    monkeypatch.setattr(ledger.time,"time",lambda:1900000010)
    expired=ledger._snapshot({
        "status":"GRANT_VERIFIED","policy_id":"policy_review_123",
        "expires_at_unix":1900000001,
    },current_revision=8)
    assert expired["remote_policy_recorded"] is True
    assert expired["policy_expired"] is True
    assert expired["policy_seconds_remaining"]==0
    assert expired["execution_enabled"] is False
    future=ledger._snapshot({
        "status":"REMOTE_VERIFIED_UNATTACHED","policy_id":"policy_review_123",
        "expires_at_unix":1900000045,
    })
    assert future["policy_expired"] is False
    assert future["policy_seconds_remaining"]==35
    assert future["remote_policy_recorded"] is True
    assert ledger._snapshot({"status":"PREPARED"})["remote_policy_recorded"] is False
