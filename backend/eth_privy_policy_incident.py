"""MiniNex ETH Privy policy incident review (stages 41-48).

This is strictly user-authenticated, read-only incident triage. The review
never retries a Privy POST, changes a policy status, adds/removes signers,
deletes a policy, or signs/submits a transaction.

An HTTP GET 404/timeout or a missing policy_id in an uncertain POST must NOT
be interpreted as proof the remote policy was not created. Such outcomes
require human reconciliation before another attempt. A stored grant alone
never implies the signer is currently attached or revocation succeeded.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import re
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_policy_transport import verify_remote_privy_policy
from .eth_privy_user_grant import _fetch_policy
from .eth_privy_signer_revocation import get_privy_wallet_signer_absence
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.eth_privy_incident")
_REMOTE_REVIEW = {"REMOTE_VERIFIED_UNATTACHED", "GRANT_VERIFIED", "REVOKE_REQUESTED", "REVOKED"}
_ALL_STATES = {"PREPARED", "SUBMITTING", "UNCERTAIN", *_REMOTE_REVIEW}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_POLICY_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_ADDR = re.compile(r"^0x[a-f0-9]{40}$")
_RECENT_SEC = 90


def _seconds(value: object, now: int) -> int | None:
    """Treat an unavailable/naive DB timestamp as UNKNOWN, not as fresh."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    seconds = int(now - value.timestamp())
    return seconds if seconds >= 0 else None


def _stored_policy_for_incident(did: str) -> dict | None:
    """One consistent DB read for live revision, consent and policy state."""
    did = _safe_did(did)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """SELECT policy.wallet_address,policy.limits_revision,
                              policy.policy_owner_id,policy.execution_signer_id,
                              policy.status,policy.policy_id,policy.candidate_json,
                              policy.candidate_sha256,policy.remote_policy_sha256,
                              policy.idempotency_key,policy.created_at,
                              policy.last_attempt_at,policy.remote_verified_at,
                              extract(epoch FROM policy.policy_expires_at)::bigint
                                AS expires_at_unix,
                              live.revision AS live_revision,
                              live.trading_paused AS trading_paused,
                              consent.status AS consent_status
                       FROM mininex_live_eth_limits live
                       LEFT JOIN mininex_live_eth_policy_enrollments policy
                         ON policy.privy_did=live.privy_did
                       LEFT JOIN mininex_live_eth_consent_intents consent
                         ON consent.privy_did=live.privy_did
                       WHERE live.privy_did=%s""", (did,),
                )
                row = cur.fetchone()
                if type(row) is not dict:
                    raise HTTPException(503, "Verified ETH LIVE state unavailable")
                return row
    except psycopg.Error:
        LOG.warning("Privy policy incident ledger unavailable")
        raise HTTPException(503, "Privy policy incident review unavailable") from None


def _remote_match(row: dict, remote: object) -> bool:
    """Only exact independent Privy GET counts; never interpret a 404."""
    if (type(remote) is not dict or type(row.get("candidate_json")) is not dict
            or type(row.get("policy_id")) is not str
            or _POLICY_ID.fullmatch(row["policy_id"]) is None
            or type(row.get("candidate_sha256")) is not str
            or _SHA.fullmatch(row["candidate_sha256"]) is None
            or type(row.get("policy_owner_id")) is not str):
        return False
    try:
        verified = verify_remote_privy_policy(remote, row["candidate_json"])
    except (ValueError, TypeError, KeyError):
        return False
    return (
        verified.get("remote_exact_match") is True
        and verified.get("policy_id") == row["policy_id"]
        and verified.get("policy_sha256") == row["candidate_sha256"]
        and remote.get("owner_id") == row["policy_owner_id"]
    )


