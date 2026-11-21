"""Owner-initiated once-only Privy policy publication for five EVM coins.

An EIP191 signed intent and locally pinned policy are prerequisite to
a single Privy POST. A timeout is UNCERTAIN, never an excuse to retry. A
remote verified unattached policy does NOT grant wallet access: only the
owner can use Privy's separate restricted addSigners workflow.
"""
from __future__ import annotations

from datetime import datetime,timezone
import asyncio
import hashlib
import json
import logging
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_policy_candidate_stage import _locked_coin_settings
from .shared_evm_remote_grant_audit import validate_pinned_evm_policy
from .shared_evm_privy_grant_review import verify_exact_remote_evm_policy
from .shared_evm_owner_consent import _asset
from .privy_wallet_verification import APP_ID,fetch_privy_user
from .privy_live_signer import _owned_eth_wallet
from .shared_evm_trade_policy_roles import require_coin_trade_policy_roles as _require_policy_roles
from .eth_privy_policy_transport import _auth
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.evm_privy_policy_publication")
_POLICY_URL="https://api.privy.io/v1/policies"


def _idempotency_key(*,did:str,asset:str,wallet:str,digest:str)->str:
    return hashlib.sha256(json.dumps({
        "did":did,"asset":asset,"wallet":wallet,"policy_sha256":digest,
    },sort_keys=True,separators=(",",":")).encode("ascii")).hexdigest()


