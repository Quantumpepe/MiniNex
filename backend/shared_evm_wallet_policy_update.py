"""Apply a ONE-TIME owner-signed Privy PATCH for an original EVM wallet.

The original user signs a scoped EIP-191 intent before this module runs.
An irreversible database claim is committed before any remote PATCH.
Unknown responses are never retried; the wallet remains locally on hold.
This does not add signers, enable an order, or debit a balance.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .eth_privy_policy_transport import _auth
from .eth_privy_user_grant import _fetch_policy
from .eth_privy_remote_quorum import check_privy_backend_quorum
from .privy_wallet_verification import APP_ID, fetch_privy_user
from .privy_live_signer import (
    _owned_eth_wallet, _scoped_signer, fetch_privy_wallet_metadata,
)
from .shared_evm_wallet_policy_extend import _verified_previous_grant
from .shared_evm_wallet_policy_prepare import _locked_selected_budget
from .shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy, verified_wallet_policy_digest, changed_wallet_coin,
)
from .shared_evm_wallet_policy_owner_signature import sign_exact_privy_owner_patch
from .shared_evm_wallet_policy_remote_review import verify_remote_combined_policy
from .storage import _db_url, _ensure_schema, _safe_did

LOG=logging.getLogger("mininex.original_wallet_policy_update")
_NONCE=re.compile(r"^[0-9a-f]{64}$")
_BASE="https://api.privy.io/v1/policies/"


def _key(*,did,nonce,source,target,policy_id):
    raw=json.dumps({
        "did":did,"nonce":nonce,"source":source,"target":target,
        "policy_id":policy_id,"operation":"ONE_ORIGINAL_SIGNER_PATCH",
    },sort_keys=True,separators=(",",":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _load_consented_coin(did,nonce):
    """No new coin is ever chosen by untrusted request JSON."""
    url=_db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT g.status AS grant_status,g.scopes_json,
                          i.status AS intent_status,i.target_scopes_json
                   FROM mininex_live_evm_wallet_policy_grants g
                   JOIN mininex_live_evm_wallet_policy_intents i
                     ON i.privy_did=g.privy_did
                   WHERE g.privy_did=%s AND i.nonce=%s""",(did,nonce),
            )
            row=cur.fetchone()
    if (type(row) is not dict
            or row.get("grant_status")!="GRANT_VERIFIED"
            or row.get("intent_status")!="SIGNED_INTENT"
            or type(row.get("scopes_json")) is not dict
            or type(row.get("target_scopes_json")) is not dict):
        raise HTTPException(409,"Original signed wallet extension unavailable")
    try:
        return changed_wallet_coin(row["scopes_json"],row["target_scopes_json"])
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None


