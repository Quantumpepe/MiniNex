"""Read-only complete ETH onboarding readiness and error-recovery diagnosis.

No side-effectful Privy policy creation, grant or on-chain execution.
The report combines independent user wallet metadata, policy ledger state,
verified per-user LIVE limits, pending order exposure and optional remote
Privy policy review; a local grant record is never live authorization.
"""
from __future__ import annotations

import asyncio
from fastapi import HTTPException

from .eth_user_enrollment_preflight import get_eth_user_enrollment_preflight
from .eth_privy_policy_ledger import get_eth_policy_ledger_status
from .eth_live_ledger_audit import get_eth_live_ledger_audit
from .eth_privy_user_grant import get_eth_user_grant_review
from .storage import _safe_did

_POLICIES_REMOTE = {"REMOTE_VERIFIED_UNATTACHED", "GRANT_VERIFIED"}
_UNCERTAIN = {"SUBMITTING","UNCERTAIN","REVOKE_REQUESTED"}


def assess_eth_prelaunch_safety(
    *, enrollment: dict, policy: dict, ledger: dict,
    grant: dict | None,
) -> dict:
    for data, mode in (
        (enrollment,"ETH_PRIVY_USER_ENROLLMENT_REVIEW_ONLY"),
        (policy,"ETH_PRIVY_POLICY_DURABLE_PREPARATION_ONLY"),
        (ledger,"ETH_LIVE_LEDGER_READ_ONLY"),
    ):
        if type(data) is not dict or data.get("mode") != mode:
            raise HTTPException(503, "Verified ETH safety audit evidence unavailable")
        if data.get("asset") != "ETH" or data.get("chain_id") != 1:
            raise HTTPException(503, "ETH audit chain identity mismatch")
        if data.get("execution_enabled") is not False:
            raise HTTPException(503, "ETH audit state inconsistent with stop switch")
    state = policy.get("status")
    if state not in (
        "NOT_PREPARED","PREPARED","SUBMITTING","UNCERTAIN",
        "REMOTE_VERIFIED_UNATTACHED","GRANT_VERIFIED",
        "REVOKE_REQUESTED","REVOKED",
    ):
        raise HTTPException(503, "Unknown ETH policy status")
    blockers = ["LIVE_EXECUTION_NOT_IMPLEMENTED", "REAL_TRADE_ABI_AUDIT_PENDING",
                "GAS_ALLOWANCE_SETTLEMENT_AUDIT_PENDING"]
    if not all(enrollment.get(field) is True for field in (
        "wallet_identity_verified", "wallet_owned_by_authenticated_user",
        "wallet_chain_verified", "policy_owner_independent",
    )):
        blockers.append("PRIVY_WALLET_OR_OWNER_NOT_VERIFIED")
    if not all(enrollment.get(field) is True for field in (
        "user_live_limits_verified", "live_limits_nonzero",
    )):
        blockers.append("LIVE_LIMITS_NOT_READY")
    if enrollment.get("trading_paused") is not True or ledger.get("trading_paused") is not True:
        blockers.append("EMERGENCY_STOP_STATE_NOT_VERIFIED")
    if (type(enrollment.get("live_limits_revision")) is not int
            or enrollment.get("live_limits_revision") != ledger.get("limits_revision")):
        blockers.append("LIVE_LIMITS_REVISION_MISMATCH")
    if (state != "NOT_PREPARED" and (
        policy.get("live_limits_revision_matches") is not True
        or policy.get("limits_revision") != ledger.get("limits_revision")
    )):
        blockers.append("POLICY_LIMITS_REVISION_MISMATCH")
    if enrollment.get("unrecognized_existing_signers") is not False:
        # Even a structurally matching signer is not trusted until the
        # full policy readback and wallet-owner confirmation are verified.
        if not (type(grant) is dict and grant.get("user_privy_grant_verified") is True):
            blockers.append("UNVERIFIED_ADDITIONAL_SIGNERS")
    if state in _UNCERTAIN:
        blockers.append("POLICY_STATE_REQUIRES_MANUAL_RECONCILIATION")
    if state == "REVOKE_REQUESTED":
        blockers.append("OWNER_PRIVY_SIGNER_REMOVAL_NOT_YET_VERIFIED")
    if state == "REVOKED":
        blockers.append("USER_REVOKED_POLICY")
    if state in ("NOT_PREPARED","PREPARED"):
        blockers.append("POLICY_NOT_CREATED_AND_VERIFIED_REMOTELY")
    if state in _POLICIES_REMOTE and policy.get("policy_expired") is True:
        blockers.append("PRIVY_POLICY_EXPIRED")
    if state in _POLICIES_REMOTE and (
        type(grant) is not dict or grant.get("policy_verified") is not True
        or grant.get("wallet_owned_by_user") is not True
    ):
        blockers.append("REMOTE_PRIVY_POLICY_READBACK_NOT_VERIFIED")
    if state == "GRANT_VERIFIED" and (
        type(grant) is not dict or grant.get("user_privy_grant_verified") is not True
    ):
        blockers.append("USER_SIGNER_GRANT_CANNOT_BE_REVERIFIED")
    if policy.get("reconciliation_required") is True:
        blockers.append("POLICY_RECONCILIATION_REQUIRED")
    if ledger.get("unresolved_order_count") is None:
        raise HTTPException(503,"Missing unsettled ETH order exposure")
    if ledger.get("reconciliation_required_count",0)>0:
        blockers.append("UNSETTLED_ORDER_RECONCILIATION_REQUIRED")
    if ledger.get("older_than_30m_pending_count",0)>0:
        blockers.append("STALE_UNRESOLVED_ORDER")
    if ledger.get("cumulative_spend_enforced_at_execution") is not False:
        raise HTTPException(503,"Unexpected ETH cumulative budget state")
    blockers.append("DAILY_SPEND_EXECUTION_GATE_NOT_ACTIVATED")
    return {
        "mode":"ETH_LIVE_PRELAUNCH_AUDIT_ONLY","asset":"ETH","chain_id":1,
        "wallet_identity_verified":enrollment["wallet_identity_verified"],
        "wallet_signer_reviewed":enrollment.get("wallet_additional_signers_reviewed") is True,
        "policy_status":state,
        "policy_expired":policy.get("policy_expired") is True,
        "policy_expires_at_unix":policy.get("policy_expires_at_unix"),
        "remote_policy_recorded":policy.get("remote_policy_recorded") is True,
        "policy_id":policy.get("policy_id"),
        "privy_signer_grant_verified":(
            type(grant) is dict
            and grant.get("user_privy_grant_verified") is True
        ),
        "grant_offer_enabled":(
            type(grant) is dict and grant.get("grant_offer_enabled") is True
        ),
        "signed_wallet_owner_grant_required":True,
        "live_limits_revision":ledger.get("limits_revision"),
        "live_ledger":ledger,
        "manual_policy_reconciliation_required":state in _UNCERTAIN,
        "automatic_privy_policy_retry_permitted":False,
        "blockers":list(dict.fromkeys(blockers)),
        "read_only":True,
        "policy_created_by_this_check":False,
        "wallet_signer_modified_by_this_check":False,
        "real_trading_enabled":False,
        "can_sign":False,"can_submit":False,"execution_enabled":False,
    }


async def get_eth_prelaunch_safety_report(did: str) -> dict:
    """One authenticated user, all external GETs with fail-closed results."""
    did = _safe_did(did)
    enrollment,policy,ledger=await asyncio.gather(
        get_eth_user_enrollment_preflight(did),
        asyncio.to_thread(get_eth_policy_ledger_status,did),
        asyncio.to_thread(get_eth_live_ledger_audit,did),
    )
    grant=None
    if policy.get("status") in _POLICIES_REMOTE:
        try:
            grant=await get_eth_user_grant_review(did)
        except (HTTPException, ValueError, TypeError):
            # External Privy unavailable; report blocked, do not claim grant.
            pass
    return assess_eth_prelaunch_safety(
        enrollment=enrollment,policy=policy,ledger=ledger,grant=grant,
    )
