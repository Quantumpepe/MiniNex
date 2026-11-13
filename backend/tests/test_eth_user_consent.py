"""EIP-191 consent intents are one-use, wallet-bound and never a Privy grant."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
import hashlib
import time

from eth_account import Account
from eth_account.messages import encode_defunct
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_user_consent as consent
from backend.storage import SCHEMA

ACCOUNT = Account.create("mininex-consent-test-local-only")
OTHER = Account.create("other-user-test-local-only")
DID = "did:privy:consent-proof-123"
SESSION = "verified-sid-200001"
WALLET = ACCOUNT.address.lower()
OWNER = "mininex_owner_quorum_123"
SIGNER = "mininex_executor_signer_123"
NOW = 1900000000


def evidence(wallet=WALLET, cap="5000", order="5000", daily="15000",
             revision=4, **changes):
    base = {
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
        "live_capital_usd": cap,
        "live_max_order_usd": order,
        "live_daily_turnover_usd": daily,
    }
    base.update(changes)
    return base


def sign(message, account=ACCOUNT):
    sig = Account.sign_message(
        encode_defunct(text=message), private_key=account.key,
    ).signature.hex()
    return "0x" + sig.removeprefix("0x")


class Cursor:
    """Simulated row-locked PostgreSQL statements, not proof of DB semantics."""
    def __init__(self):
        self.controls = {
            "planned_capital_usd": Decimal("5000"),
            "max_order_usd": Decimal("5000"),
            "max_daily_turnover_usd": Decimal("15000"),
            "trading_paused": True,
            "revision": 4,
        }
        self.intent = None
        self.result = None
        self.queries = []

    def execute(self, sql, params):
        compact = " ".join(sql.split())
        self.queries.append((compact, params))
        if "FROM mininex_live_eth_limits" in compact:
            assert "FOR UPDATE" in compact
            assert params == (DID,)
            self.result = deepcopy(self.controls)
        elif compact.startswith("INSERT INTO mininex_live_eth_consent_intents"):
            assert "ON CONFLICT (privy_did)" in compact
            self.intent = {
                "wallet_address": params[1],
                "session_sha256": params[2],
                "limits_revision": params[3],
                "policy_owner_id": params[4],
                "execution_signer_id": params[5],
                "message_text": params[6],
                "message_sha256": params[7],
                "status": "PENDING",
                "expires_at_unix": params[9],
            }
            self.result = None
        elif "FROM mininex_live_eth_consent_intents" in compact:
            assert "FOR UPDATE" in compact
            assert params == (DID,)
            self.result = deepcopy(self.intent)
        elif compact.startswith("UPDATE mininex_live_eth_consent_intents"):
            assert params == (DID,)
            if self.intent is not None:
                self.intent["status"] = "SIGNED_INTENT"
            self.result = None
        else:
            raise AssertionError("Unexpected consent SQL: " + compact[:130])

    def fetchone(self):
        return self.result


def issue(cur, **kwargs):
    e = consent._require_enrollment_evidence(evidence(**kwargs))
    did, session_hash = consent._require_identity(DID, SESSION)
    return consent._begin_locked(
        cur, did=did, session_digest=session_hash, evidence=e,
        owner=OWNER, signer=SIGNER, now=NOW,
        nonce="b" * 64,
    )


def confirm(cur, signature, **changes):
    return consent._confirm_locked(
        cur, did=DID,
        session_digest=changes.get("session_digest",
                                   hashlib.sha256(SESSION.encode()).hexdigest()),
        evidence=consent._require_enrollment_evidence(
            changes.get("evidence", evidence())),
        signature=signature, now=changes.get("now", NOW+5),
        owner=changes.get("owner", OWNER),
        signer=changes.get("signer", SIGNER),
    )


def test_signed_intent_exact_wallet_limits_and_action_scope_not_live_grant():
    cur = Cursor()
    pending = issue(cur)
    message = pending["challenge"]
    assert pending["status"] == "PENDING"
    assert pending["wallet"] == WALLET
    assert pending["expires_at_unix"] == NOW+300
    assert "Application: " + consent.APP_ID in message
    assert f"Wallet: {WALLET}" in message
    assert "Planned capital USD: 5000" in message
    assert "Maximum order USD: 5000" in message
    assert "Maximum daily turnover USD: 15000" in message
    assert "Nonce: " + "b"*64 in message
    assert "This signature records my enrollment request ONLY." in message
    assert "A separate explicit Privy wallet approval will be required." in message
    assert consent.SWAPROUTER02_MAINNET in message
    assert consent.WETH_MAINNET in message
    assert pending["execution_enabled"] is False
    assert pending["user_grant_created"] is False
    assert pending["can_submit"] is False

    signature = sign(message)
    assert consent.verify_eoa_eip191_signature(
        message=message, signature=signature, wallet=WALLET
    ) is True
    proof = confirm(cur, signature)
    assert proof["status"] == "SIGNED_INTENT"
    assert cur.intent["status"] == "SIGNED_INTENT"
    assert proof["user_intent_signature_verified"] is True
    assert proof["user_privy_delegation_granted"] is False
    assert proof["live_signing_approved"] is False
    assert proof["can_submit"] is False
    assert proof["execution_enabled"] is False
    assert [x for x, _ in cur.queries if "FOR UPDATE" in x]
    with pytest.raises(HTTPException) as err:
        confirm(cur, signature)
    assert err.value.status_code == 409


@pytest.mark.parametrize("mutation", [
    lambda c: setattr(c, "intent", None),
    lambda c: c.intent.update(status="REVOKED"),
    lambda c: c.intent.update(wallet_address=OTHER.address.lower()),
    lambda c: c.intent.update(limits_revision=99),
    lambda c: c.intent.update(session_sha256="0" * 64),
    lambda c: c.intent.update(policy_owner_id="changed_owner_123"),
    lambda c: c.intent.update(execution_signer_id="changed_signer_123"),
    lambda c: c.intent.update(message_sha256="f" * 64),
    lambda c: c.intent.update(expires_at_unix=NOW+1),
])
def test_stale_replayed_or_tampered_stored_challenge_is_rejected(mutation):
    cur = Cursor()
    challenge = issue(cur)["challenge"]
    signature = sign(challenge)
    mutation(cur)
    with pytest.raises(HTTPException) as err:
        confirm(cur, signature, now=NOW+5)
    assert err.value.status_code == 409


def test_signature_does_not_accept_wrong_eoa_or_modified_message():
    cur = Cursor()
    message = issue(cur)["challenge"]
    wrong_signature = sign(message, OTHER)
    assert consent.verify_eoa_eip191_signature(
        message=message, signature=wrong_signature, wallet=WALLET
    ) is False
    with pytest.raises(HTTPException) as err:
        confirm(cur, wrong_signature)
    assert err.value.status_code == 403
    assert cur.intent["status"] == "PENDING"
    assert consent.verify_eoa_eip191_signature(
        message=message+"extra", signature=sign(message),
        wallet=WALLET,
    ) is False
    assert consent.verify_eoa_eip191_signature(
        message=message, signature="0x"+"00"*65, wallet=WALLET
    ) is False


@pytest.mark.parametrize("case", [
    {"trading_paused": False},
    {"live_limits_nonzero": False},
    {"unrecognized_existing_signers": True},
    {"policy_owner_independent": False},
    {"wallet_identity_verified": False},
    {"wallet_additional_signers_reviewed": False},
    {"live_limits_revision": True},
    {"live_max_order_usd": "99999"},
    {"live_capital_usd": "0"},
    {"verified_wallet_address": "0x1"},
])
def test_missing_wallet_grant_conditions_and_limits_fail_before_persistence(case):
    with pytest.raises(HTTPException) as err:
        consent._require_enrollment_evidence(evidence(**case))
    assert err.value.status_code == 409


def test_changed_live_limits_revision_and_paused_state_block_stale_signature():
    cur = Cursor()
    message = issue(cur)["challenge"]
    cur.controls["revision"] = 5
    with pytest.raises(HTTPException):
        confirm(cur, sign(message))
    cur.controls["revision"] = 4
    cur.controls["trading_paused"] = False
    with pytest.raises(HTTPException):
        confirm(cur, sign(message))
    cur.controls["trading_paused"] = True
    cur.controls["max_order_usd"] = Decimal("4000")
    with pytest.raises(HTTPException):
        confirm(cur, sign(message))


def test_new_challenge_invalidates_previous_signature_and_nonce():
    cur = Cursor()
    old = issue(cur)["challenge"]
    # Next challenge replaces the old row under the same account lock.
    did, session_hash = consent._require_identity(DID, SESSION)
    new = consent._begin_locked(
        cur, did=did, session_digest=session_hash,
        evidence=consent._require_enrollment_evidence(evidence()),
        owner=OWNER, signer=SIGNER, now=NOW+1, nonce="c"*64,
    )["challenge"]
    assert old != new
    assert cur.intent["status"] == "PENDING"
    with pytest.raises(HTTPException) as err:
        confirm(cur, sign(old))
    assert err.value.status_code == 403
    assert confirm(cur, sign(new))["status"] == "SIGNED_INTENT"


def test_schema_records_expiry_nonce_replay_status_and_grant_separation():
    ddl = "\n".join(SCHEMA)
    assert "mininex_live_eth_consent_intents" in ddl
    assert "CHECK (expires_at <= issued_at + interval '5 minutes')" in ddl
    assert "status IN ('PENDING','SIGNED_INTENT','REVOKED')" in ddl
    assert "session_sha256" in ddl and "message_sha256" in ddl
    assert "policy_owner_id <> execution_signer_id" in ddl
    assert "trading_paused BOOLEAN NOT NULL DEFAULT TRUE" in ddl
    assert "user_granted" not in ddl


@pytest.fixture
def auth_headers(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": SESSION,
        "iat": now, "exp": now+450,
    }, key, algorithm="ES256")
    return {"Authorization": "Bearer "+token}


def test_consent_routes_accept_only_user_jwt_and_never_enable_trading(
    monkeypatch, auth_headers,
):
    seen = []
    async def prepared(did, sid):
        seen.append(("challenge", did, sid))
        return {"status": "PENDING", "can_submit": False, "execution_enabled": False}
    async def confirmed(did, sid, signature):
        seen.append(("confirm", did, sid, signature))
        return {"status": "SIGNED_INTENT", "execution_enabled": False}
    def revoked(did):
        seen.append(("revoke", did))
        return {"status": "REVOKED_LOCALLY", "execution_enabled": False,
                "privy_remote_signer_removed": False}
    monkeypatch.setattr(api, "prepare_eth_consent_challenge", prepared)
    monkeypatch.setattr(api, "confirm_eth_consent_intent", confirmed)
    monkeypatch.setattr(api, "revoke_eth_consent_intent", revoked)
    client = TestClient(api.app)
    base = "/api/live/eth/consent"
    assert client.post(base+"/challenge").status_code == 401
    assert client.post(base+"/confirm", json={"signature": "0x"+"a"*130}).status_code == 401
    assert client.post(base+"/revoke").status_code == 401
    challenge = client.post(base+"/challenge", headers=auth_headers)
    assert challenge.status_code == 200
    assert challenge.headers["cache-control"] == "private, no-store"
    assert challenge.headers["x-content-type-options"] == "nosniff"
    assert challenge.json()["execution_enabled"] is False
    assert client.post(base+"/challenge", headers=auth_headers,
                       json={"wallet": OTHER.address}).status_code == 200
    # The handler accepts no browser wallet, but without a Pydantic body
    # FastAPI ignores the unexpected JSON; it remains non-authoritative.
    assert client.post(base+"/confirm", headers=auth_headers, json={
        "signature": "not-a-signature",
    }).status_code == 422
    assert client.post(base+"/confirm", headers=auth_headers, json={
        "signature": "0x"+"a"*130, "wallet": OTHER.address,
    }).status_code == 422
    result = client.post(base+"/confirm", headers=auth_headers, json={
        "signature": "0x"+"a"*130,
    })
    assert result.status_code == 200
    assert result.json()["execution_enabled"] is False
    stop = client.post(base+"/revoke", headers=auth_headers)
    assert stop.status_code == 200
    assert stop.json()["privy_remote_signer_removed"] is False
    assert seen[0] == ("challenge", DID, SESSION)
    assert seen[-1] == ("revoke", DID)
    assert client.get(base+"/confirm", headers=auth_headers).status_code == 405
    assert client.put(base+"/revoke", headers=auth_headers).status_code == 405
    assert not any(r.path == "/api/live/eth/grant" for r in api.app.routes)
    assert not any(r.path == "/api/live/eth/execute" for r in api.app.routes)