def _claim_locked(cur,*,did,nonce,observed,old_scopes,now):
    """Claim once under the wallet lock; no remote call until commit."""
    cur.execute(
        """SELECT wallet_id,wallet_address,owner_id,signer_id,policy_id,
                  policy_sha256,status,revision,scopes_json,revisions_json,
                  legacy_reconciliation_required
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    g=cur.fetchone()
    keys=("wallet_id","wallet_address","owner_id","signer_id",
          "policy_id","policy_sha256")
    if (type(g) is not dict
            or g.get("status")!="GRANT_VERIFIED"
            or g.get("legacy_reconciliation_required") is not False
            or type(g.get("revision")) is not int or g["revision"]<=0
            or any(g.get(k)!=observed.get(k) for k in keys)
            or type(g.get("scopes_json")) is not dict
            or g["scopes_json"]!={x["asset"]:x for x in old_scopes}
            or type(g.get("revisions_json")) is not dict):
        raise HTTPException(409,"Original wallet permission changed")
    cur.execute(
        """SELECT status,source_grant_revision,previous_policy_sha256,
                  target_policy_sha256,target_scopes_json,target_revisions_json,
                  signed_at,update_idempotency_key,
                  extract(epoch FROM expires_at)::bigint AS expires_at_unix
           FROM mininex_live_evm_wallet_policy_intents
           WHERE privy_did=%s AND nonce=%s FOR UPDATE""",(did,nonce),
    )
    intent=cur.fetchone()
    if (type(intent) is not dict or intent.get("status")!="SIGNED_INTENT"
            or intent.get("signed_at") is None
            or intent.get("update_idempotency_key") is not None
            or intent.get("source_grant_revision")!=g["revision"]
            or intent.get("previous_policy_sha256")!=g["policy_sha256"]
            or type(intent.get("expires_at_unix")) is not int
            or now>=intent["expires_at_unix"]
            or type(intent.get("target_scopes_json")) is not dict
            or type(intent.get("target_revisions_json")) is not dict):
        raise HTTPException(409,"Signed user permission expired or already sent")
    target=intent["target_scopes_json"]
    revised=intent["target_revisions_json"]
    old=g["scopes_json"]
    try:
        changed=changed_wallet_coin(old,target,g["revisions_json"],revised)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None
    for asset in sorted(target):
        scope=target[asset]
        rev=revised[asset]
        if type(scope) is not dict or type(rev) is not int or rev<=0:
            raise HTTPException(409,"Requested coin budget revision invalid")
        if asset==changed:
            _locked_selected_budget(
                cur,did=did,asset=asset,stable=scope["stable"],
                expected_revision=rev,
            )
    args=dict(wallet=g["wallet_address"],owner_id=g["owner_id"],
              executor_id=g["signer_id"],scopes=list(target.values()),now=now,allow_expired=True)
    policy=compose_scoped_evm_wallet_policy(**args)
    digest=verified_wallet_policy_digest(policy,**args)
    if digest!=intent.get("target_policy_sha256"):
        raise HTTPException(409,"Wallet owner did not sign this policy digest")
    key=_key(
        did=did,nonce=nonce,source=g["policy_sha256"],target=digest,
        policy_id=g["policy_id"],
    )
    cur.execute(
        """UPDATE mininex_live_evm_wallet_policy_intents
           SET status='PATCH_SUBMITTING',update_idempotency_key=%s,
               update_submitted_at=now()
           WHERE privy_did=%s AND nonce=%s AND status='SIGNED_INTENT'
           RETURNING status""",(key,did,nonce),
    )
    x=cur.fetchone()
    if type(x) is not dict or x.get("status")!="PATCH_SUBMITTING":
        raise HTTPException(409,"Concurrent wallet policy update already claimed")
    cur.execute(
        """UPDATE mininex_live_evm_wallet_policy_grants
           SET status='UPDATING',updated_at=now()
           WHERE privy_did=%s AND status='GRANT_VERIFIED'
             AND policy_sha256=%s AND revision=%s
           RETURNING status""",(did,g["policy_sha256"],g["revision"]),
    )
    y=cur.fetchone()
    if type(y) is not dict or y.get("status")!="UPDATING":
        raise HTTPException(409,"Original wallet policy claim conflict")
    return {
        "did":did,"nonce":nonce,"policy_id":g["policy_id"],
        "wallet_id":g["wallet_id"],"wallet_address":g["wallet_address"],
        "signer_id":g["signer_id"],"owner_id":g["owner_id"],
        "old_digest":g["policy_sha256"],"old_revision":g["revision"],
        "target_digest":digest,"target_scopes_json":target,
        "target_revisions_json":revised,"idempotency_key":key,
        "candidate":policy,"new_coin":changed,
    }


async def _patch_once(*,claim,client):
    """One owner-authorized PATCH and exact Privy GET, no retry."""
    signed=sign_exact_privy_owner_patch(
        url=_BASE+claim["policy_id"],candidate=claim["candidate"],
        idempotency_key=claim["idempotency_key"],
        expiry_unix_ms=int(time.time()*1000)+120_000,
        owner_quorum_verified=True,
    )
    response=await client.patch(
        signed["url"],json=signed["body"],
        headers=signed["headers"],auth=_auth(),
    )
    response.raise_for_status()
    if len(response.content)>256000:
        raise ValueError("Oversized Privy PATCH response")
    fetched=await client.get(
        signed["url"],auth=_auth(),
        headers={"privy-app-id":APP_ID,"Accept":"application/json"},
    )
    fetched.raise_for_status()
    if len(fetched.content)>256000 or not fetched.content:
        raise ValueError("Missing Privy updated policy GET")
    if verify_remote_combined_policy(
        fetched.json(),expected=claim["candidate"],
    )!=claim["policy_id"]:
        raise ValueError("Privy remote policy differs from signed coin scope")
    user=await fetch_privy_user(claim["did"])
    wallet=await fetch_privy_wallet_metadata(claim["wallet_id"])
    owned=_owned_eth_wallet(user,claim["did"])
    if (owned["delegated"] is not True
            or owned["wallet_id"]!=claim["wallet_id"]
            or owned["address"]!=claim["wallet_address"]
            or type(wallet.get("additional_signers")) is not list
            or len(wallet["additional_signers"])!=1
            or not _scoped_signer(
                wallet,did=claim["did"],address=owned["address"],
                config={
                    "signer_id":claim["signer_id"],
                    "policy_id":claim["policy_id"],
                })):
        raise ValueError("Original wallet's one delegated signer changed")
    return {
        "policy_id":claim["policy_id"],"digest":claim["target_digest"],
        "one_signer_verified":True,"remote_rules_verified":True,
    }


def _finish_locked(cur,*,did,claim,remote):
    """Atomic result: only exact target+budgets may promote original wallet."""
    cur.execute(
        """SELECT wallet_id,signer_id,policy_id,policy_sha256,status,revision
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    grant=cur.fetchone()
    cur.execute(
        """SELECT status,source_grant_revision,previous_policy_sha256,
                  target_policy_sha256,target_scopes_json,target_revisions_json,
                  update_idempotency_key
           FROM mininex_live_evm_wallet_policy_intents
           WHERE privy_did=%s AND nonce=%s FOR UPDATE""",(did,claim["nonce"]),
    )
    intent=cur.fetchone()
    if (type(grant) is not dict or type(intent) is not dict
            or grant.get("status") not in ("UPDATING","UPDATE_UNCERTAIN")
            or grant.get("wallet_id")!=claim["wallet_id"]
            or grant.get("signer_id")!=claim["signer_id"]
            or grant.get("policy_id")!=claim["policy_id"]
            or grant.get("policy_sha256")!=claim["old_digest"]
            or grant.get("revision")!=claim["old_revision"]
            or intent.get("status") not in ("PATCH_SUBMITTING","PATCH_UNCERTAIN")
            or intent.get("source_grant_revision")!=claim["old_revision"]
            or intent.get("previous_policy_sha256")!=claim["old_digest"]
            or intent.get("target_policy_sha256")!=claim["target_digest"]
            or intent.get("target_scopes_json")!=claim["target_scopes_json"]
            or intent.get("target_revisions_json")!=claim["target_revisions_json"]
            or intent.get("update_idempotency_key")!=claim["idempotency_key"]):
        raise HTTPException(409,"Original Privy PATCH requires operator audit")
    verified=(type(remote) is dict and remote.get("one_signer_verified") is True
              and remote.get("remote_rules_verified") is True
              and remote.get("policy_id")==claim["policy_id"]
              and remote.get("digest")==claim["target_digest"])
    if verified:
        try:
            for asset in sorted(claim["target_scopes_json"]):
                scope=claim["target_scopes_json"][asset]
                if asset==claim["new_coin"]:
                    _locked_selected_budget(
                        cur,did=did,asset=asset,stable=scope["stable"],
                        expected_revision=claim["target_revisions_json"][asset],
                    )
        except (HTTPException,ValueError,KeyError,TypeError):
            verified=False
    if not verified and grant["status"]=="UPDATE_UNCERTAIN":
        # Already held after a committed remote attempt. No second PATCH and
        # no second transition needed; only exact readback can release it.
        return {"status":"UPDATE_UNCERTAIN","retry_forbidden":True,
                "reconciliation_required":True,"can_submit":False}
    if not verified:
        cur.execute(
            """UPDATE mininex_live_evm_wallet_policy_intents
               SET status='PATCH_UNCERTAIN'
               WHERE privy_did=%s AND nonce=%s AND status='PATCH_SUBMITTING'
               RETURNING status""",(did,claim["nonce"]),
        )
        x=cur.fetchone()
        if type(x) is not dict or x.get("status")!="PATCH_UNCERTAIN":
            raise HTTPException(409,"Cannot hold uncertain original policy update")
        cur.execute(
            """UPDATE mininex_live_evm_wallet_policy_grants
               SET status='UPDATE_UNCERTAIN',updated_at=now()
               WHERE privy_did=%s AND status='UPDATING'
               RETURNING status""",(did,),
        )
        y=cur.fetchone()
        if type(y) is not dict or y.get("status")!="UPDATE_UNCERTAIN":
            raise HTTPException(409,"Cannot hold uncertain original signer")
        return {"status":"UPDATE_UNCERTAIN","retry_forbidden":True,
                "reconciliation_required":True,"can_submit":False}
    cur.execute(
        """UPDATE mininex_live_evm_wallet_policy_grants
           SET status='GRANT_VERIFIED',policy_sha256=%s,
               scopes_json=%s,revisions_json=%s,revision=revision+1,
               remote_verified_at=now(),updated_at=now()
           WHERE privy_did=%s AND status IN ('UPDATING','UPDATE_UNCERTAIN')
             AND revision=%s AND policy_sha256=%s
           RETURNING status""",(
                claim["target_digest"],Jsonb(claim["target_scopes_json"]),
                Jsonb(claim["target_revisions_json"]),did,
                claim["old_revision"],claim["old_digest"],
        ),
    )
    x=cur.fetchone()
    if type(x) is not dict or x.get("status")!="GRANT_VERIFIED":
        raise HTTPException(409,"Remote wallet scope activation failed")
    cur.execute(
        """UPDATE mininex_live_evm_wallet_policy_intents
           SET status='PATCH_VERIFIED',consumed_at=now(),
               update_resolved_at=now()
           WHERE privy_did=%s AND nonce=%s
             AND status IN ('PATCH_SUBMITTING','PATCH_UNCERTAIN')
           RETURNING status""",(did,claim["nonce"]),
    )
    y=cur.fetchone()
    if type(y) is not dict or y.get("status")!="PATCH_VERIFIED":
        raise HTTPException(409,"Original wallet consent not consumed")
    return {
        "status":"GRANT_VERIFIED","one_original_signer":True,
        "new_coin":claim["new_coin"],
        "scope_coins":sorted(claim["target_scopes_json"]),
        "shared_signer_id":claim["signer_id"],
        "policy_id":claim["policy_id"],
        "remote_policy_verified":True,
        "additional_signer_created":False,
        "new_policy_created":False,
        "can_submit":False,"live_trade_started":False,
    }


async def apply_original_wallet_coin_extension(did,nonce):
    did=_safe_did(did)
    if type(nonce) is not str or not _NONCE.fullmatch(nonce):
        raise HTTPException(422,"Original owner-signed nonce required")
    try:
        coin=await asyncio.to_thread(_load_consented_coin,did,nonce)
    except psycopg.Error:
        raise HTTPException(503,"Original signed scope unavailable") from None
    original,scopes=await _verified_previous_grant(did,coin)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                claim=_claim_locked(
                    cur,did=did,nonce=nonce,observed=original,
                    old_scopes=scopes,now=int(time.time()),
                )
    except psycopg.Error:
        LOG.warning("Original user-owned policy PATCH claim unavailable")
        raise HTTPException(503,"Original policy update claim unavailable") from None
    remote=None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9.0),follow_redirects=False,
        ) as client:
            remote=await _patch_once(claim=claim,client=client)
    except (httpx.HTTPError,ValueError,TypeError,KeyError,HTTPException):
        LOG.warning("Original Privy policy PATCH uncertain: never retry")
    try:
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                result=_finish_locked(cur,did=did,claim=claim,remote=remote)
    except psycopg.Error:
        LOG.warning("Original Privy PATCH remote outcome requires reconciliation")
        raise HTTPException(503,"Original policy outcome requires audit") from None
    if result["status"]!="GRANT_VERIFIED":
        raise HTTPException(503,"Privy scope update uncertain; do not retry")
    return result



