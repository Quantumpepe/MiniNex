"""One-time Privy policy creation for the user's original ONE EVM signer.

Requires a previously staged server-priced single-wallet draft. Never POST
twice after SUBMITTING: uncertain outcomes remain UNCERTAIN and must be
reconciled by an independent Privy read. Creating a policy does not grant
its signer access to any wallet, enable LIVE orders or change a fee ledger.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_policy_transport import _auth
from .eth_user_consent import _require_policy_roles
from .privy_wallet_verification import APP_ID,fetch_privy_user
from .privy_live_signer import _owned_eth_wallet,fetch_privy_wallet_metadata
from .shared_evm_wallet_policy_prepare import (
    _require_unattached_original_wallet,_locked_selected_budget,
)
from .shared_evm_wallet_policy_remote_review import verify_remote_combined_policy
from .shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,verified_wallet_policy_digest,
)
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.one_signer_policy_publish")
_URL="https://api.privy.io/v1/policies"


def _publication_key(*,did:str,wallet_id:str,digest:str)->str:
    return hashlib.sha256(json.dumps({
        "mode":"CENTRAL_EVM_INITIAL_POLICY_ONCE",
        "did":did,"wallet_id":wallet_id,"policy_sha256":digest,
    },sort_keys=True,separators=(",",":")).encode("ascii")).hexdigest()


def _claim_first_policy_locked(
    cur,*,did:str,owned:dict,owner_id:str,signer_id:str,now:int,
)->dict:
    """Claim a COMMITTED one-time remote publish attempt under wallet lock."""
    cur.execute(
        """SELECT wallet_id,wallet_address,owner_id,signer_id,
                  policy_id,policy_sha256,scopes_json,revisions_json,
                  status,legacy_reconciliation_required
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    record=cur.fetchone()
    if (type(record) is not dict or record.get("status")!="DRAFT"
            or record.get("policy_id") is not None
            or record.get("legacy_reconciliation_required") is not False
            or record.get("wallet_id")!=owned["wallet_id"]
            or record.get("wallet_address")!=owned["address"]
            or record.get("owner_id")!=owner_id
            or record.get("signer_id")!=signer_id
            or type(record.get("scopes_json")) is not dict
            or type(record.get("revisions_json")) is not dict
            or len(record["scopes_json"])!=1
            or set(record["scopes_json"])!=set(record["revisions_json"])):
        raise HTTPException(409,"One initial owner wallet policy draft required")
    asset,scope=next(iter(record["scopes_json"].items()))
    revision=record["revisions_json"][asset]
    if (type(scope) is not dict or scope.get("asset")!=asset
            or type(scope.get("stable")) is not str
            or type(revision) is not int or revision<1):
        raise HTTPException(409,"Original coin budget policy snapshot invalid")
    _locked_selected_budget(
        cur,did=did,asset=asset,stable=scope["stable"],
        expected_revision=revision,
    )
    args=dict(
        wallet=owned["address"],owner_id=owner_id,
        executor_id=signer_id,scopes=[scope],now=now,
    )
    try:
        policy=compose_scoped_evm_wallet_policy(**args)
        digest=verified_wallet_policy_digest(policy,**args)
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"Original wallet policy has expired or changed") from None
    if digest!=record.get("policy_sha256"):
        raise HTTPException(409,"Original wallet policy digest mismatch")
    key=_publication_key(
        did=did,wallet_id=owned["wallet_id"],digest=digest,
    )
    cur.execute(
        """UPDATE mininex_live_evm_wallet_policy_grants
           SET status='SUBMITTING',publication_idempotency_key=%s,
               last_attempt_at=now(),updated_at=now()
           WHERE privy_did=%s AND status='DRAFT' AND policy_sha256=%s
           RETURNING status""",(key,did,digest),
    )
    updated=cur.fetchone()
    if type(updated) is not dict or updated.get("status")!="SUBMITTING":
        raise HTTPException(409,"Concurrent original policy publication prevented")
    return {
        "asset":asset,"wallet_id":owned["wallet_id"],
        "wallet_address":owned["address"],
        "owner_id":owner_id,"signer_id":signer_id,
        "candidate":policy,"policy_sha256":digest,
        "idempotency_key":key,
        "budget_revision":revision,
    }


async def _post_exact_initial_policy_once(*,claimed:dict,client:httpx.AsyncClient)->dict:
    """Single POST and exact independent GET; never attach an additional signer."""
    if (type(claimed) is not dict
            or type(claimed.get("candidate")) is not dict
            or type(claimed.get("idempotency_key")) is not str):
        raise ValueError("A previously committed publication claim is required")
    headers={
        "privy-app-id":APP_ID,
        "privy-idempotency-key":claimed["idempotency_key"],
        "Accept":"application/json",
        "Content-Type":"application/json",
    }
    response=await client.post(
        _URL,json=claimed["candidate"],auth=_auth(),headers=headers,
    )
    response.raise_for_status()
    if len(response.content)>256000:
        raise ValueError("Oversized Privy policy response")
    created=response.json()
    policy_id=created.get("id") if type(created) is dict else None
    if type(policy_id) is not str or not 6<=len(policy_id)<=160:
        raise ValueError("Missing published policy ID")
    readback=await client.get(
        _URL+"/"+policy_id,auth=_auth(),
        headers={"privy-app-id":APP_ID,"Accept":"application/json"},
    )
    readback.raise_for_status()
    if len(readback.content)>256000:
        raise ValueError("Oversized Privy policy readback")
    verified=verify_remote_combined_policy(
        readback.json(),expected=claimed["candidate"],
    )
    if verified!=policy_id:
        raise ValueError("Published original wallet policy changed")
    return {"policy_id":policy_id,
            "policy_sha256":claimed["policy_sha256"],
            "remote_exact_match":True}


