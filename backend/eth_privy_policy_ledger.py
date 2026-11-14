"""Stage 21: durable MiniNex ETH Privy policy staging and revocation review.

No route or worker creates a real Privy policy, adds an additional signer,
deletes a remote policy, submits calldata, or enables live trading.

A user's EIP-191 enrollment *intent* authorizes ONLY storing a PREPARED
record. Creating a wallet-scoped Privy delegation still needs a separate
Privy wallet-owner grant and independent remote verification.

Unlike the per-request preview, this module keeps a durable, one-per-DID
candidate and unique idempotency key in PostgreSQL. Conflicts, uncertain
POSTs, prior revocations or changed wallet/limits are NOT blindly retried
or replaced with a new policy. Revocation starts by stopping MiniNex and
marking REVOKE_REQUESTED, never falsely claims that Privy access ended.
"""
from __future__ import annotations

from datetime import datetime
import asyncio
import json
import logging
import re
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_user_consent import (
    _check_locked_limits, _require_enrollment_evidence,
    _require_policy_roles,
)
from .eth_user_enrollment_preflight import get_eth_user_enrollment_preflight
from .eth_privy_policy_plan import (
    build_verified_eth_policy_candidate, fetch_reference_prices,
)
from .eth_privy_policy_transport import _validate_candidate
from .privy_live_signer import _owned_eth_wallet, fetch_privy_wallet_metadata
from .privy_wallet_verification import fetch_privy_user
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.eth_policy_ledger")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REMOTE_VERIFIED = "REMOTE_VERIFIED_UNATTACHED"
_REVOKE_REQUESTED = "REVOKE_REQUESTED"
_LIVE_DISABLED = {
    "policy_created_in_privy": False,
    "privy_signer_attached": False,
    "privy_remote_signer_removed": False,
    "remote_policy_deleted": False,
    "user_privy_delegation_verified": False,
    "remote_revocation_implemented": False,
    "execution_enabled": False,
    "can_grant_permission": False,
    "can_sign": False,
    "can_submit": False,
}


def _snapshot(row: object, *, current_revision: object = None) -> dict:
    """Only inert state and public IDs; no policy bytes or wallet authority."""
    base = {
        "mode": "ETH_PRIVY_POLICY_DURABLE_PREPARATION_ONLY",
        "asset": "ETH", "chain_id": 1,
        "status": "NOT_PREPARED",
        "policy_id": None,
        "candidate_sha256": None,
        "idempotency_key": None,
        "wallet_address": None,
        "limits_revision": None,
        "current_live_limits_revision": current_revision,
        "live_limits_revision_matches": False,
        "reconciliation_required": False,
        "privy_remote_revocation_requested": False,
        **_LIVE_DISABLED,
    }
    if type(row) is not dict:
        return base
    status = row.get("status")
    if status not in {
        "PREPARED", "SUBMITTING", "UNCERTAIN",
        _REMOTE_VERIFIED, _REVOKE_REQUESTED, "REVOKED",
    }:
        raise HTTPException(503, "Policy ledger state invalid")
    base.update({
        "status": status,
        "policy_id": row.get("policy_id"),
        "candidate_sha256": row.get("candidate_sha256"),
        "idempotency_key": row.get("idempotency_key"),
        "wallet_address": row.get("wallet_address"),
        "limits_revision": row.get("limits_revision"),
        "live_limits_revision_matches": (
            type(current_revision) is int
            and row.get("limits_revision") == current_revision),
        "reconciliation_required": status in {
            "SUBMITTING", "UNCERTAIN", _REVOKE_REQUESTED,
        },
        "privy_remote_revocation_requested": status == _REVOKE_REQUESTED,
    })
    # This is an inert local database record: even an independent GET or a
    # recorded policy ID does not prove an actual wallet grant.
    return base


def _require_signed_intent(cur, *, did: str, values: dict, owner: str,
                           executor: str) -> None:
    """Consent row is independently locked and confirmed by Stage 17."""
    cur.execute(
        """SELECT wallet_address,limits_revision,policy_owner_id,
                  execution_signer_id,status,confirmed_at,revoked_at,
                  message_sha256
           FROM mininex_live_eth_consent_intents
           WHERE privy_did=%s FOR UPDATE""", (did,),
    )
    row = cur.fetchone()
    if (type(row) is not dict or row.get("status") != "SIGNED_INTENT"
            or row.get("wallet_address") != values["wallet"]
            or row.get("limits_revision") != values["revision"]
            or row.get("policy_owner_id") != owner
            or row.get("execution_signer_id") != executor
            or not isinstance(row.get("confirmed_at"), datetime)
            or row.get("revoked_at") is not None
            or type(row.get("message_sha256")) is not str
            or _SHA.fullmatch(row["message_sha256"]) is None):
        raise HTTPException(409, "A fresh matching signed consent intent is required")