def _load_reconcilable_claim(did,nonce):
    """Reconstruct remote GET target from a previously claimed update only."""
    url=_db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT g.wallet_id,g.wallet_address,g.owner_id,g.signer_id,
                          g.policy_id,g.policy_sha256,g.scopes_json AS original_scopes_json,
                          g.revisions_json AS original_revisions_json,
                          g.revision AS original_revision,
                          g.status AS grant_status,
                          i.status AS intent_status,i.source_grant_revision,
                          i.previous_policy_sha256,i.target_policy_sha256,
                          i.target_scopes_json,i.target_revisions_json,
                          i.update_idempotency_key
                   FROM mininex_live_evm_wallet_policy_grants g
                   JOIN mininex_live_evm_wallet_policy_intents i
                     ON g.privy_did=i.privy_did
                   WHERE g.privy_did=%s AND i.nonce=%s""",(did,nonce),
            )
            saved=cur.fetchone()
    if (type(saved) is not dict
            or saved.get("grant_status") not in ("UPDATING","UPDATE_UNCERTAIN")
            or saved.get("intent_status") not in ("PATCH_SUBMITTING","PATCH_UNCERTAIN")
            or saved.get("source_grant_revision")!=saved.get("original_revision")
            or saved.get("previous_policy_sha256")!=saved.get("policy_sha256")
            or type(saved.get("original_scopes_json")) is not dict
            or type(saved.get("target_scopes_json")) is not dict
            or type(saved.get("target_revisions_json")) is not dict
            or not saved["target_scopes_json"]
            or set(saved["target_scopes_json"])!=set(saved["target_revisions_json"])
            or type(saved.get("update_idempotency_key")) is not str
            or not re.fullmatch(r"[0-9a-f]{64}",saved["update_idempotency_key"])):
        raise HTTPException(409,"No previously claimed original wallet policy update")
    args=dict(
        wallet=saved["wallet_address"],owner_id=saved["owner_id"],
        executor_id=saved["signer_id"],
        scopes=list(saved["target_scopes_json"].values()),now=int(time.time()),
    )
    try:
        policy=compose_scoped_evm_wallet_policy(**args)
        digest=verified_wallet_policy_digest(policy,**args)
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"Previously consented policy scope expired") from None
    if digest!=saved["target_policy_sha256"]:
        raise HTTPException(409,"Saved remote update digest mismatch")
    source=saved["original_scopes_json"]
    target=saved["target_scopes_json"]
    try:
        changed=changed_wallet_coin(source,target,saved["original_revisions_json"],saved["target_revisions_json"])
    except (ValueError,KeyError) as exc:
        raise HTTPException(409,"Original user-approved coin scope was replaced") from None
    return {
        "did":did,"nonce":nonce,"policy_id":saved["policy_id"],
        "wallet_id":saved["wallet_id"],
        "wallet_address":saved["wallet_address"],
        "signer_id":saved["signer_id"],
        "owner_id":saved["owner_id"],
        "old_digest":saved["policy_sha256"],
        "old_revision":saved["original_revision"],
        "target_digest":digest,
        "target_scopes_json":saved["target_scopes_json"],
        "target_revisions_json":saved["target_revisions_json"],
        "idempotency_key":saved["update_idempotency_key"],
        "candidate":policy,
        "new_coin":changed,
    }


async def _observe_remote_patch_without_sending(claim):
    """One GET-only original policy+wallet+owner quorum reconciliation."""
    remote=await _fetch_policy(claim["policy_id"])
    if verify_remote_combined_policy(
        remote,expected=claim["candidate"],
    )!=claim["policy_id"]:
        raise ValueError("Privy has not confirmed the exact target policy")
    user=await fetch_privy_user(claim["did"])
    wallet=await fetch_privy_wallet_metadata(claim["wallet_id"])
    owned=_owned_eth_wallet(user,claim["did"])
    if (owned["delegated"] is not True
            or owned["address"]!=claim["wallet_address"]
            or owned["wallet_id"]!=claim["wallet_id"]
            or type(wallet.get("additional_signers")) is not list
            or len(wallet["additional_signers"])!=1
            or not _scoped_signer(
                wallet,did=claim["did"],address=owned["address"],
                config={
                    "signer_id":claim["signer_id"],
                    "policy_id":claim["policy_id"],
                })):
        raise ValueError("Original wallet signer changed during uncertain PATCH")
    for quorum_id in (claim["owner_id"],claim["signer_id"]):
        evidence=await check_privy_backend_quorum(
            expected_quorum_id=quorum_id,
        )
        if evidence.get("remote_quorum_public_key_verified") is not True:
            raise ValueError("Registered original signer/owner quorum changed")
    return {
        "policy_id":claim["policy_id"],
        "digest":claim["target_digest"],
        "one_signer_verified":True,
        "remote_rules_verified":True,
    }


async def reconcile_original_wallet_extension(did,nonce):
    """Independent remote readback only; no second Privy PATCH or signer."""
    did=_safe_did(did)
    if type(nonce) is not str or not _NONCE.fullmatch(nonce):
        raise HTTPException(422,"Previous wallet update nonce required")
    try:
        claim=await asyncio.to_thread(_load_reconcilable_claim,did,nonce)
    except psycopg.Error:
        raise HTTPException(503,"Original wallet update journal unavailable") from None
    proof=None
    try:
        proof=await _observe_remote_patch_without_sending(claim)
    except (ValueError,TypeError,KeyError,HTTPException,httpx.HTTPError):
        LOG.warning("Original Privy wallet policy not yet independently reconcilable")
    try:
        url=_db_url()
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                result=_finish_locked(cur,did=did,claim=claim,remote=proof)
    except psycopg.Error:
        raise HTTPException(503,"Cannot finalize original wallet remote readback") from None
    if result["status"]!="GRANT_VERIFIED":
        raise HTTPException(503,"Uncertain Privy policy update remains on hold")
    return result