def _claim_policy_publication_locked(
    cur,*,did:str,asset:str,owned:dict,
    owner_id:str,signer_id:str,now:int,
)->dict:
    """One permanent, committed SUBMITTING claim before remote POST."""
    asset=_asset(asset)
    settings=_locked_coin_settings(cur,did=did,asset=asset)
    cur.execute(
        """SELECT asset,chain_id,stable,wallet_id,wallet_address,
                  owner_id,signer_id,policy_id,policy_sha256,
                  candidate_json,consent_sha256,expires_at,status,revoked_at,
                  budget_revision,authorization_method
           FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    policy=cur.fetchone()
    if (type(policy) is not dict
            or policy.get("status")!="DRAFT"
            or policy.get("policy_id") is not None
            or policy.get("revoked_at") is not None
            or policy.get("wallet_id")!=owned["wallet_id"]
            or policy.get("wallet_address")!=owned["address"]
            or policy.get("owner_id")!=owner_id
            or policy.get("signer_id")!=signer_id
            or policy.get("budget_revision")!=settings["revision"]
            or policy.get("stable")!=settings["stable"]):
        raise HTTPException(409,"No current signed owner EVM policy draft")
    if policy.get("authorization_method")=="PRIVY_SCOPED_GRANT":
        # Creating a restrictive policy never grants access to funds.
        if policy.get("consent_sha256") is not None:
            raise HTTPException(409,"Unexpected premature owner authorization")
    else:
        cur.execute(
            """SELECT status,wallet_id,wallet_address,stable,chain_id,
                      budget_revision,message_sha256,confirmed_at
               FROM mininex_live_evm_consent_challenges
               WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
        consent=cur.fetchone()
        if (type(consent) is not dict
                or consent.get("status")!="SIGNED_INTENT"
                or consent.get("confirmed_at") is None
                or consent.get("wallet_id")!=policy["wallet_id"]
                or consent.get("wallet_address")!=policy["wallet_address"]
                or consent.get("stable")!=policy["stable"]
                or consent.get("chain_id")!=policy["chain_id"]
                or consent.get("budget_revision")!=policy["budget_revision"]
                or consent.get("message_sha256")!=policy.get("consent_sha256")):
            raise HTTPException(409,"Original coin enrollment intent was revoked")
    try:
        candidate=validate_pinned_evm_policy(
            policy,asset=asset,now=now)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Privy candidate no longer matches approved policy") from None
    key=_idempotency_key(
        did=did,asset=asset,wallet=policy["wallet_address"],
        digest=policy["policy_sha256"])
    cur.execute(
        """INSERT INTO mininex_live_evm_policy_publications
           (privy_did,asset,policy_sha256,idempotency_key,status)
           VALUES (%s,%s,%s,%s,'SUBMITTING')
           ON CONFLICT(privy_did,asset) DO NOTHING RETURNING status""",
        (did,asset,policy["policy_sha256"],key))
    row=cur.fetchone()
    if type(row) is not dict or row.get("status")!="SUBMITTING":
        raise HTTPException(409,"Privy policy already attempted; reconcile, never retry")
    return {"asset":asset,"candidate":candidate,
            "policy_sha256":policy["policy_sha256"],
            "idempotency_key":key}


async def _post_policy_once(
    *,claimed:dict,client:httpx.AsyncClient,
)->dict:
    """Single create POST then independently GET; no remote signer grants."""
    if (type(claimed) is not dict
            or type(claimed.get("candidate")) is not dict
            or type(claimed.get("idempotency_key")) is not str):
        raise ValueError("Missing durable Privy publication claim")
    auth=_auth()
    headers={
        "privy-app-id":APP_ID,
        "privy-idempotency-key":claimed["idempotency_key"],
        "Accept":"application/json",
        "Content-Type":"application/json",
    }
    # Never issue a second POST in this helper, even on network timeout.
    response=await client.post(
        _POLICY_URL,json=claimed["candidate"],
        auth=auth,headers=headers)
    response.raise_for_status()
    if len(response.content)>256000:
        raise ValueError("Oversized Privy policy creation response")
    created=response.json()
    policy_id=created.get("id") if type(created) is dict else None
    if type(policy_id) is not str or len(policy_id)<6:
        raise ValueError("Missing remote EVM policy ID")
    fetched=await client.get(
        _POLICY_URL+"/"+policy_id,
        auth=auth,headers={"privy-app-id":APP_ID,"Accept":"application/json"})
    fetched.raise_for_status()
    if len(fetched.content)>256000:
        raise ValueError("Oversized remote Privy policy")
    remote=fetched.json()
    verified=verify_exact_remote_evm_policy(remote,claimed["candidate"])
    if (verified["policy_id"]!=policy_id
            or verified["policy_sha256"]!=claimed["policy_sha256"]):
        raise ValueError("Privy policy remote rules mismatch")
    return verified


def _finish_policy_publication_locked(
    cur,*,did:str,asset:str,claimed:dict,remote:dict|None,
)->dict:
    cur.execute(
        """SELECT status,policy_sha256,idempotency_key
           FROM mininex_live_evm_policy_publications
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    record=cur.fetchone()
    if (type(record) is not dict
            or record.get("status")!="SUBMITTING"
            or record.get("policy_sha256")!=claimed["policy_sha256"]
            or record.get("idempotency_key")!=claimed["idempotency_key"]):
        raise HTTPException(409,"Cannot reconcile changed policy submission")
    cur.execute(
        """SELECT status,revoked_at,policy_sha256,authorization_method
           FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    original=cur.fetchone()
    if (type(original) is not dict or original.get("status")!="DRAFT"
            or original.get("revoked_at") is not None
            or original.get("policy_sha256")!=claimed["policy_sha256"]):
        raise HTTPException(409,"Owner revoked the pending Privy policy")
    success=(
        type(remote) is dict
        and remote.get("remote_exact_match") is True
        and remote.get("policy_sha256")==claimed["policy_sha256"]
        and type(remote.get("policy_id")) is str
        and len(remote["policy_id"])>=6)
    if not success:
        cur.execute(
            """UPDATE mininex_live_evm_policy_publications
               SET status='UNCERTAIN',updated_at=now()
               WHERE privy_did=%s AND asset=%s AND status='SUBMITTING'
               RETURNING status""",(did,asset))
        row=cur.fetchone()
        if type(row) is not dict or row.get("status")!="UNCERTAIN":
            raise HTTPException(409,"Cannot hold Privy policy in uncertain state")
        return {
            "status":"UNCERTAIN","remote_policy_not_retried":True,
            "wallet_signer_attached":False,"can_submit":False,
        }
    cur.execute(
        """UPDATE mininex_live_evm_policy_publications
           SET status='REMOTE_VERIFIED_UNATTACHED',policy_id=%s,
               updated_at=now()
           WHERE privy_did=%s AND asset=%s AND status='SUBMITTING'
           RETURNING status""",(remote["policy_id"],did,asset))
    stored=cur.fetchone()
    if type(stored) is not dict or stored.get("status")!="REMOTE_VERIFIED_UNATTACHED":
        raise HTTPException(409,"Remote policy publication conflict")
    cur.execute(
        """UPDATE mininex_live_evm_owner_permissions
           SET status=CASE
                 WHEN authorization_method='PRIVY_SCOPED_GRANT'
                 THEN 'DRAFT' ELSE 'CONSENT_VERIFIED' END,
               policy_id=%s,remote_policy_sha256=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND status='DRAFT'
           RETURNING status""",
        (remote["policy_id"],remote["policy_sha256"],did,asset))
    active=cur.fetchone()
    expected=("DRAFT" if
        original.get("authorization_method")=="PRIVY_SCOPED_GRANT"
        else "CONSENT_VERIFIED")
    if type(active) is not dict or active.get("status")!=expected:
        raise HTTPException(409,"Remote Privy policy identity could not persist")
    return {
        "status":"REMOTE_VERIFIED_UNATTACHED",
        "policy_id":remote["policy_id"],"asset":asset,
        "remote_exact_rules_verified":True,
        "wallet_signer_attached":False,
        "separate_privy_owner_add_signer_required":True,
        "can_submit":False,"live_auto_enabled":False,
    }


def _claim(did:str,asset:str,owned:dict,owner_id:str,
           signer_id:str,now:int)->dict:
    url=_db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _claim_policy_publication_locked(
                cur,did=did,asset=asset,owned=owned,
                owner_id=owner_id,signer_id=signer_id,now=now)


def _finish(did:str,asset:str,claimed:dict,remote:dict|None)->dict:
    url=_db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _finish_policy_publication_locked(
                cur,did=did,asset=asset,claimed=claimed,remote=remote)


async def publish_user_evm_privy_policy(did:str,asset:str)->dict:
    """Explicit user POST after EIP191 intent; never creates wallet grant."""
    did=_safe_did(did)
    asset=_asset(asset)
    user=await fetch_privy_user(did)
    owned=_owned_eth_wallet(user,did)
    owner_id,signer_id=_require_policy_roles(asset)
    now=int(time.time())
    try:
        claimed=await asyncio.to_thread(
            _claim,did,asset,owned,owner_id,signer_id,now)
    except psycopg.Error:
        raise HTTPException(503,"Cannot claim Privy policy publication") from None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            remote=await _post_policy_once(claimed=claimed,client=client)
    except Exception:
        # Could already exist remotely. No repeat POST. Keep UNCERTAIN and
        # preserve the once-only claim for manual policy reconciliation.
        try:
            await asyncio.to_thread(_finish,did,asset,claimed,None)
        except psycopg.Error:
            LOG.error("Uncertain Privy policy outcome could not be stored")
        raise HTTPException(503,"Remote Privy policy outcome requires reconciliation") from None
    try:
        return await asyncio.to_thread(_finish,did,asset,claimed,remote)
    except psycopg.Error:
        raise HTTPException(503,"Remote Privy policy persistence unavailable") from None
