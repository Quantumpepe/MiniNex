"""Independently prove owner's *separate* Privy fee signer grant.

A published fee policy does NOT mean user delegation. Owner must explicitly
execute Privy addSigners; only then can a server-side fresh GET prove the
exact wallet, policy, Treasury, caller identity and registered P-256 quorum.
The fee grant cannot inherit an unrelated trading grant or authorize an
immediate payment. No call here signs or broadcasts.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timezone
import logging
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_fee_policy_publish import original_signed_fee_policy
from .shared_evm_privy_grant_review import verify_exact_remote_evm_policy
from .shared_evm_owner_consent import _asset
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import (
    _owned_eth_wallet,_scoped_signer,fetch_privy_wallet_metadata,
)
from .eth_privy_user_grant import _fetch_policy
from .shared_evm_fee_policy_roles import check_privy_fee_backend_quorum as check_privy_backend_quorum
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.owner_fee_privy_grant")


def _read_original_fee_grant(did:str,asset:str)->dict:
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT * FROM mininex_evm_fee_auto_permissions
                   WHERE privy_did=%s AND asset=%s""",(did,asset))
            record=cur.fetchone()
    return record


def _verify_owner_fee_grant(
    *,did:str,record:dict,expected:dict,user:object,
    wallet_metadata:object,remote_policy:object,quorum:object,
)->dict:
    """No client-provided grant claims; all inputs are server Privy GETs."""
    try:
        owned=_owned_eth_wallet(user,did)
        if (record.get("status") not in ("REMOTE_UNATTACHED","GRANT_VERIFIED")
                or owned.get("delegated") is not True
                or owned.get("wallet_id")!=record["wallet_id"]
                or owned.get("address")!=record["wallet_address"]
                or type(wallet_metadata) is not dict
                or wallet_metadata.get("id")!=owned["wallet_id"]):
            raise ValueError("Owner has not granted this exact fee wallet")
        report=verify_exact_remote_evm_policy(remote_policy,expected)
        if (report.get("remote_exact_match") is not True
                or report.get("policy_id")!=record["policy_id"]
                or report.get("policy_sha256")!=record["policy_sha256"]
                or record.get("remote_policy_sha256")!=record["policy_sha256"]):
            raise ValueError("Remote Treasury fee policy does not match owner intent")
        if not _scoped_signer(
            wallet_metadata,did=did,address=owned["address"],
            config={"signer_id":record["signer_id"],
                    "policy_id":record["policy_id"]}):
            raise ValueError("Exact owner fee additional signer not attached")
        if (type(quorum) is not dict
                or quorum.get("remote_quorum_public_key_verified") is not True):
            raise ValueError("Registered backend P-256 fee signer unverified")
    except (ValueError,TypeError,KeyError,AttributeError):
        raise HTTPException(409,"Owner's separate Privy fee grant not verified") from None
    return {
        "asset":record["asset"],"stable":record["stable"],
        "wallet_id":record["wallet_id"],
        "wallet_address":record["wallet_address"],
        "treasury_wallet":record["treasury_wallet"],
        "policy_id":record["policy_id"],
        "policy_sha256":record["policy_sha256"],
        "verified":True,
    }