def assess_privy_policy_incident(
    *, row: dict, now: int, remote: object = None,
    signer_absence: object = None,
) -> dict:
    """Pure fail-closed triage. Never infer a missing remote policy is safe."""
    if (type(row) is not dict or type(now) is not int or now <= 0
            or type(row.get("live_revision")) is not int
            or row["live_revision"] < 0
            or type(row.get("trading_paused")) is not bool):
        raise HTTPException(503, "ETH wallet policy incident state unavailable")
    status = row.get("status") or "NOT_PREPARED"
    if status != "NOT_PREPARED" and status not in _ALL_STATES:
        raise HTTPException(503, "Unknown ETH Privy policy incident state")
    policy_id = row.get("policy_id")
    if policy_id is not None and (
        type(policy_id) is not str or _POLICY_ID.fullmatch(policy_id) is None
    ):
        raise HTTPException(503, "Malformed persisted Privy policy ID")
    if status in _REMOTE_REVIEW and policy_id is None:
        # A REVOKE_REQUESTED state may also stem from a POST that timed out:
        # the remote ID is genuinely unknown. Never assume zero policies.
        if status not in {"REVOKE_REQUESTED", "REVOKED"}:
            raise HTTPException(503, "Verified policy missing remote identity")
    elapsed = _seconds(row.get("last_attempt_at"), now)
    expires = row.get("expires_at_unix")
    if expires is not None and (type(expires) is not int or expires <= 0):
        raise HTTPException(503, "Malformed Privy policy expiry")
    if status != "NOT_PREPARED" and (
        type(row.get("wallet_address")) is not str
        or _ADDR.fullmatch(row["wallet_address"]) is None
        or type(row.get("limits_revision")) is not int
        or row["limits_revision"] < 0
    ):
        raise HTTPException(503, "Stored Privy wallet/revision invalid")

    wallet_reviewed = (
        type(signer_absence) is dict
        and signer_absence.get("mode") == "PRIVY_SIGNER_ABSENCE_REMOTE_READ_ONLY"
        and signer_absence.get("verified_wallet_address")
            == row.get("wallet_address")
        and signer_absence.get("wallet_identity_verified") is True
        and signer_absence.get("wallet_owned_by_authenticated_user") is True
        and signer_absence.get("remote_wallet_metadata_verified") is True
        and signer_absence.get("additional_signers_field_verified") is True
        and signer_absence.get("execution_enabled") is False
    )
    signers_absent = (
        wallet_reviewed and signer_absence.get("signer_absence_verified") is True
    )
    remote_verified = _remote_match(row, remote) if policy_id else False
    local_revision_matches = (
        status == "NOT_PREPARED"
        or row.get("limits_revision") == row["live_revision"]
    )
    expired = type(expires) is int and expires <= now
    blockers = ["ETH_LIVE_EXECUTION_DISABLED", "NO_AUTOMATIC_PRIVY_RETRY"]
    action = "NO_POLICY_PREPARED"

    if not row["trading_paused"]:
        blockers.append("LOCAL_ETH_STOP_NOT_VERIFIED")
    if not local_revision_matches:
        blockers.append("POLICY_LIMITS_CHANGED")
    if status == "PREPARED":
        action = "PREPARED_REVIEW"
        if expired:
            blockers.append("PREPARED_POLICY_EXPIRED")
            action = "MANUAL_RECONCILIATION_REQUIRED"
    elif status == "SUBMITTING":
        action = "MANUAL_RECONCILIATION_REQUIRED"
        blockers.append(
            "RECENT_POLICY_POST_OUTCOME_UNKNOWN"
            if elapsed is not None and elapsed <= _RECENT_SEC
            else "STALE_OR_UNKNOWN_POLICY_POST_OUTCOME"
        )
    elif status == "UNCERTAIN":
        action = "MANUAL_RECONCILIATION_REQUIRED"
        blockers.append("POLICY_POST_RESULT_UNCERTAIN")
    elif status == "REMOTE_VERIFIED_UNATTACHED":
        action = "REMOTE_POLICY_REVIEW"
        blockers.append("USER_SIGNER_GRANT_NOT_VERIFIED")
    elif status == "GRANT_VERIFIED":
        action = "RECHECK_WALLET_AUTHORITY"
        blockers.append("LOCAL_GRANT_IS_NOT_CURRENT_PRIVY_AUTHORIZATION")
    elif status == "REVOKE_REQUESTED":
        action = "REMOVE_OR_VERIFY_PRIVY_SIGNERS"
        blockers.append("REMOTE_SIGNER_REVOCATION_NOT_CONFIRMED")
    elif status == "REVOKED":
        action = "REVOKED_KEEP_STOPPED"
        blockers.append("REMOTE_POLICY_DELETE_NOT_PROVEN")

    if expires is not None and expired and status not in ("NOT_PREPARED", "REVOKED"):
        blockers.append("PRIVY_POLICY_EXPIRED")
    if status in _REMOTE_REVIEW and policy_id is not None and not remote_verified:
        blockers.append("REMOTE_POLICY_NOT_INDEPENDENTLY_VERIFIED")
        action = "MANUAL_RECONCILIATION_REQUIRED"
    if status == "REVOKE_REQUESTED" and signers_absent:
        action = "CONFIRM_LOCAL_REVOKE_AFTER_OWNER_CHECK"
        blockers.append("LOCAL_REVOKE_CONFIRMATION_REQUIRED")
    if status in ("REVOKE_REQUESTED", "REVOKED") and wallet_reviewed and not signers_absent:
        blockers.append("PRIVY_ADDITIONAL_SIGNERS_REMAIN")
    if status == "GRANT_VERIFIED" and wallet_reviewed and signers_absent:
        blockers.append("STORED_GRANT_NO_LONGER_ATTACHED")
    if status in _REMOTE_REVIEW and not wallet_reviewed:
        blockers.append("PRIVY_WALLET_SIGNERS_NOT_INDEPENDENTLY_VERIFIED")
    if status in _REMOTE_REVIEW and policy_id is None:
        blockers.append("REMOTE_POLICY_ID_UNKNOWN")
        action = "MANUAL_RECONCILIATION_REQUIRED"

    return {
        "mode": "ETH_PRIVY_INCIDENT_REVIEW_READ_ONLY",
        "asset": "ETH", "chain_id": 1,
        "status": status,
        "action_code": action,
        "last_remote_attempt_age_seconds": elapsed,
        "policy_expired": expired,
        "policy_expires_at_unix": expires,
        "policy_id": policy_id,
        "policy_digest_prefix": (
            row["candidate_sha256"][:12]
            if type(row.get("candidate_sha256")) is str
            and _SHA.fullmatch(row["candidate_sha256"]) else None
        ),
        "limits_revision_matches": local_revision_matches,
        "remote_policy_exactly_verified_now": remote_verified,
        "privy_wallet_signers_verified_now": wallet_reviewed,
        "privy_additional_signers_absent_now": signers_absent,
        "local_stop_verified": row["trading_paused"],
        "consent_status": row.get("consent_status"),
        "blockers": list(dict.fromkeys(blockers)),
        "remote_policy_deleted": False,
        "remote_policy_created_by_review": False,
        "privy_signer_changed_by_review": False,
        "automatic_retry_allowed": False,
        "safe_to_recreate_policy": False,
        "real_trading_enabled": False,
        "can_sign": False, "can_submit": False,
        "execution_enabled": False,
    }


async def get_eth_privy_policy_incident_review(did: str) -> dict:
    did = _safe_did(did)
    row = await asyncio.to_thread(_stored_policy_for_incident, did)
    status = row.get("status")
    remote = None
    signers = None
    # For known IDs, a read-only Privy GET may independently verify a
    # still-existing policy. A remote failure is NEVER treated as deletion.
    policy_id = row.get("policy_id")
    if status in _REMOTE_REVIEW and type(policy_id) is str and _POLICY_ID.fullmatch(policy_id):
        try:
            remote = await _fetch_policy(policy_id)
        except (HTTPException, ValueError, TypeError):
            remote = None
    if status in _REMOTE_REVIEW:
        try:
            signers = await get_privy_wallet_signer_absence(did)
        except (HTTPException, ValueError, TypeError):
            signers = None
    return assess_privy_policy_incident(
        row=row, now=int(time.time()), remote=remote,
        signer_absence=signers,
    )