def _stage_locked(
    cur, *, did: str, evidence: dict, plan: dict,
    owner: str, executor: str, now: int,
) -> dict:
    """One atomic DB transaction under live-limits, consent, policy locks.

    An initial insert is the ONLY supported state change. Every subsequent
    *identical* prepare request returns the original row; all conflicting
    wallet, revision, policy bytes, or revoke states must be reconciled.
    """
    values = _require_enrollment_evidence(evidence)
    _check_locked_limits(cur, did, values)
    _require_signed_intent(
        cur, did=did, values=values, owner=owner, executor=executor,
    )
    if type(plan) is not dict or type(plan.get("policy")) is not dict:
        raise HTTPException(409, "Audited policy candidate missing")
    info = plan.get("snapshot")
    if (type(info) is not dict
            or info.get("wallet") != values["wallet"]
            or info.get("limits_revision") != values["revision"]
            or info.get("capital_usd") != values["capital"]
            or info.get("max_order_usd") != values["order"]
            or info.get("max_daily_turnover_usd") != values["daily"]
            or type(info.get("caps")) is not dict
            or type(info.get("sha256")) is not str
            or _SHA.fullmatch(info["sha256"]) is None
            or type(info.get("expires_at_unix")) is not int
            or info["expires_at_unix"] <= now):
        raise HTTPException(409, "Policy candidate and live limits mismatch")
    caps = info["caps"]
    prepared = _validate_candidate(
        plan["policy"], did=did, wallet=values["wallet"],
        limits_revision=values["revision"], policy_owner_id=owner,
        executor_signer_id=executor,
        expires_at=info["expires_at_unix"], now=now,
        **caps,
    )
    if prepared["candidate_sha256"] != info["sha256"]:
        raise HTTPException(409, "Policy candidate digest mismatch")

    cur.execute(
        """SELECT wallet_address,limits_revision,policy_owner_id,
                  execution_signer_id,candidate_sha256,idempotency_key,
                  status,policy_id,extract(epoch FROM policy_expires_at)::bigint
                      AS expires_at_unix
           FROM mininex_live_eth_policy_enrollments
           WHERE privy_did=%s FOR UPDATE""", (did,),
    )
    row = cur.fetchone()
    if type(row) is dict:
        same = (
            row.get("wallet_address") == values["wallet"]
            and row.get("limits_revision") == values["revision"]
            and row.get("policy_owner_id") == owner
            and row.get("execution_signer_id") == executor
            and row.get("candidate_sha256") == info["sha256"]
            and row.get("idempotency_key") == prepared["idempotency_key"]
            and row.get("expires_at_unix") == info["expires_at_unix"]
        )
        if not same or row.get("status") != "PREPARED" or row.get("policy_id") is not None:
            raise HTTPException(
                409, "Existing Privy policy must be independently reconciled before any new preparation",
            )
        return _snapshot(row, current_revision=values["revision"])

    cur.execute(
        """INSERT INTO mininex_live_eth_policy_enrollments
              (privy_did,wallet_address,limits_revision,policy_owner_id,
               execution_signer_id,candidate_sha256,idempotency_key,
               candidate_json,policy_expires_at,status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,to_timestamp(%s),'PREPARED')""",
        (
            did, values["wallet"], values["revision"], owner, executor,
            info["sha256"], prepared["idempotency_key"],
            json.dumps(prepared["candidate"], sort_keys=True),
            info["expires_at_unix"],
        ),
    )
    return _snapshot({
        "wallet_address": values["wallet"],
        "limits_revision": values["revision"],
        "candidate_sha256": info["sha256"],
        "idempotency_key": prepared["idempotency_key"],
        "status": "PREPARED",
    }, current_revision=values["revision"])


def _store_prepared(did: str, evidence: dict, plan: dict,
                    owner: str, executor: str, now: int) -> dict:
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                return _stage_locked(
                    cur, did=did, evidence=evidence, plan=plan,
                    owner=owner, executor=executor, now=now,
                )
    except psycopg.Error:
        LOG.warning("ETH Privy policy ledger unavailable")
        raise HTTPException(503, "Policy preparation storage unavailable") from None


