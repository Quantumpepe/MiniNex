"""Independent owner-confirmed Privy signer grant for five EVM assets.

An owner must separately attach the scoped additional signer in Privy.
Only a fresh server-side read of the owner wallet, complete remote policy,
exact signer scope and backend quorum can promote a published *unattached*
per-asset policy to GRANT_VERIFIED. This never invokes addSigners, signs a
transaction or starts a trading session.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timezone
import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .storage import _db_url,_ensure_schema,_safe_did
from .shared_evm_owner_consent import _asset
from .shared_evm_remote_grant_audit import validate_pinned_evm_policy,_grant_scope_digest
from .shared_evm_privy_grant_review import verify_exact_remote_evm_policy
from .privy_live_signer import (
    _owned_eth_wallet,_scoped_signer,fetch_privy_wallet_metadata,
)
from .privy_wallet_verification import fetch_privy_user
from .eth_privy_user_grant import _fetch_policy
from .shared_evm_trade_policy_roles import check_coin_trade_backend_quorum as check_privy_backend_quorum

LOG=logging.getLogger("mininex.evm_grant_confirm")


def _load_owner_grant_candidate_locked(cur,*,did:str,asset:str,now:int)->dict:
    """Read the immutable approved per-coin snapshot and signed consent."""
    cur.execute(
        """SELECT asset,chain_id,stable,wallet_id,wallet_address,
                  owner_id,signer_id,policy_id,policy_sha256,
                  remote_policy_sha256,consent_sha256,
                  consent_verified_at,revoked_at,expires_at,status,
                  budget_revision,candidate_json,authorization_method
           FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    record=cur.fetchone()
    single=(type(record) is dict and
            record.get("authorization_method")=="PRIVY_SCOPED_GRANT")
    if (type(record) is not dict
            or not (
                single and record.get("status")=="DRAFT"
                and record.get("consent_verified_at") is None
                and record.get("consent_sha256") is None
                or not single and record.get("status")=="CONSENT_VERIFIED"
                and record.get("consent_verified_at") is not None
                and bool(record.get("consent_sha256")))
            or record.get("revoked_at") is not None
            or type(record.get("policy_id")) is not str
            or record.get("remote_policy_sha256")!=record.get("policy_sha256")):
        raise HTTPException(409,"An unattached owner-approved Privy policy is required")
    try:
        candidate=validate_pinned_evm_policy(record,asset=asset,now=now)
    except (TypeError,KeyError,ValueError):
        raise HTTPException(409,"Owner's pinned Privy policy is expired or changed") from None
    cur.execute(
        """SELECT status,policy_id,policy_sha256
           FROM mininex_live_evm_policy_publications
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    publication=cur.fetchone()
    if (type(publication) is not dict
            or publication.get("status")!="REMOTE_VERIFIED_UNATTACHED"
            or publication.get("policy_id")!=record["policy_id"]
            or publication.get("policy_sha256")!=record["policy_sha256"]):
        raise HTTPException(409,"Unverified or uncertain Privy policy publication")
    if not single:
        cur.execute(
            """SELECT status,wallet_id,wallet_address,chain_id,stable,
                      budget_revision,message_sha256,confirmed_at
               FROM mininex_live_evm_consent_challenges
               WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
        consent=cur.fetchone()
        if (type(consent) is not dict
                or consent.get("status")!="SIGNED_INTENT"
                or consent.get("confirmed_at") is None
                or consent.get("wallet_id")!=record["wallet_id"]
                or consent.get("wallet_address")!=record["wallet_address"]
                or consent.get("chain_id")!=record["chain_id"]
                or consent.get("stable")!=record["stable"]
                or consent.get("budget_revision")!=record["budget_revision"]
                or consent.get("message_sha256")!=record["consent_sha256"]):
            raise HTTPException(409,"Current wallet-signed per-coin consent required")
    cur.execute(
        """SELECT stable,revision,budget_usd
           FROM mininex_extra_coin_preferences
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    settings=cur.fetchone()
    if (type(settings) is not dict
            or settings.get("stable")!=record["stable"]
            or settings.get("revision")!=record["budget_revision"]):
        raise HTTPException(409,"Coin budget changed since the signed approval")
    if single:
        if type(settings.get("budget_usd")) is not int or settings["budget_usd"]<=0:
            raise HTTPException(409,"Original per-coin budget unavailable")
        record["approved_budget_usd"]=settings["budget_usd"]
    return record


def _prove_remote_owner_grant(
    *,did:str,record:dict,expected:dict,user:object,
    wallet_metadata:object,remote_policy:object,quorum:object,
)->dict:
    """Only independently fetched Privy facts, never a frontend statement."""
    try:
        owned=_owned_eth_wallet(user,did)
        if (owned.get("delegated") is not True
                or owned.get("wallet_id")!=record["wallet_id"]
                or owned.get("address")!=record["wallet_address"]
                or type(wallet_metadata) is not dict
                or wallet_metadata.get("id")!=owned["wallet_id"]):
            raise ValueError("Owner's delegated wallet differs from signed approval")
        checked=verify_exact_remote_evm_policy(remote_policy,expected)
        if (checked["policy_id"]!=record["policy_id"]
                or checked["policy_sha256"]!=record["policy_sha256"]):
            raise ValueError("Privy policy changed since owner's approval")
        if not _scoped_signer(
            wallet_metadata,did=did,address=owned["address"],
            config={
                "signer_id":record["signer_id"],
                "policy_id":record["policy_id"],
            }):
            raise ValueError("Owner has not granted the exact restricted signer")
        if (type(quorum) is not dict
                or quorum.get("remote_quorum_public_key_verified") is not True):
            raise ValueError("Registered Privy backend signer quorum missing")
    except (ValueError,TypeError,KeyError,AttributeError):
        raise HTTPException(409,"Restricted owner Privy signer grant is not verified") from None
    return {
        "asset":record["asset"],
        "wallet_id":record["wallet_id"],
        "wallet_address":record["wallet_address"],
        "policy_id":record["policy_id"],
        "policy_sha256":record["policy_sha256"],
        "budget_revision":record["budget_revision"],
        "verified":True,
    }


def _commit_owner_grant_locked(
    cur,*,did:str,asset:str,proof:dict,now:int,
)->dict:
    """Recheck consent/policy under locks after Privy network requests."""
    current=_load_owner_grant_candidate_locked(
        cur,did=did,asset=asset,now=now)
    if (type(proof) is not dict or proof.get("verified") is not True
            or proof.get("asset")!=asset
            or any(proof.get(field)!=current.get(field) for field in (
                "wallet_id","wallet_address","policy_id",
                "policy_sha256","budget_revision"))):
        raise HTTPException(409,"Owner Privy consent or grant changed during audit")
    single=current.get("authorization_method")=="PRIVY_SCOPED_GRANT"
    # A SHA is an audit commitment, never consent by itself. Permission
    # is promoted only AFTER fresh remote owner+policy+signer verification.
    digest=_grant_scope_digest(
        did=did,record=current,budget_usd=current["approved_budget_usd"],
    ) if single else None
    expected="DRAFT" if single else "CONSENT_VERIFIED"
    cur.execute(
        """UPDATE mininex_live_evm_owner_permissions
           SET status='GRANT_VERIFIED',grant_verified_at=now(),
               consent_sha256=CASE WHEN
                 authorization_method='PRIVY_SCOPED_GRANT'
                 THEN %s ELSE consent_sha256 END,
               consent_verified_at=CASE WHEN
                 authorization_method='PRIVY_SCOPED_GRANT'
                 THEN now() ELSE consent_verified_at END,
               updated_at=now(),revision=revision+1
           WHERE privy_did=%s AND asset=%s AND status=%s
             AND revoked_at IS NULL AND policy_id=%s
             AND policy_sha256=%s
           RETURNING status""",
        (digest,did,asset,expected,
         proof["policy_id"],proof["policy_sha256"]))
    updated=cur.fetchone()
    if type(updated) is not dict or updated.get("status")!="GRANT_VERIFIED":
        raise HTTPException(409,"Concurrent EVM wallet grant confirmation")
    return {
        "mode":"OWNER_EVM_PRIVY_SIGNER_GRANT_VERIFIED",
        "asset":asset,
        "state":"GRANT_VERIFIED",
        "remote_policy_rules_verified":True,
        "owner_signer_grant_verified":True,
        "user_budget_revision":proof["budget_revision"],
        "trading_session_started":False,
        "transaction_signed_or_sent":False,
    }


def _read_candidate(did:str,asset:str,now:int)->dict:
    url=_db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _load_owner_grant_candidate_locked(
                cur,did=did,asset=asset,now=now)


def _commit(did:str,asset:str,proof:dict,now:int)->dict:
    url=_db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _commit_owner_grant_locked(
                cur,did=did,asset=asset,proof=proof,now=now)


async def confirm_owner_evm_privy_grant(did:str,asset:str)->dict:
    """Owner-requested post-delegation readback; no wallet permissions created."""
    did=_safe_did(did)
    asset=_asset(asset)
    from time import time
    now=int(time())
    try:
        record=await asyncio.to_thread(_read_candidate,did,asset,now)
        candidate=validate_pinned_evm_policy(
            record,asset=asset,now=now)
    except psycopg.Error:
        raise HTTPException(503,"EVM grant ledger unavailable") from None
    user=await fetch_privy_user(did)
    metadata=await fetch_privy_wallet_metadata(record["wallet_id"])
    policy=await _fetch_policy(record["policy_id"])
    quorum=await check_privy_backend_quorum(asset)
    proof=_prove_remote_owner_grant(
        did=did,record=record,expected=candidate,user=user,
        wallet_metadata=metadata,remote_policy=policy,quorum=quorum)
    try:
        return await asyncio.to_thread(
            _commit,did,asset,proof,int(time()))
    except psycopg.Error:
        raise HTTPException(503,"Cannot commit verified EVM wallet grant") from None
