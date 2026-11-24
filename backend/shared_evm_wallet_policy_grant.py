"""Original owner attaches the ONE scoped Privy signer to their wallet.

Separate read-only offer and independent post-Privy approval confirmation.
The frontend may call addSigners only after verified offer, and the backend
accepts GRANT_VERIFIED only after actual Privy wallet+policy GETs.

This does not convert existing per-coin LIVE workers or start trading.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_user_consent import _require_policy_roles
from .eth_privy_user_grant import _fetch_policy
from .eth_privy_remote_quorum import check_privy_backend_quorum
from .privy_live_signer import (
    _owned_eth_wallet,_scoped_signer,fetch_privy_wallet_metadata,
)
from .privy_wallet_verification import fetch_privy_user
from .shared_evm_wallet_policy_status import _read_saved_and_current_preferences
from .shared_evm_wallet_policy_prepare import _locked_selected_budget
from .shared_evm_wallet_policy_remote_review import verify_remote_combined_policy
from .shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,verified_wallet_policy_digest,
)
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.original_wallet_grant")


def _validate_original_candidate(
    *,record:dict,scopes:list[dict],did:str,
    user:dict,wallet:dict,remote:dict,now:int,
)->dict:
    if (type(record) is not dict
            or record.get("status")!="REMOTE_VERIFIED_UNATTACHED"
            or record.get("legacy_reconciliation_required") is not False
            or type(scopes) is not list or len(scopes)!=1):
        raise ValueError("Only one initial ungranted wallet policy can be offered")
    owned=_owned_eth_wallet(user,did)
    if (record.get("wallet_id")!=owned["wallet_id"]
            or record.get("wallet_address")!=owned["address"]
            or type(wallet) is not dict
            or wallet.get("id")!=owned["wallet_id"]
            or wallet.get("chain_type")!="ethereum"
            or str(wallet.get("address","")).lower()!=owned["address"]
            or type(wallet.get("entity")) is not dict
            or wallet["entity"].get("type")!="user"
            or wallet["entity"].get("id")!=did
            or wallet.get("archived_at") is not None
            or type(record.get("policy_id")) is not str
            or type(record.get("owner_id")) is not str
            or type(record.get("signer_id")) is not str):
        raise ValueError("Original Privy owner wallet or policy changed")
    args=dict(
        wallet=owned["address"],owner_id=record["owner_id"],
        executor_id=record["signer_id"],scopes=scopes,now=now,
    )
    expected=compose_scoped_evm_wallet_policy(**args)
    digest=verified_wallet_policy_digest(expected,**args)
    if (digest!=record.get("policy_sha256")
            or verify_remote_combined_policy(
                remote,expected=expected)!=record["policy_id"]):
        raise ValueError("Original user EVM policy is not exact")
    return {"owned":owned,"digest":digest,"scope":scopes[0]}


async def _read_current_original_candidate(did:str)->tuple[dict,dict,dict,dict]:
    """Read original wallet+policy from Privy, never from request data."""
    record,scopes=await asyncio.to_thread(
        _read_saved_and_current_preferences,did,
    )
    if (record is None or len(scopes)!=1
            or record.get("status")!="REMOTE_VERIFIED_UNATTACHED"
            or type(record.get("policy_id")) is not str):
        raise HTTPException(409,"No published original one-signer policy available")
    user=await fetch_privy_user(did)
    wallet=await fetch_privy_wallet_metadata(record["wallet_id"])
    policy=await _fetch_policy(record["policy_id"])
    try:
        proof=_validate_original_candidate(
            record=record,scopes=scopes,did=did,
            user=user,wallet=wallet,remote=policy,now=int(time.time()),
        )
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Original owner policy or capital needs review") from None
    return record,wallet,proof,user


async def get_original_one_signer_grant_offer(did:str)->dict:
    """Read-only, owner-specific policy to approve with ONE addSigners call."""
    did=_safe_did(did)
    record,wallet,proof,user=await _read_current_original_candidate(did)
    owner,signer=_require_policy_roles()
    if record["owner_id"]!=owner or record["signer_id"]!=signer:
        raise HTTPException(409,"Original Privy owner or signer configuration changed")
    # A successful SDK grant can outlive a failed HTTP confirmation. Read
    # the actual existing attachment rather than requiring another prompt.
    owned=proof["owned"]
    attached=(owned["delegated"] is True
        and type(wallet.get("additional_signers")) is list
        and len(wallet["additional_signers"])==1
        and _scoped_signer(wallet,did=did,address=owned["address"],
            config={"signer_id":record["signer_id"],"policy_id":record["policy_id"]}))
    if not attached and (owned["delegated"] is not False
                         or wallet.get("additional_signers")!=[]):
        raise HTTPException(409,"Original wallet has existing signer; reconcile first")
    quorum=await check_privy_backend_quorum(
        expected_quorum_id=signer,
    )
    owner_quorum=await check_privy_backend_quorum(
        expected_quorum_id=owner,
    )
    if (quorum.get("remote_quorum_public_key_verified") is not True
            or owner_quorum.get("remote_quorum_public_key_verified") is not True):
        raise HTTPException(409,"Registered Privy signer and policy owner are required")
    scope=proof["scope"]
    return {
        "mode":"ORIGINAL_EVM_ONE_SIGNER_GRANT_OFFER",
        "wallet_address":proof["owned"]["address"],
        "asset":scope["asset"],"stable":scope["stable"],
        "execution_signer_id":signer,
        "policy_id":record["policy_id"],
        "policy_sha256":proof["digest"],
        "scope_coins":[scope["asset"]],
        "settlement_policy":scope.get("settlement_policy"),
        "fee_scope":None if scope.get("settlement_policy") else {
            "treasury_wallet":scope["treasury"],
            "fee_max_atomic":scope["owner_max_fee_atomic"],
            "performance_fee_percent":5,
            "fee_basis":"REALIZED_NET_GAIN_ABOVE_HWM",
        },
        "owner_add_signer_required":not attached,
        "original_wallet_approved":bool(attached),
        "live_trading_enabled":False,"can_submit":False,
    }


def _confirm_original_wallet_grant_locked(
    cur,*,did:str,record:dict,proof:dict,now:int,
)->dict:
    """Recheck unchanged origin, per-coin settings and policy under DB lock."""
    cur.execute(
        """SELECT wallet_id,wallet_address,owner_id,signer_id,
                  policy_id,policy_sha256,status,scopes_json,revisions_json,
                  legacy_reconciliation_required
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    row=cur.fetchone()
    if (type(row) is not dict
            or row.get("status")!="REMOTE_VERIFIED_UNATTACHED"
            or row.get("legacy_reconciliation_required") is not False
            or any(row.get(key)!=record.get(key) for key in (
                "wallet_id","wallet_address","owner_id","signer_id",
                "policy_id","policy_sha256",
            ))
            or row.get("policy_sha256")!=proof["digest"]
            or type(row.get("scopes_json")) is not dict
            or type(row.get("revisions_json")) is not dict
            or len(row["scopes_json"])!=1):
        raise HTTPException(409,"Owner policy was changed after Privy approval")
    asset,scope=next(iter(row["scopes_json"].items()))
    revision=row["revisions_json"].get(asset)
    if (type(scope) is not dict or type(revision) is not int
            or scope!=proof["scope"]):
        raise HTTPException(409,"Owner coin capital changed after Privy approval")
    _locked_selected_budget(
        cur,did=did,asset=asset,stable=scope["stable"],
        expected_revision=revision,
    )
    cur.execute(
        """UPDATE mininex_live_evm_wallet_policy_grants
           SET status='GRANT_VERIFIED',original_owner_granted_at=now(),
               updated_at=now()
           WHERE privy_did=%s AND status='REMOTE_VERIFIED_UNATTACHED'
             AND policy_id=%s AND policy_sha256=%s
           RETURNING status""",
        (did,record["policy_id"],record["policy_sha256"]),
    )
    saved=cur.fetchone()
    if type(saved) is not dict or saved.get("status")!="GRANT_VERIFIED":
        raise HTTPException(409,"Original owner signer approval changed")
    return {
        "mode":"ORIGINAL_EVM_ONE_SIGNER_GRANTED",
        "state":"GRANT_VERIFIED",
        "owner_signer_grant_verified":True,
        "wallet_address":row["wallet_address"],
        "policy_id":row["policy_id"],
        "asset":asset,
        "additional_coin_grants_created":0,
        "live_auto_session_started":False,
        "transaction_sent":False,
        "can_submit":False,
    }


