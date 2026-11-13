"""Authenticated, read-only user ETH policy enrollment evidence for MiniNex.

Connects a server-verified Privy *user-owned* wallet and its independently
stored LIVE ETH risk preferences to the future per-wallet policy generator.
This is deliberately NOT a user-consent/signature flow or authorization
for funds. It neither derives USD->token policy caps without independent
fresh price evidence nor manufactures user-granted authority.

NO Privy policy POST/PUT, signer association, wallet mutation, approval,
simulation, send, stop-toggle release, or trade execution takes place.
"""
from __future__ import annotations

import asyncio
import os
import re

from .live_eth_controls import get_live_eth_limits, validate_limits
from .privy_live_signer import (
    _owned_eth_wallet, fetch_privy_wallet_metadata,
)
from .privy_wallet_verification import fetch_privy_user
from .storage import _safe_did

_ID = re.compile(r"^[a-zA-Z0-9_-]{6,160}$")


def assess_eth_enrollment_evidence(
    *, did: str, privy_user: object, wallet_metadata: object,
    live_limits: object, policy_owner_id: object, executor_signer_id: object,
) -> dict:
    """Strict input provenance check; never equate readiness with consent.

    The real caller obtains Privy identity and wallet metadata through
    server-to-Privy GET requests and live limits via its own database.
    Unit tests can supply explicit independent fixtures.
    """
    _safe_did(did)
    result = {
        "mode": "ETH_PRIVY_USER_ENROLLMENT_REVIEW_ONLY",
        "asset": "ETH",
        "chain_id": 1,
        "verified_wallet_address": None,
        "wallet_identity_verified": False,
        "wallet_owned_by_authenticated_user": False,
        "wallet_chain_verified": False,
        "wallet_additional_signers_reviewed": False,
        "unrecognized_existing_signers": True,
        "policy_owner_configured": False,
        "executor_signer_configured": False,
        "policy_owner_independent": False,
        "user_live_limits_verified": False,
        "live_limits_nonzero": False,
        "live_limits_revision": None,
        "live_capital_usd": None,
        "live_max_order_usd": None,
        "live_daily_turnover_usd": None,
        "trading_paused": True,
        "user_consent_signed_and_nonce_verified": False,
        "user_consent_revocation_proven": False,
        "fresh_usd_to_atomic_token_caps_verified": False,
        "privy_policy_schema_and_owner_confirmed_remotely": False,
        "complete_privy_policy_generated": False,
        "policy_created": False,
        "additional_signer_enrolled": False,
        "wallet_delegation_enabled": False,
        "can_grant_permission": False,
        "can_sign": False,
        "can_submit": False,
        "execution_enabled": False,
    }

    owner_ok = (type(policy_owner_id) is str
                and _ID.fullmatch(policy_owner_id) is not None)
    signer_ok = (type(executor_signer_id) is str
                 and _ID.fullmatch(executor_signer_id) is not None)
    result["policy_owner_configured"] = owner_ok
    result["executor_signer_configured"] = signer_ok
    result["policy_owner_independent"] = (
        owner_ok and signer_ok and policy_owner_id != executor_signer_id
    )

    try:
        owned = _owned_eth_wallet(privy_user, did)
    except ValueError:
        return result
    if (type(wallet_metadata) is not dict
            or wallet_metadata.get("id") != owned["wallet_id"]
            or wallet_metadata.get("chain_type") != "ethereum"
            or type(wallet_metadata.get("address")) is not str
            or wallet_metadata["address"].lower() != owned["address"]
            or type(wallet_metadata.get("owner")) is not dict
            or wallet_metadata["owner"].get("user_id") != did):
        return result

    result["verified_wallet_address"] = owned["address"]
    result["wallet_identity_verified"] = True
    result["wallet_owned_by_authenticated_user"] = True
    result["wallet_chain_verified"] = True

    signers = wallet_metadata.get("additional_signers")
    if type(signers) is list:
        # This check only detects unexpected signers, it does not prove
        # the safety of any pre-existing signer or verify its policy.
        result["wallet_additional_signers_reviewed"] = True
        # No pre-existing additional signer can be trusted on shape alone:
        # even an expected ID with a policy pointer might reference a broad
        # policy. Actual policy contents and user consent remain unverified.
        result["unrecognized_existing_signers"] = bool(signers)
    if type(live_limits) is not dict:
        return result
    rev = live_limits.get("revision")
    if type(rev) is not int or rev < 0:
        return result
    capital, order, daily = (
        live_limits.get("planned_capital_usd"),
        live_limits.get("max_order_usd"),
        live_limits.get("max_daily_turnover_usd"),
    )
    try:
        cap, max_order, max_daily = validate_limits(capital, order, daily)
    except Exception:
        # Fail closed: malformed budget never becomes a draft.
        return result

    # Database stored budget is not user signing authority, even if nonzero.
    result["user_live_limits_verified"] = True
    result["live_limits_revision"] = rev
    result["live_capital_usd"] = str(cap)
    result["live_max_order_usd"] = str(max_order)
    result["live_daily_turnover_usd"] = str(max_daily)
    result["live_limits_nonzero"] = bool(cap and max_order and max_daily)
    result["trading_paused"] = live_limits.get("trading_paused") is not False
    return result


async def get_eth_user_enrollment_preflight(privy_did: str) -> dict:
    """Authenticated read-only report using only independently sourced IDs.

    The caller supplies only a DID from a verified Privy JWT; no wallet ID,
    policy ID, signer ID, router or budget is accepted from the browser.
    """
    did = _safe_did(privy_did)
    user = await fetch_privy_user(did)
    try:
        owned = _owned_eth_wallet(user, did)
    except ValueError:
        # Empty metadata still gives a blocked, non-executable diagnostic.
        return assess_eth_enrollment_evidence(
            did=did, privy_user=user, wallet_metadata=None, live_limits=None,
            policy_owner_id=os.getenv("MININEX_PRIVY_ETH_POLICY_OWNER_ID", ""),
            executor_signer_id=os.getenv("MININEX_PRIVY_ETH_SIGNER_ID", ""),
        )

    wallet = await fetch_privy_wallet_metadata(owned["wallet_id"])
    limits = await asyncio.to_thread(get_live_eth_limits, did)
    return assess_eth_enrollment_evidence(
        did=did, privy_user=user, wallet_metadata=wallet,
        live_limits=limits,
        policy_owner_id=os.getenv("MININEX_PRIVY_ETH_POLICY_OWNER_ID", ""),
        executor_signer_id=os.getenv("MININEX_PRIVY_ETH_SIGNER_ID", ""),
    )
