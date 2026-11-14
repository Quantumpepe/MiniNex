"""MiniNex ETH Stage 22: verified user-owned Privy signer *absence*.

The client invokes Privy's useSigners().removeSigners({address}) only after an
explicit user confirmation, and this module independently GETs the wallet
from Privy. It CANNOT authorize or send transactions or remove signers via
server credentials.

The server only marks a locally revoked policy record REVOKED after:
* the user's exact Privy wallet owner/id/address is independently verified,
* the remote additional_signers field exists and is an EMPTY list,
* the MiniNex LIVE stop is still active, signed intent is REVOKED,
* the matching ledger row was already REVOKE_REQUESTED.
The status records signer absence at a point in time; it does not promise
future absence, proof that the backend performed removal, or policy deletion.
"""
from __future__ import annotations

import asyncio
import logging
import os

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .privy_live_signer import _owned_eth_wallet, fetch_privy_wallet_metadata
from .privy_wallet_verification import fetch_privy_user
from .storage import _safe_did, _db_url, _ensure_schema, _prepare_rows

LOG = logging.getLogger("mininex.eth_privy_signer_revocation")


def check_privy_wallet_signers_absent(
    *, did: str, privy_user: object, wallet_metadata: object,
) -> dict:
    """Fail closed if signer field is absent, malformed, or any signer exists.

    Wallet and owner identities are verified from independent Privy GETs,
    not from frontend wallet addresses or delegated boolean assertions.
    """
    _safe_did(did)
    result = {
        "asset": "ETH",
        "chain_id": 1,
        "mode": "PRIVY_SIGNER_ABSENCE_REMOTE_READ_ONLY",
        "verified_wallet_address": None,
        "wallet_identity_verified": False,
        "wallet_owned_by_authenticated_user": False,
        "remote_wallet_metadata_verified": False,
        "additional_signers_field_verified": False,
        "signer_absence_verified": False,
        "privy_remote_signer_removed_by_backend": False,
        "privy_policy_deleted": False,
        "revocation_remains_continuously_enforced": False,
        "execution_enabled": False,
        "can_sign": False,
        "can_submit": False,
    }
    try:
        owned = _owned_eth_wallet(privy_user, did)
    except ValueError:
        return result
    if (
        type(wallet_metadata) is not dict
        or wallet_metadata.get("id") != owned["wallet_id"]
        or wallet_metadata.get("chain_type") != "ethereum"
        or type(wallet_metadata.get("address")) is not str
        or wallet_metadata["address"].lower() != owned["address"]
        or type(wallet_metadata.get("owner")) is not dict
        or wallet_metadata["owner"].get("user_id") != did
    ):
        return result
    result["verified_wallet_address"] = owned["address"]
    result["wallet_identity_verified"] = True
    result["wallet_owned_by_authenticated_user"] = True
    result["remote_wallet_metadata_verified"] = True
    signers = wallet_metadata.get("additional_signers")
    if type(signers) is not list:
        return result
    result["additional_signers_field_verified"] = True
    result["signer_absence_verified"] = len(signers) == 0
    return result


async def get_privy_wallet_signer_absence(did: str) -> dict:
    """Authenticated, independent Privy GETs; does not mutate wallets."""
    did = _safe_did(did)
    user = await fetch_privy_user(did)
    try:
        owned = _owned_eth_wallet(user, did)
    except ValueError:
        return check_privy_wallet_signers_absent(
            did=did, privy_user=user, wallet_metadata=None,
        )
    metadata = await fetch_privy_wallet_metadata(owned["wallet_id"])
    return check_privy_wallet_signers_absent(
        did=did, privy_user=user, wallet_metadata=metadata,
    )