async def confirm_original_one_signer_grant(did:str)->dict:
    """Check real owner approval after Privy's addSigners returns successfully."""
    did=_safe_did(did)
    record,wallet,proof,user=await _read_current_original_candidate(did)
    owner,signer=_require_policy_roles()
    if record["owner_id"]!=owner or record["signer_id"]!=signer:
        raise HTTPException(409,"Original policy owner or signer changed")
    owned=proof["owned"]
    if (owned["delegated"] is not True
            or type(wallet.get("additional_signers")) is not list
            or len(wallet["additional_signers"])!=1
            or not _scoped_signer(
                wallet,did=did,address=owned["address"],
                config={
                    "signer_id":record["signer_id"],
                    "policy_id":record["policy_id"],
                },
            )):
        raise HTTPException(409,"Original wallet signer approval not confirmed by Privy")
    quorum=await check_privy_backend_quorum(
        expected_quorum_id=signer,
    )
    if quorum.get("remote_quorum_public_key_verified") is not True:
        raise HTTPException(409,"Registered backend signer no longer matches")
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _confirm_original_wallet_grant_locked(
                    cur,did=did,record=record,proof=proof,
                    now=int(time.time()),
                )
    except psycopg.Error:
        LOG.warning("Cannot persist original owner Privy grant")
        raise HTTPException(503,"Original owner grant confirmation unavailable") from None
