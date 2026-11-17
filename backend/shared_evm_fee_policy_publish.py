"""One-shot publication of a *separate* owner-approved Privy 5% fee policy.

Not the trading policy. The fee policy has one ERC-20 Treasury transfer
rule, exact chain/token/recipient, and a user-chosen atomic ceiling.
Signed owner EIP191 intent is required before any Privy policy POST.
One committed REMOTE_UNCERTAIN claim prevents repeats after any timeout.
Remote policy creation is NEVER wallet delegation: the wallet holder must
separately add the signer and confirm their exact policy in Privy.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timezone
import hashlib
import json
import logging
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_policy_transport import _auth
from .shared_evm_fee_policy_roles import require_fee_policy_roles as _require_policy_roles
from .privy_wallet_verification import APP_ID,fetch_privy_user
from .privy_live_signer import _owned_eth_wallet
from .shared_evm_owner_consent import _asset
from .shared_evm_fee_privy_policy_draft import build_owner_evm_fee_policy_draft
from .shared_evm_fee_transfer_proof import owner_fee_treasury
from .shared_evm_privy_grant_review import verify_exact_remote_evm_policy
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.owner_fee_auto_policy")
_URL="https://api.privy.io/v1/policies"


def original_signed_fee_policy(record:object,*,asset:str,now:int)->dict:
    """Trust only exact current original owner scope and offline factory."""
    if (type(record) is not dict or record.get("status") not in (
                "CONSENT_SIGNED","REMOTE_UNCERTAIN","REMOTE_UNATTACHED",
                "GRANT_VERIFIED")
            or record.get("asset")!=asset
            or not isinstance(record.get("expires_at"),datetime)
            or record["expires_at"].tzinfo is None
            or int(record["expires_at"].timestamp())<=now
            or record.get("owner_signed_at") is None
            or record.get("revoked_at") is not None
            or type(record.get("policy_json")) is not dict
            or type(record.get("policy_sha256")) is not str):
        raise ValueError("No original signed fee permission")
    pair_from=__import__("backend.shared_evm_swap_receipts",fromlist=["_pair"])._pair
    route=pair_from(asset,record["stable"],"BUY")
    owner,signer=_require_policy_roles()
    if (record.get("chain_id")!=route["chain_id"]
            or record.get("treasury_wallet") != owner_fee_treasury(
                asset=asset,stable=record["stable"])
            or record.get("owner_id")!=owner
            or record.get("signer_id")!=signer):
        raise ValueError("Fee Treasury or registered signer changed")
    expected=build_owner_evm_fee_policy_draft(
        asset=asset,stable=record["stable"],
        wallet=record["wallet_address"],treasury=record["treasury_wallet"],
        owner_id=owner,executor_id=signer,
        owner_max_fee_atomic=str(record["owner_max_fee_atomic"]),
        expires_at=int(record["expires_at"].timestamp()),now=now)
    digest=hashlib.sha256(json.dumps(
        expected,sort_keys=True,separators=(",",":")
    ).encode("utf-8")).hexdigest()
    if record["policy_json"]!=expected or record["policy_sha256"]!=digest:
        raise ValueError("Remote fee policy candidate changed since owner consent")
    return expected


def _claim_fee_policy_locked(
    cur,*,did:str,asset:str,owned:dict,now:int,
)->dict:
    cur.execute(
        """SELECT * FROM mininex_evm_fee_auto_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    record=cur.fetchone()
    if (type(record) is not dict or record.get("status")!="CONSENT_SIGNED"
            or record.get("wallet_id")!=owned["wallet_id"]
            or record.get("wallet_address")!=owned["address"]
            or record.get("policy_id") is not None
            or record.get("policy_attempted_at") is not None):
        raise HTTPException(409,"No unsubmitted wallet-signed fee policy")
    candidate=original_signed_fee_policy(record,asset=asset,now=now)
    key=hashlib.sha256(json.dumps(
        [did,asset,record["wallet_id"],record["policy_sha256"]],
        separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    cur.execute(
        """UPDATE mininex_evm_fee_auto_permissions
           SET status='REMOTE_UNCERTAIN',idempotency_key=%s,
               policy_attempted_at=now(),updated_at=now()
           WHERE privy_did=%s AND asset=%s
             AND status='CONSENT_SIGNED' AND policy_attempted_at IS NULL
           RETURNING status""",(key,did,asset))
    changed=cur.fetchone()
    if type(changed) is not dict or changed.get("status")!="REMOTE_UNCERTAIN":
        raise HTTPException(409,"Concurrent original fee policy claim")
    return {"asset":asset,"candidate":candidate,
            "policy_sha256":record["policy_sha256"],
            "idempotency_key":key}


async def _post_original_fee_policy_once(
    *,claim:dict,client:httpx.AsyncClient,
)->dict:
    if (type(claim) is not dict or type(claim.get("candidate")) is not dict
            or type(claim.get("idempotency_key")) is not str):
        raise ValueError("Missing immutable original fee policy claim")
    secret=_auth()
    headers={"privy-app-id":APP_ID,
             "privy-idempotency-key":claim["idempotency_key"],
             "Accept":"application/json",
             "Content-Type":"application/json"}
    created=await client.post(
        _URL,json=claim["candidate"],auth=secret,headers=headers)
    created.raise_for_status()
    if len(created.content)>256000:
        raise ValueError("Privy fee policy POST produced oversized response")
    payload=created.json()
    policy_id=payload.get("id") if type(payload) is dict else None
    if type(policy_id) is not str or not 6<=len(policy_id)<=160:
        raise ValueError("Remote fee policy ID unavailable")
    get=await client.get(
        _URL+"/"+policy_id,auth=secret,
        headers={"privy-app-id":APP_ID,"Accept":"application/json"})
    get.raise_for_status()
    if len(get.content)>256000:
        raise ValueError("Privy owner fee policy GET oversized")
    reviewed=verify_exact_remote_evm_policy(get.json(),claim["candidate"])
    if (reviewed["policy_id"]!=policy_id
            or reviewed["policy_sha256"]!=claim["policy_sha256"]
            or reviewed.get("remote_exact_match") is not True):
        raise ValueError("Privy fee scope differs from signed owner's original")
    return reviewed


def _finish_fee_policy_locked(
    cur,*,did:str,asset:str,claim:dict,remote:dict,
)->dict:
    cur.execute(
        """SELECT * FROM mininex_evm_fee_auto_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    record=cur.fetchone()
    if (type(record) is not dict or record.get("status")!="REMOTE_UNCERTAIN"
            or record.get("revoked_at") is not None
            or record.get("policy_sha256")!=claim["policy_sha256"]
            or record.get("idempotency_key")!=claim["idempotency_key"]
            or remote.get("remote_exact_match") is not True
            or remote.get("policy_sha256")!=claim["policy_sha256"]
            or type(remote.get("policy_id")) is not str):
        raise HTTPException(409,"Privy original fee publication must reconcile")
    cur.execute(
        """UPDATE mininex_evm_fee_auto_permissions
           SET status='REMOTE_UNATTACHED',policy_id=%s,
               remote_policy_sha256=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s
             AND status='REMOTE_UNCERTAIN' AND revoked_at IS NULL
           RETURNING status""",
        (remote["policy_id"],remote["policy_sha256"],did,asset))
    updated=cur.fetchone()
    if type(updated) is not dict or updated.get("status")!="REMOTE_UNATTACHED":
        raise HTTPException(409,"Original fee policy state changed after Privy GET")
    return {
        "asset":asset,"policy_id":remote["policy_id"],
        "state":"REMOTE_UNATTACHED",
        "privy_policy_rules_verified":True,
        "privy_wallet_owner_add_signer_still_required":True,
        "automatic_fee_transfer_enabled":False,
    }


async def publish_owner_signed_fee_policy(
    *,did:str,asset:str,
)->dict:
    """Explicit authenticated wallet-owner action, no auto signer attachment."""
    did=_safe_did(did); asset=_asset(asset)
    owned=_owned_eth_wallet(await fetch_privy_user(did),did)
    db=_db_url();_ensure_schema(db)
    now=int(time.time())
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            claim=_claim_fee_policy_locked(
                cur,did=did,asset=asset,owned=owned,now=now)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False) as client:
            remote=await _post_original_fee_policy_once(
                claim=claim,client=client)
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        # An unknown network reply may have created a policy remotely.
        # Do NOT retry a second POST; owner reconciliation required.
        raise HTTPException(
            503,"Original Privy fee policy outcome uncertain; never re-POST") from None
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _finish_fee_policy_locked(
                cur,did=did,asset=asset,claim=claim,remote=remote)
