"""Steps 23+24: one-shot Privy policy creation for a *verified consenting user*.

Creating a policy does not grant wallet access. The only POST route invoking
this module is authenticated and explicit user initiation AFTER a real
EIP-191 wallet-signed MiniNex INTENT, fresh user/wallet/limit checks and a
durably committed SUBMITTING reservation. No automatic retry of an uncertain
Privy response, no signer registration or money movement.

A policy can only be offered to the wallet owner through a separate
user-approved Privy addSigners flow, which is handled elsewhere.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_policy_ledger import (
    _require_signed_intent, _require_enrollment_evidence, _check_locked_limits,
    _require_policy_roles, _snapshot,
)
from .eth_user_enrollment_preflight import get_eth_user_enrollment_preflight
from .eth_privy_policy_plan import fetch_reference_prices, derive_policy_atomic_caps
from .eth_privy_policy_transport import (
    _validate_candidate, _post_and_readback_unattached,
)
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.eth_policy_provisioner")
_MAX_PREPARE_AGE = 300
_CAP_KEYS = (
    "usdc_max_input_micro","usdt_max_input_micro",
    "weth_max_input_wei","native_wrap_max_wei",
)


def _rule_lte(rule: dict, field: str) -> str:
    matches = [
        c.get("value")
        for c in rule.get("conditions", [])
        if type(c) is dict and c.get("field") == field
        and c.get("operator") == "lte"
    ]
    if len(matches) != 1 or type(matches[0]) is not str:
        raise HTTPException(409, "Invalid individual Privy policy ceiling")
    return matches[0]


def _extract_candidate_caps(policy: object) -> dict:
    if type(policy) is not dict or type(policy.get("rules")) is not list:
        raise HTTPException(409, "Stored policy has invalid rules")
    rules = policy["rules"]
    if len(rules) != 6 or any(type(r) is not dict for r in rules):
        raise HTTPException(409, "Privy policy must have six restricted rules")
    a = _rule_lte(rules[0], "exactInputSingle.params.amountIn")
    b = _rule_lte(rules[2], "exactInputSingle.params.amountIn")
    w = _rule_lte(rules[1], "exactInputSingle.params.amountIn")
    if w != _rule_lte(rules[3], "exactInputSingle.params.amountIn"):
        raise HTTPException(409, "Different WETH sell ceilings")
    if w != _rule_lte(rules[5], "withdraw.wad"):
        raise HTTPException(409, "Different WETH withdrawal ceilings")
    wrap = _rule_lte(rules[4], "value")
    return dict(zip(_CAP_KEYS, (a,b,w,wrap)))


def _claim_prepared_locked(
    cur, *, did: str, evidence: dict, owner: str, executor: str,
    current_caps: dict, now: int,
) -> dict:
    """Single committed claim before sending one Privy create request.

    Any uncertainty is terminal for automatic retries. No remote I/O while
    SQL locks are held. Limits changes/revocation race closes here.
    """
    values = _require_enrollment_evidence(evidence)
    _check_locked_limits(cur, did, values)
    _require_signed_intent(
        cur, did=did, values=values, owner=owner, executor=executor,
    )
    cur.execute(
        """SELECT wallet_address,limits_revision,policy_owner_id,
                  execution_signer_id,candidate_json,candidate_sha256,
                  idempotency_key,status,policy_id,
                  extract(epoch FROM policy_expires_at)::bigint AS expires_at_unix,
                  created_at
           FROM mininex_live_eth_policy_enrollments
           WHERE privy_did=%s FOR UPDATE""",
        (did,),
    )
    row = cur.fetchone()
    if (
        type(row) is not dict or row.get("status") != "PREPARED"
        or row.get("policy_id") is not None
        or row.get("wallet_address") != values["wallet"]
        or row.get("limits_revision") != values["revision"]
        or row.get("policy_owner_id") != owner
        or row.get("execution_signer_id") != executor
        or type(row.get("expires_at_unix")) is not int
        or row["expires_at_unix"] <= now
        or type(row.get("created_at")) is not datetime
        or row["created_at"].tzinfo is None
        or now - int(row["created_at"].timestamp()) > _MAX_PREPARE_AGE
        or int(row["created_at"].timestamp()) > now + 5
        or type(row.get("candidate_json")) is not dict
        or type(row.get("candidate_sha256")) is not str
        or type(row.get("idempotency_key")) is not str
    ):
        raise HTTPException(409, "Policy record missing, stale or already claimed")

    candidate = row["candidate_json"]
    old_caps = _extract_candidate_caps(candidate)
    # Do not create a policy based on a price that would now allow an order
    # exceeding its separately stored LIVE max-order value.
    if any(
        k not in current_caps or int(old_caps[k]) > int(current_caps[k])
        for k in _CAP_KEYS
    ):
        raise HTTPException(409, "Reference prices changed; restage with new consent")
    validated = _validate_candidate(
        candidate, did=did, wallet=values["wallet"],
        policy_owner_id=owner, executor_signer_id=executor,
        limits_revision=values["revision"],
        expires_at=row["expires_at_unix"], now=now, **old_caps,
    )
    if (
        validated["candidate_sha256"] != row["candidate_sha256"]
        or validated["idempotency_key"] != row["idempotency_key"]
    ):
        raise HTTPException(409, "Privy policy digest mismatch")

    cur.execute(
        """UPDATE mininex_live_eth_policy_enrollments
           SET status='SUBMITTING',last_attempt_at=now(),updated_at=now()
           WHERE privy_did=%s AND status='PREPARED'
             AND idempotency_key=%s
           RETURNING status""",
        (did, validated["idempotency_key"]),
    )
    saved = cur.fetchone()
    if type(saved) is not dict or saved.get("status") != "SUBMITTING":
        raise HTTPException(409, "Concurrent Privy submission conflict")
    return validated


def _reserve(did: str, evidence: dict, owner: str, executor: str,
             caps: dict, now: int) -> dict:
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                return _claim_prepared_locked(
                    cur, did=did, evidence=evidence, owner=owner,
                    executor=executor, current_caps=caps, now=now,
                )
        # psycopg's context manager COMMITTED the SUBMITTING row here.
    except psycopg.Error:
        LOG.warning("Privy one-shot reservation unavailable")
        raise HTTPException(503, "Privy policy reservation storage unavailable") from None


def _finish_locked(cur, *, did: str, validated: dict,
                   result: dict | None) -> dict:
    cur.execute(
        """SELECT wallet_address,limits_revision,policy_owner_id,
                  execution_signer_id,candidate_sha256,idempotency_key,status
           FROM mininex_live_eth_policy_enrollments
           WHERE privy_did=%s FOR UPDATE""",
        (did,),
    )
    row = cur.fetchone()
    if type(row) is not dict or (
        row.get("candidate_sha256") != validated["candidate_sha256"]
        or row.get("idempotency_key") != validated["idempotency_key"]
    ):
        raise HTTPException(409, "Privy policy reconciliation data mismatch")
    # A concurrent revoke may have switched state to REVOKE_REQUESTED;
    # NEVER resuscitate or overwrite the revoked wallet record.
    if row.get("status") != "SUBMITTING":
        raise HTTPException(409, "Privy policy was stopped or changed while creating")
    # A returned object is NOT trusted merely because the HTTP POST
    # completed. If its fields are malformed or do not match the exact
    # remote readback, persist UNCERTAIN instead of leaving SUBMITTING
    # indefinitely or exposing an apparent success.
    if result is not None and (
        type(result) is not dict
        or result.get("remote_exact_match") is not True
        or result.get("policy_sha256") != validated["candidate_sha256"]
        or type(result.get("policy_id")) is not str
        or len(result["policy_id"]) < 6
    ):
        result = None
    if result is None:
        cur.execute(
            """UPDATE mininex_live_eth_policy_enrollments
               SET status='UNCERTAIN',updated_at=now()
               WHERE privy_did=%s AND status='SUBMITTING'
               RETURNING status""", (did,),
        )
        changed = cur.fetchone()
        if type(changed) is not dict or changed.get("status") != "UNCERTAIN":
            raise HTTPException(409, "Cannot hold Privy policy in uncertain state")
        return {
            "status":"UNCERTAIN", "can_grant_permission":False,
            "can_sign":False, "can_submit":False, "execution_enabled":False,
        }
    if (
        result.get("remote_exact_match") is not True
        or result.get("policy_sha256") != validated["candidate_sha256"]
        or type(result.get("policy_id")) is not str
    ):
        raise HTTPException(409, "Privy remote policy not independently verified")
    cur.execute(
        """UPDATE mininex_live_eth_policy_enrollments
           SET status='REMOTE_VERIFIED_UNATTACHED',policy_id=%s,
               remote_policy_sha256=%s,remote_verified_at=now(),
               updated_at=now()
           WHERE privy_did=%s AND status='SUBMITTING'
           RETURNING status""",
        (result["policy_id"], result["policy_sha256"], did),
    )
    saved = cur.fetchone()
    if type(saved) is not dict or saved.get("status") != "REMOTE_VERIFIED_UNATTACHED":
        raise HTTPException(409, "Privy policy confirmation conflict")
    return {
        "status":"REMOTE_VERIFIED_UNATTACHED",
        "policy_id":result["policy_id"],
        "policy_contents_verified":True,
        "wallet_signer_attached":False,
        "can_grant_permission":False, "can_sign":False,
        "can_submit":False, "execution_enabled":False,
    }


def _finish(did: str, validated: dict, result: dict | None) -> dict:
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _finish_locked(cur, did=did, validated=validated,
                                      result=result)
    except psycopg.Error:
        LOG.warning("Privy policy remote status couldn't be recorded")
        raise HTTPException(
            503, "Privy policy status reconciliation required",
        ) from None


async def publish_user_eth_privy_policy(did: str) -> dict:
    """Explicit user action after wallet-signed intent; one remote POST max.

    No automatic wallet access/grant. An uncertain API response leaves a
    locked UNCERTAIN state and an operator reconciliation requirement.
    """
    did = _safe_did(did)
    evidence = await get_eth_user_enrollment_preflight(did)
    _require_enrollment_evidence(evidence)
    owner, executor = _require_policy_roles()
    prices = await fetch_reference_prices()
    now = int(time.time())
    caps = derive_policy_atomic_caps(
        max_order_usd=evidence["live_max_order_usd"],
        snapshot=prices, now=now,
    )
    validated = await asyncio.to_thread(
        _reserve, did, evidence, owner, executor, caps, now,
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9.0), follow_redirects=False,
        ) as client:
            remote = await _post_and_readback_unattached(
                validated=validated, client=client,
            )
    except Exception:
        # Remote POST might have succeeded and GET/readback timed out.
        # Fail-closed status is persisted BEFORE returning an error.
        await asyncio.to_thread(_finish, did, validated, None)
        raise HTTPException(
            503, "Privy creation outcome uncertain; support reconciliation required",
        ) from None
    return await asyncio.to_thread(_finish, did, validated, remote)
