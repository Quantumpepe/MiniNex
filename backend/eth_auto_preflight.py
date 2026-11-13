"""One private read-only ETH automatic-trading preflight, never a signer.

Combines independent Privy wallet signer evidence, pinned Privy policy
inspection, and server-owned ETH live risk limits. This is an integration
checkpoint for the future executor, NOT an execution/grant endpoint.

The current Privy static policy checks exactInputSingle, while the ETH
swap-plan uses the Universal Router execute(bytes,bytes[],uint256) path.
Those are different call semantics. Therefore even an otherwise valid
policy report cannot authorize the current route.

No funds, approvals, order reservations, signing, wallet delegation,
policy issuance, or chain transactions may be initiated here.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from .live_eth_controls import get_live_eth_limits, validate_limits
from .live_orders import LIVE_RESERVATION_ACTIVATED
from .privy_eth_policy import get_eth_policy_review
from .privy_live_signer import get_eth_signer_status
from .storage import _safe_did

# This is a documented mismatch, NOT a switch to enable signing.
CURRENT_ROUTER_CALL = "execute(bytes,bytes[],uint256)"
STATIC_POLICY_CALL = "exactInputSingle"


def assess_eth_automatic_preflight(*, signer: object, policy: object,
                                   limits: object) -> dict:
    """Explain blockers from server-derived evidence without granting access."""
    if not all(isinstance(source, dict) for source in (signer, policy, limits)):
        raise ValueError("Invalid server-side ETH preflight evidence")

    try:
        capital, order, daily = validate_limits(
            str(limits.get("planned_capital_usd", "0")),
            str(limits.get("max_order_usd", "0")),
            str(limits.get("max_daily_turnover_usd", "0")),
        )
        valid_limits = all(amount > Decimal("0") for amount in (capital, order, daily))
    except (ValueError, TypeError, KeyError, AttributeError):
        valid_limits = False
    except Exception as exc:
        # Limit validation may raise HTTPException; an invalid saved value must
        # never become a positive readiness check.
        from fastapi import HTTPException
        if not isinstance(exc, HTTPException):
            raise
        valid_limits = False

    checks = {
        "privy_wallet_delegated": signer.get("wallet_delegated") is True,
        "privy_additional_signer_verified": signer.get("signer_verified") is True,
        "privy_restricted_policy_reference_verified": (
            signer.get("policy_reference_verified") is True),
        "privy_policy_digest_pinned_and_matched": (
            policy.get("policy_hash_pinned") is True
            and policy.get("policy_digest_matches") is True),
        "privy_policy_structure_checked": (
            policy.get("policy_structure_checked") is True
            and policy.get("recipient_wallet_bound") is True),
        "per_user_eth_limits_configured": valid_limits,
        "live_eth_stop_released": limits.get("trading_paused") is False,
        # The pinned Privy static-check logic currently guards a *different*
        # method, and cannot safely authorize Universal Router's nested calls.
        "policy_covers_current_router_call": False,
        "separate_revocable_execution_consent_verified": False,
        "atomic_order_reservations_enabled": LIVE_RESERVATION_ACTIVATED is True,
        "live_receipt_reconciliation_proven": False,
    }

    blockers = [key for key, passed in checks.items() if passed is not True]
    return {
        "mode": "ETH_AUTO_EXECUTION_READINESS_ONLY",
        "asset": "ETH",
        "chain_id": 1,
        "scope": "VERIFIED_PRIVY_ACCOUNT_ONLY",
        "checks": checks,
        "blockers": blockers,
        "router_method": CURRENT_ROUTER_CALL,
        "current_policy_method": STATIC_POLICY_CALL,
        "risk_limit_revision": (
            limits.get("revision") if type(limits.get("revision")) is int else None),
        "preflight_passed": False,
        "can_execute": False,
        "can_reserve": False,
        "automated_signing_enabled": False,
        "live_trading_enabled": False,
        "warning": (
            "Privy signer, policy, and limits are independent checks. The "
            "current policy does not authorize Universal Router execute; "
            "execution consent, atomic reservations, signing and receipt "
            "reconciliation are not implemented."
        ),
    }


async def get_eth_automatic_preflight(privy_did: str) -> dict:
    """On-demand account-scoped check; no client-supplied wallet or route."""
    did = _safe_did(privy_did)
    # The independent existing endpoints are the authoritative evidence.
    # PostgreSQL is synchronous; do not block the API event loop with it.
    signer = await get_eth_signer_status(did)
    policy = await get_eth_policy_review(did)
    limits = await asyncio.to_thread(get_live_eth_limits, did)
    return assess_eth_automatic_preflight(signer=signer, policy=policy, limits=limits)