def _finish_first_policy_locked(
    cur,*,did:str,claimed:dict,remote:dict|None,
)->dict:
    """Record verified UNATTACHED policy or permanently uncertain outcome."""
    cur.execute(
        """SELECT status,policy_id,policy_sha256,publication_idempotency_key,
                  wallet_id,signer_id
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    current=cur.fetchone()
    if (type(current) is not dict or current.get("status")!="SUBMITTING"
            or current.get("policy_id") is not None
            or current.get("policy_sha256")!=claimed["policy_sha256"]
            or current.get("publication_idempotency_key")!=claimed["idempotency_key"]
            or current.get("wallet_id")!=claimed["wallet_id"]
            or current.get("signer_id")!=claimed["signer_id"]):
        raise HTTPException(409,"Original policy publication already reconciled or changed")
    valid=(type(remote) is dict
           and remote.get("remote_exact_match") is True
           and remote.get("policy_sha256")==claimed["policy_sha256"]
           and type(remote.get("policy_id")) is str
           and 6<=len(remote["policy_id"])<=160)
    if not valid:
        cur.execute(
            """UPDATE mininex_live_evm_wallet_policy_grants
               SET status='UNCERTAIN',updated_at=now()
               WHERE privy_did=%s AND status='SUBMITTING'
               RETURNING status""",(did,),
        )
        row=cur.fetchone()
        if type(row) is not dict or row.get("status")!="UNCERTAIN":
            raise HTTPException(409,"Original policy uncertainty could not be recorded")
        return {
            "status":"UNCERTAIN",
            "remote_publish_not_retried":True,
            "wallet_additional_signer_attached":False,
            "can_submit":False,
        }
    cur.execute(
        """UPDATE mininex_live_evm_wallet_policy_grants
           SET status='REMOTE_VERIFIED_UNATTACHED',
               policy_id=%s,remote_verified_at=now(),
               updated_at=now()
           WHERE privy_did=%s AND status='SUBMITTING'
           RETURNING status""",(remote["policy_id"],did),
    )
    updated=cur.fetchone()
    if type(updated) is not dict or updated.get("status")!="REMOTE_VERIFIED_UNATTACHED":
        raise HTTPException(409,"Original Privy policy publication state changed")
    return {
        "status":"REMOTE_VERIFIED_UNATTACHED",
        "policy_id":remote["policy_id"],
        "execution_signer_id":claimed["signer_id"],
        "policy_sha256":claimed["policy_sha256"],
        "asset":claimed["asset"],
        "wallet_additional_signer_attached":False,
        "owner_must_approve_add_signers_once":True,
        "can_submit":False,
    }


async def publish_initial_owner_wallet_policy(did:str)->dict:
    """Explicit owner click: publish once; any uncertain response never retries."""
    did=_safe_did(did)
    owner,signer=_require_policy_roles()
    user=await fetch_privy_user(did)
    preowned=_owned_eth_wallet(user,did)
    metadata=await fetch_privy_wallet_metadata(preowned["wallet_id"])
    owned=_require_unattached_original_wallet(user,did,metadata)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                claimed=_claim_first_policy_locked(
                    cur,did=did,owned=owned,
                    owner_id=owner,signer_id=signer,now=int(time.time()),
                )
    except psycopg.Error:
        LOG.warning("Cannot commit original wallet policy publish claim")
        raise HTTPException(503,"Original wallet policy publication unavailable") from None

    # The claim has COMMITTED. Even connection timeout is an uncertain POST:
    # no automatic replay and no second POST from this request or a retry.
    remote=None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9.0),follow_redirects=False,
        ) as client:
            remote=await _post_exact_initial_policy_once(
                claimed=claimed,client=client,
            )
    except (httpx.HTTPError,ValueError,TypeError,KeyError):
        LOG.warning("One-signer original policy POST/readback outcome uncertain")
    try:
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                result=_finish_first_policy_locked(
                    cur,did=did,claimed=claimed,remote=remote,
                )
    except psycopg.Error:
        LOG.warning("Cannot persist initial original-wallet Privy outcome")
        raise HTTPException(503,"Original policy reconciliation required") from None
    if result["status"]!="REMOTE_VERIFIED_UNATTACHED":
        raise HTTPException(503,"Original Privy policy outcome uncertain; do not retry")
    return result
