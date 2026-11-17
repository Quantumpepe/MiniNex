"""Owner-only revocable status for separate MiniNex auto-fee consent.

A local revoke *immediately* blocks the MiniNex fee executor, but does not
falsely assert remote Privy signer removal; the user must revoke the remote
additional signer separately. Neither GET nor POST signs or sends a fee.
"""
from __future__ import annotations

import logging
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_owner_consent import _asset
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.evm_fee_auto_permission_status")


def owner_fee_auto_permission_status(*,did:str,asset:str)->dict:
    did=_safe_did(did);asset=_asset(asset)
    db=_db_url();_ensure_schema(db)
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT stable,chain_id,wallet_address,treasury_wallet,
                              owner_max_fee_atomic,expires_at,status,
                              owner_signed_at,policy_id,grant_verified_at,
                              revoked_at
                       FROM mininex_evm_fee_auto_permissions
                       WHERE privy_did=%s AND asset=%s""",(did,asset))
                row=cur.fetchone()
    except psycopg.Error:
        LOG.warning("Separate owner fee permission status unavailable")
        raise HTTPException(503,"Auto-fee owner permission unavailable") from None
    if row is None:
        return {
            "asset":asset,"fee_auto_enrollment_exists":False,
            "fee_transfer_enabled":False,"privy_grant_verified":False,
        }
    return {
        "asset":asset,"fee_auto_enrollment_exists":True,
        "stable":row["stable"],"chain_id":row["chain_id"],
        "owner_wallet":row["wallet_address"],
        "treasury_wallet":row["treasury_wallet"],
        "owner_max_fee_atomic":str(row["owner_max_fee_atomic"]),
        "policy_expires_at":row["expires_at"].isoformat(),
        "state":row["status"],
        "owner_intent_signed":row["owner_signed_at"] is not None,
        "privy_policy_id":row["policy_id"],
        "privy_grant_verified":row["status"]=="GRANT_VERIFIED"
            and row["grant_verified_at"] is not None
            and row["revoked_at"] is None,
        "fee_transfer_enabled":False,  # no actual fee executor presently
        "remote_privy_revocation_may_be_required":
            row["revoked_at"] is not None
            and row["policy_id"] is not None,
    }


def revoke_owner_fee_auto_permission(*,did:str,asset:str)->dict:
    did=_safe_did(did);asset=_asset(asset)
    db=_db_url();_ensure_schema(db)
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """UPDATE mininex_evm_fee_auto_permissions
                       SET status='REVOKED',revoked_at=now(),updated_at=now()
                       WHERE privy_did=%s AND asset=%s
                         AND status <> 'REVOKED'
                       RETURNING policy_id""",
                    (did,asset))
                result=cur.fetchone()
                if result is None:
                    cur.execute(
                        """SELECT status,policy_id
                           FROM mininex_evm_fee_auto_permissions
                           WHERE privy_did=%s AND asset=%s""",(did,asset))
                    result=cur.fetchone()
    except psycopg.Error:
        LOG.warning("Could not persist owner fee consent revocation")
        raise HTTPException(503,"Cannot revoke local fee auto permission") from None
    return {
        "asset":asset,
        "state":"REVOKED_LOCALLY" if result else "NO_ACTIVE_PERMISSION",
        "future_automatic_fee_transactions_blocked":True,
        "existing_wallet_transactions_unaffected":True,
        "remote_privy_grant_removed":False,
        "remote_privy_owner_revocation_required":
            result is not None and result.get("policy_id") is not None,
    }