def _commit_fee_grant_locked(
    cur,*,did:str,asset:str,evidence:dict,now:int,
)->dict:
    cur.execute(
        """SELECT * FROM mininex_evm_fee_auto_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    record=cur.fetchone()
    if (type(record) is not dict
            or record.get("status")!="REMOTE_UNATTACHED"
            or record.get("revoked_at") is not None
            or record.get("owner_signed_at") is None
            or any(evidence.get(key)!=record.get(key) for key in (
                "asset","stable","wallet_id","wallet_address",
                "treasury_wallet","policy_id","policy_sha256"))
            or evidence.get("verified") is not True):
        raise HTTPException(409,"Fee owner permission changed during remote verification")
    try:
        original_signed_fee_policy(record,asset=asset,now=now)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Fee policy expired or Treasury/signer changed") from None
    cur.execute(
        """UPDATE mininex_evm_fee_auto_permissions
           SET status='GRANT_VERIFIED',grant_verified_at=now(),
               updated_at=now()
           WHERE privy_did=%s AND asset=%s
             AND status='REMOTE_UNATTACHED'
             AND revoked_at IS NULL
           RETURNING status""",(did,asset))
    changed=cur.fetchone()
    if type(changed) is not dict or changed.get("status")!="GRANT_VERIFIED":
        raise HTTPException(409,"Concurrent owner fee grant confirmation")
    return {
        "asset":asset,"state":"GRANT_VERIFIED",
        "policy_id":evidence["policy_id"],
        "owner_fee_signer_granted":True,
        "owner_fee_policy_remotely_verified":True,
        "fee_payment_sent":False,
    }


async def confirm_owner_privy_fee_auto_grant(*,did:str,asset:str)->dict:
    """Only owner-requested confirmation AFTER their Privy addSigners action."""
    did=_safe_did(did);asset=_asset(asset)
    row=await asyncio.to_thread(_read_original_fee_grant,did,asset)
    if type(row) is not dict:
        raise HTTPException(409,"No exact owner-signed fee policy published")
    try:
        candidate=original_signed_fee_policy(row,asset=asset,now=int(time.time()))
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Original fee policy not valid") from None
    if row.get("status")!="REMOTE_UNATTACHED":
        raise HTTPException(409,"Fee policy not awaiting owner wallet grant")
    user=await fetch_privy_user(did)
    metadata=await fetch_privy_wallet_metadata(row["wallet_id"])
    remote=await _fetch_policy(row["policy_id"])
    quorum=await check_privy_backend_quorum()
    evidence=_verify_owner_fee_grant(
        did=did,record=row,expected=candidate,
        user=user,wallet_metadata=metadata,
        remote_policy=remote,quorum=quorum)
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _commit_fee_grant_locked(
                cur,did=did,asset=asset,evidence=evidence,
                now=int(time.time()))


async def get_owner_fee_auto_remote_grant_audit(*,did:str,asset:str)->dict:
    """Fresh Privy GET + current local consent; must be repeated before SEND.

    The prior GRANT_VERIFIED SQL label alone does not authorize wallet access.
    The sending transaction must still recheck this row under the owner lock.
    """
    did=_safe_did(did);asset=_asset(asset)
    row=await asyncio.to_thread(_read_original_fee_grant,did,asset)
    if (type(row) is not dict or row.get("status")!="GRANT_VERIFIED"
            or row.get("grant_verified_at") is None
            or row.get("revoked_at") is not None):
        raise HTTPException(409,"Wallet owner has no current fee auto consent")
    try:
        expected=original_signed_fee_policy(
            row,asset=asset,now=int(time.time()))
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Owner's fee Treasury policy not current") from None
    user=await fetch_privy_user(did)
    metadata=await fetch_privy_wallet_metadata(row["wallet_id"])
    remote=await _fetch_policy(row["policy_id"])
    quorum=await check_privy_backend_quorum()
    evidence=_verify_owner_fee_grant(
        did=did,record=row,expected=expected,user=user,
        wallet_metadata=metadata,remote_policy=remote,quorum=quorum)
    return {
        "asset":asset,"chain_id":row["chain_id"],
        "owner_wallet":row["wallet_address"],
        "treasury_wallet":row["treasury_wallet"],
        "stable":row["stable"],"wallet_id":row["wallet_id"],
        "policy_id":row["policy_id"],
        "policy_sha256":row["policy_sha256"],
        "owner_max_fee_atomic":str(row["owner_max_fee_atomic"]),
        "owner_fee_grant_remotely_verified":evidence["verified"],
        "signer_quorum_remotely_verified":True,
        "fee_transfer_sent":False,
    }
