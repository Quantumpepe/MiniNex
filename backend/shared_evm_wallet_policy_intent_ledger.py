"""Original user wallet policy-update intent journal. No remote Privy writes.

Only server-recomputed, original-wallet-bound EIP-191 challenges are staged.
This intentionally cannot update an attached signer, replace an existing
policy, start a trade, or transfer funds.

Future rollout controller must independently fetch current Privy signer and
policy metadata, the locked per-coin user preferences and the prospective
policy before calling these internal transitions. A SIGNED_INTENT is NOT a
remote Privy policy update and never grants permissions by itself.
"""
from __future__ import annotations

import hashlib

from fastapi import HTTPException

from .shared_evm_wallet_scope_transition import (
    verify_wallet_policy_update_intent,
)
from .storage import _safe_did


def _read_current_wallet_grant_locked(cur, did: str) -> dict:
    cur.execute(
        """SELECT wallet_id,wallet_address,owner_id,signer_id,policy_id,
                  policy_sha256,status,legacy_reconciliation_required,
                  original_owner_granted_at,revision
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""", (did,),
    )
    grant = cur.fetchone()
    if (type(grant) is not dict or grant.get("status") != "GRANT_VERIFIED"
            or grant.get("legacy_reconciliation_required") is not False
            or grant.get("policy_id") is None
            or grant.get("original_owner_granted_at") is None
            or type(grant.get("revision")) is not int
            or grant["revision"] < 1):
        raise HTTPException(409, "Current original owner wallet grant not verified")
    return grant


def _match_current_plan(grant: dict, did: str, plan: dict, now: int) -> None:
    """Reject stale, mismatched or non-expanding demands before DB writes."""
    if (type(plan) is not dict
            or plan.get("mode") != "OFFLINE_OWNER_EVM_WALLET_POLICY_TRANSITION"
            or plan.get("state") != "OWNER_SIGNED_POLICY_UPDATE_INTENT_REQUIRED"
            or plan.get("did") != did
            or plan.get("wallet_id") != grant["wallet_id"]
            or plan.get("wallet") != grant["wallet_address"]
            or plan.get("shared_signer_id") != grant["signer_id"]
            or plan.get("previous_policy_sha256") != grant["policy_sha256"]
            or type(plan.get("target_policy_sha256")) is not str
            or len(plan["target_policy_sha256"]) != 64
            or plan["target_policy_sha256"] == grant["policy_sha256"]
            or type(plan.get("intent_challenge")) is not str
            or f"Policy owner ID: {grant['owner_id']}" not in plan["intent_challenge"]
            or type(plan.get("expires_at_unix")) is not int
            or not plan["expires_at_unix"] - 300 <= now < plan["expires_at_unix"]):
        raise HTTPException(409, "Wallet policy change differs from original grant")


def begin_locked_user_policy_update_intent(
    cur, *, did: str, plan: dict, now: int,
) -> dict:
    """Record PENDING signed challenge under original wallet row lock.

    The *trusted server* must construct plan from original current grants,
    fresh remote policy readback and locked source budgets, not HTTP input.
    """
    did = _safe_did(did)
    current = _read_current_wallet_grant_locked(cur, did)
    _match_current_plan(current, did, plan, now)
    message = plan["intent_challenge"]
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    if digest != plan.get("intent_challenge_sha256"):
        raise HTTPException(409, "Owner wallet consent bytes changed")
    cur.execute(
        """INSERT INTO mininex_live_evm_wallet_policy_intents
           (privy_did,nonce,wallet_id,wallet_address,
            previous_policy_sha256,target_policy_sha256,
            challenge_text,challenge_sha256,
            status,issued_at,expires_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',
                   to_timestamp(%s),to_timestamp(%s))
           ON CONFLICT(privy_did,nonce) DO NOTHING RETURNING status""",
        (
            did,plan["nonce"],current["wallet_id"],current["wallet_address"],
            current["policy_sha256"],plan["target_policy_sha256"],
            message,digest,plan["expires_at_unix"] - 300,
            plan["expires_at_unix"],
        ),
    )
    result = cur.fetchone()
    if type(result) is not dict or result.get("status") != "PENDING":
        raise HTTPException(409, "Duplicate wallet policy consent request")
    return {
        "mode": "OWNER_EVM_WALLET_POLICY_INTENT_ONLY",
        "status": "PENDING",
        "challenge": message,
        "target_policy_sha256": plan["target_policy_sha256"],
        "policy_updated_in_privy": False,
        "signer_grant_changed": False,
        "live_trading_enabled_by_this_action": False,
    }


def confirm_locked_user_policy_update_intent(
    cur, *, did: str, plan: dict, signature: str, now: int,
) -> dict:
    """Check original EIP-191 signer then commit SIGNED_INTENT exactly once."""
    did = _safe_did(did)
    current = _read_current_wallet_grant_locked(cur, did)
    _match_current_plan(current, did, plan, now)
    cur.execute(
        """SELECT wallet_id,wallet_address,previous_policy_sha256,
                  target_policy_sha256,challenge_text,challenge_sha256,
                  status,extract(epoch FROM expires_at)::bigint
                      AS expires_at_unix
           FROM mininex_live_evm_wallet_policy_intents
           WHERE privy_did=%s AND nonce=%s FOR UPDATE""",
        (did,plan["nonce"]),
    )
    record=cur.fetchone()
    if (type(record) is not dict or record.get("status") != "PENDING"
            or record.get("wallet_id") != current["wallet_id"]
            or record.get("wallet_address") != current["wallet_address"]
            or record.get("previous_policy_sha256") != current["policy_sha256"]
            or record.get("target_policy_sha256") != plan["target_policy_sha256"]
            or record.get("challenge_text") != plan["intent_challenge"]
            or record.get("challenge_sha256") != plan["intent_challenge_sha256"]
            or record.get("expires_at_unix") != plan["expires_at_unix"]
            or now >= record["expires_at_unix"]):
        raise HTTPException(409, "Original wallet policy signature challenge unavailable")
    if not verify_wallet_policy_update_intent(
        plan=plan,expected_did=did,
        expected_wallet=current["wallet_address"],
        expected_wallet_id=current["wallet_id"],
        expected_previous_digest=current["policy_sha256"],
        expected_target_digest=plan["target_policy_sha256"],
        signature=signature,now=now,
    ):
        raise HTTPException(403, "Original wallet did not approve this EVM scope")
    cur.execute(
        """UPDATE mininex_live_evm_wallet_policy_intents
           SET status='SIGNED_INTENT',signed_at=now()
           WHERE privy_did=%s AND nonce=%s AND status='PENDING'
           RETURNING status""", (did,plan["nonce"]),
    )
    result=cur.fetchone()
    if type(result) is not dict or result.get("status") != "SIGNED_INTENT":
        raise HTTPException(409, "Wallet scope consent was already used")
    return {
        "mode": "OWNER_EVM_WALLET_POLICY_INTENT_ONLY",
        "status": "SIGNED_INTENT",
        "wallet_signature_verified": True,
        "original_policy_sha256": current["policy_sha256"],
        "requested_policy_sha256": plan["target_policy_sha256"],
        "policy_updated_in_privy": False,
        "signer_grant_changed": False,
        "live_trading_enabled_by_this_action": False,
    }