def _confirm_local_revocation_locked(
    cur, *, did: str, verified_wallet: str, expected_executor: str,
) -> dict:
    """Atomic local finalization; user-visible success requires remote GET.

    The remote check must have completed before entering this DB
    transaction; never hold DB locks around external Privy HTTP requests.
    """
    cur.execute(
        """SELECT trading_paused
           FROM mininex_live_eth_limits
           WHERE privy_did=%s FOR UPDATE""", (did,),
    )
    live = cur.fetchone()
    if type(live) is not dict or live.get("trading_paused") is not True:
        raise HTTPException(409, "MiniNex ETH stop must remain active")

    cur.execute(
        """SELECT status,revoked_at
           FROM mininex_live_eth_consent_intents
           WHERE privy_did=%s FOR UPDATE""", (did,),
    )
    consent = cur.fetchone()
    if (
        type(consent) is not dict
        or consent.get("status") != "REVOKED"
        or consent.get("revoked_at") is None
    ):
        raise HTTPException(409, "Local ETH consent must first be revoked")

    cur.execute(
        """SELECT wallet_address,execution_signer_id,status
           FROM mininex_live_eth_policy_enrollments
           WHERE privy_did=%s FOR UPDATE""", (did,),
    )
    policy = cur.fetchone()
    if (
        type(policy) is not dict
        or policy.get("wallet_address") != verified_wallet
        or policy.get("execution_signer_id") != expected_executor
        or policy.get("status") not in ("REVOKE_REQUESTED", "REVOKED")
    ):
        raise HTTPException(409, "Matching revoked wallet policy required")
    if policy["status"] == "REVOKE_REQUESTED":
        cur.execute(
            """UPDATE mininex_live_eth_policy_enrollments
               SET status='REVOKED',revoked_at=now(),updated_at=now()
               WHERE privy_did=%s AND status='REVOKE_REQUESTED'
                     AND wallet_address=%s
               RETURNING status""",
            (did, verified_wallet),
        )
        updated = cur.fetchone()
        if type(updated) is not dict or updated.get("status") != "REVOKED":
            raise HTTPException(409, "Privy policy revocation state changed")
    return {
        "asset": "ETH",
        "chain_id": 1,
        "mode": "PRIVY_REMOTE_SIGNER_ABSENCE_CONFIRMATION_ONLY",
        "status": "REVOKED",
        "local_eth_trading_paused": True,
        "local_consent_revoked": True,
        "signer_absence_verified_by_privy_get": True,
        "privy_policy_deleted": False,
        "privy_remote_signer_removed_by_backend": False,
        "revocation_remains_continuously_enforced": False,
        "can_grant_permission": False,
        "can_sign": False,
        "can_submit": False,
        "execution_enabled": False,
    }


def _persist_remote_absence_confirmation(
    did: str, verified_wallet: str, expected_executor: str,
) -> dict:
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                return _confirm_local_revocation_locked(
                    cur, did=did, verified_wallet=verified_wallet,
                    expected_executor=expected_executor,
                )
    except psycopg.Error:
        LOG.warning("ETH Privy signer absence record unavailable")
        raise HTTPException(
            503, "Cannot record remote Privy signer absence",
        ) from None


async def confirm_privy_wallet_signer_revocation(did: str) -> dict:
    """Verify first-party Privy wallet signer absence before local final state.

    Only a fresh server-observed empty signer list can trigger finalization.
    No user request body can choose a different address, signer, or policy.
    """
    did = _safe_did(did)
    # Independent owner/executor roles must still be configured.
    owner = os.getenv("MININEX_PRIVY_ETH_POLICY_OWNER_ID", "").strip()
    executor = os.getenv("MININEX_PRIVY_ETH_SIGNER_ID", "").strip()
    if (
        not owner or not executor or owner == executor
        or len(owner) > 160 or len(executor) > 160
    ):
        raise HTTPException(409, "Independent Privy quorums are required")
    verification = await get_privy_wallet_signer_absence(did)
    if (
        verification["wallet_identity_verified"] is not True
        or verification["wallet_owned_by_authenticated_user"] is not True
        or verification["remote_wallet_metadata_verified"] is not True
        or verification["additional_signers_field_verified"] is not True
        or verification["signer_absence_verified"] is not True
    ):
        raise HTTPException(409, "Privy still reports signers or cannot verify wallet")
    return await asyncio.to_thread(
        _persist_remote_absence_confirmation,
        did, verification["verified_wallet_address"], executor,
    )