async def prepare_eth_policy_record(did: str) -> dict:
    """Authenticated *user-initiated* local prepare. NO Privy write or grant."""
    did = _safe_did(did)
    evidence = await get_eth_user_enrollment_preflight(did)
    _require_enrollment_evidence(evidence)
    owner, executor = _require_policy_roles()
    # Fetch from a fixed independent HTTPS source; never browser prices.
    prices = await fetch_reference_prices()
    now = int(time.time())
    plan = build_verified_eth_policy_candidate(
        evidence=evidence, snapshot=prices, owner=owner,
        executor=executor, now=now,
    )
    return await asyncio.to_thread(
        _store_prepared, did, evidence, plan, owner, executor, now,
    )


def get_eth_policy_ledger_status(did: str) -> dict:
    """Authenticated read of persistent state; never accesses Privy writes."""
    did = _safe_did(did)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """SELECT policy.wallet_address,policy.limits_revision,
                              policy.candidate_sha256,policy.idempotency_key,
                              policy.status,policy.policy_id,
                              live.revision AS live_revision
                       FROM mininex_live_eth_policy_enrollments policy
                       JOIN mininex_live_eth_limits live
                         ON live.privy_did=policy.privy_did
                       WHERE policy.privy_did=%s""", (did,),
                )
                row = cur.fetchone()
                if type(row) is not dict:
                    return _snapshot(None)
                return _snapshot(row, current_revision=row.get("live_revision"))
    except psycopg.Error:
        LOG.warning("ETH Privy policy ledger read unavailable")
        raise HTTPException(503, "Policy state unavailable") from None


def verify_revocation_remote_absence(
    *, did: str, row: dict, privy_user: object, wallet_metadata: object,
) -> dict:
    """Read-only independent evidence whether THIS wallet has any signers.

    Absence of signers is never inferred from a local stopped flag,
    deleted consent, old policy expiry or a missing metadata field.
    """
    result = {
        "mode": "ETH_PRIVY_REMOTE_REVOCATION_REVIEW_ONLY",
        "status": row.get("status") if type(row) is dict else None,
        "wallet_identity_verified": False,
        "remote_wallet_metadata_verified": False,
        "additional_signers_field_verified": False,
        "signer_absence_verified": False,
        "privy_policy_deleted": False,
        "remote_revocation_executed": False,
        "remote_revocation_complete": False,
        "can_sign": False, "can_submit": False,
        "execution_enabled": False,
    }
    if (type(row) is not dict
            or row.get("status") not in {_REVOKE_REQUESTED, "REVOKED"}
            or row.get("wallet_address") is None):
        return result
    try:
        owned = _owned_eth_wallet(privy_user, did)
    except ValueError:
        return result
    if owned["address"] != row["wallet_address"]:
        return result
    result["wallet_identity_verified"] = True
    if (type(wallet_metadata) is not dict
            or wallet_metadata.get("id") != owned["wallet_id"]
            or wallet_metadata.get("chain_type") != "ethereum"
            or str(wallet_metadata.get("address", "")).lower() != owned["address"]
            or type(wallet_metadata.get("owner")) is not dict
            or wallet_metadata["owner"].get("user_id") != did):
        return result
    result["remote_wallet_metadata_verified"] = True
    signers = wallet_metadata.get("additional_signers")
    if type(signers) is not list:
        return result
    result["additional_signers_field_verified"] = True
    # For safety we only call it absent if there are absolutely no additional
    # signers, including unrelated existing ones; never guess per-signer IDs.
    result["signer_absence_verified"] = len(signers) == 0
    # Signer absence is a read-only fact, not proof we actually executed a
    # remote DELETE/UNLINK: the policy may still exist as an orphan.
    return result


async def get_eth_policy_remote_revocation_review(did: str) -> dict:
    did = _safe_did(did)
    row = await asyncio.to_thread(get_eth_policy_ledger_status, did)
    if row.get("status") not in {_REVOKE_REQUESTED, "REVOKED"}:
        return verify_revocation_remote_absence(
            did=did, row=row, privy_user=None, wallet_metadata=None,
        )
    user = await fetch_privy_user(did)
    try:
        owned = _owned_eth_wallet(user, did)
    except ValueError:
        return verify_revocation_remote_absence(
            did=did, row=row, privy_user=user, wallet_metadata=None,
        )
    metadata = await fetch_privy_wallet_metadata(owned["wallet_id"])
    return verify_revocation_remote_absence(
        did=did, row=row, privy_user=user, wallet_metadata=metadata,
    )
