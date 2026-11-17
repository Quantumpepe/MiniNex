"""One explicit user-signed *intent* for MiniNex automatic net-profit fees.

The user reviews the exact 5% net/HWM purpose, coin, network, ERC20 token,
Treasury, single transfer maximum and expiry. EIP-191 signature proves intent,
NOT Privy wallet delegation. A separate user-approved addSigners action,
independent remote-policy/quorum verification and durable payment claim
are still required before any server-signed fee transfer.

Nothing here signs on behalf of users, posts a Privy policy or moves funds.
"""
from __future__ import annotations

from datetime import datetime,timezone
import hashlib
import json
import logging
import secrets
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_user_consent import (
    _require_identity,verify_eoa_eip191_signature,
)
from .privy_wallet_verification import APP_ID,fetch_privy_user
from .privy_live_signer import _owned_eth_wallet
from .shared_evm_owner_consent import _asset,_locked_coin_settings
from .shared_evm_fee_privy_policy_draft import build_owner_evm_fee_policy_draft
from .shared_evm_fee_policy_roles import require_fee_policy_roles as _require_policy_roles
from .shared_evm_fee_transfer_proof import owner_fee_treasury
from .shared_evm_swap_receipts import _pair
from .storage import _db_url,_ensure_schema,_prepare_rows

LOG=logging.getLogger("mininex.evm_fee_owner_consent")
_TTL=300


def fee_owner_intent_text(
    *,did:str,session_sha256:str,asset:str,stable:str,
    wallet:str,wallet_id:str,treasury:str,chain_id:int,
    max_atomic:str,owner_id:str,signer_id:str,
    policy_sha256:str,issued:int,challenge_expires:int,
    policy_expires:int,nonce:str,
)->str:
    """Human-readable and exact signed fee scope; no implied wallet grant."""
    return "\n".join((
        "MiniNex - optional automated 5% NET PROFIT fee enrollment",
        "I approve the following FEE POLICY request ONLY.",
        "Performance fee: 5% of independently VERIFIED REALIZED NET USD gains",
        "Only new gains above my per-coin High-Water-Mark are fee eligible.",
        "No fee is due on losses or unverified historical costs.",
        "The server cannot use this signature to delegate my wallet.",
        "A separate explicit Privy wallet addSigners approval is required.",
        "Only one original verified obligation can be paid once.",
        "I can revoke the MiniNex fee enrollment and remote Privy grant.",
        f"Application: {APP_ID}",
        f"Privy user: {did}",
        f"Session SHA256: {session_sha256}",
        f"Coin: {asset}",
        f"Settlement stablecoin: {stable}",
        f"Chain ID: {chain_id}",
        f"Owner wallet: {wallet}",
        f"Wallet ID: {wallet_id}",
        f"Exact treasury recipient: {treasury}",
        f"Maximum fee per transfer (atomic token units): {max_atomic}",
        f"Proposed Privy policy SHA256: {policy_sha256}",
        f"Policy owner ID: {owner_id}",
        f"Server executor ID: {signer_id}",
        f"Policy expires Unix: {policy_expires}",
        f"Issued Unix: {issued}",
        f"Enrollment challenge expires Unix: {challenge_expires}",
        f"Nonce: {nonce}",
        "No permission for arbitrary transfers, approvals or DEX swaps.",
    ))


def _begin_fee_intent_locked(
    cur,*,did:str,session_sha256:str,asset:str,
    owned:dict,treasury:str,owner_id:str,signer_id:str,
    max_atomic:str,expires_at:int,now:int,nonce:str,
)->dict:
    settings=_locked_coin_settings(cur,did=did,asset=asset)
    stable=settings["stable"]
    route=_pair(asset,stable,"BUY")
    candidate=build_owner_evm_fee_policy_draft(
        asset=asset,stable=stable,wallet=owned["address"],
        treasury=treasury,owner_id=owner_id,executor_id=signer_id,
        owner_max_fee_atomic=max_atomic,expires_at=expires_at,now=now)
    digest=hashlib.sha256(json.dumps(
        candidate,sort_keys=True,separators=(",",":")
    ).encode("utf-8")).hexdigest()
    message=fee_owner_intent_text(
        did=did,session_sha256=session_sha256,asset=asset,
        stable=stable,wallet=owned["address"],wallet_id=owned["wallet_id"],
        treasury=treasury,chain_id=route["chain_id"],
        max_atomic=max_atomic,owner_id=owner_id,signer_id=signer_id,
        policy_sha256=digest,issued=now,challenge_expires=now+_TTL,
        policy_expires=expires_at,nonce=nonce)
    message_hash=hashlib.sha256(message.encode("utf-8")).hexdigest()
    cur.execute(
        """INSERT INTO mininex_evm_fee_auto_permissions
           (privy_did,asset,stable,chain_id,wallet_id,wallet_address,
            treasury_wallet,owner_id,signer_id,owner_max_fee_atomic,
            expires_at,challenge_text,challenge_sha256,
            challenge_expires_at,policy_json,policy_sha256,status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   to_timestamp(%s),%s,%s,to_timestamp(%s),%s,%s,
                   'PENDING_INTENT')
           ON CONFLICT(privy_did,asset) DO NOTHING RETURNING status""",
        (did,asset,stable,route["chain_id"],owned["wallet_id"],
         owned["address"],treasury,owner_id,signer_id,max_atomic,
         expires_at,message,message_hash,now+_TTL,
         json.dumps(candidate,separators=(",",":")),digest))
    written=cur.fetchone()
    if type(written) is not dict or written.get("status")!="PENDING_INTENT":
        raise HTTPException(409,"Existing fee grant requires explicit reconciliation")
    return {
        "mode":"EVM_OWNER_FEE_ENROLLMENT_INTENT_ONLY",
        "asset":asset,"chain_id":route["chain_id"],
        "wallet_address":owned["address"],"stable":stable,
        "treasury_wallet":treasury,"owner_max_fee_atomic":max_atomic,
        "policy_sha256":digest,
        "message_to_sign":message,"expires_at_unix":now+_TTL,
        "owner_wallet_eip191_signature_required":True,
        "privy_policy_published":False,"privy_grant_attached":False,
        "fee_auto_transfer_enabled":False,
    }


def _confirm_fee_intent_locked(
    cur,*,did:str,asset:str,session_sha256:str,
    owned:dict,signature:str,now:int,
)->dict:
    cur.execute(
        """SELECT asset,stable,chain_id,wallet_id,wallet_address,
                  treasury_wallet,owner_id,signer_id,owner_max_fee_atomic,
                  expires_at,challenge_text,challenge_sha256,
                  challenge_expires_at,policy_json,policy_sha256,status
           FROM mininex_evm_fee_auto_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    row=cur.fetchone()
    if (type(row) is not dict or row.get("status")!="PENDING_INTENT"
            or row.get("wallet_id")!=owned["wallet_id"]
            or row.get("wallet_address")!=owned["address"]
            or not isinstance(row.get("expires_at"),datetime)
            or not isinstance(row.get("challenge_expires_at"),datetime)
            or row["challenge_expires_at"].tzinfo is None
            or int(row["challenge_expires_at"].timestamp())<=now
            or int(row["expires_at"].timestamp())<=now
            or type(row.get("challenge_text")) is not str
            or type(row.get("policy_sha256")) is not str):
        raise HTTPException(409,"Original fee-owner challenge absent or expired")
    settings=_locked_coin_settings(cur,did=did,asset=asset)
    route=_pair(asset,settings["stable"],"BUY")
    if (row.get("stable")!=settings["stable"]
            or row.get("chain_id")!=route["chain_id"]
            or row.get("treasury_wallet")!=owner_fee_treasury(
                asset=asset,stable=settings["stable"])
            or hashlib.sha256(row["challenge_text"].encode()).hexdigest()
                !=row["challenge_sha256"]
            or f"Session SHA256: {session_sha256}" not in row["challenge_text"]):
        raise HTTPException(409,"Wallet fee enrollment or Treasury changed")
    now_expiry=int(row["expires_at"].timestamp())
    candidate=build_owner_evm_fee_policy_draft(
        asset=asset,stable=row["stable"],wallet=row["wallet_address"],
        treasury=row["treasury_wallet"],owner_id=row["owner_id"],
        executor_id=row["signer_id"],
        owner_max_fee_atomic=str(row["owner_max_fee_atomic"]),
        expires_at=now_expiry,now=now)
    digest=hashlib.sha256(json.dumps(
        candidate,sort_keys=True,separators=(",",":")
    ).encode("utf-8")).hexdigest()
    if row["policy_json"]!=candidate or digest!=row["policy_sha256"]:
        raise HTTPException(409,"Privy fee policy no longer matches signed scope")
    if not verify_eoa_eip191_signature(
        message=row["challenge_text"],signature=signature,
        wallet=row["wallet_address"]):
        raise HTTPException(403,"Wallet signature does not authorize fee consent")
    cur.execute(
        """UPDATE mininex_evm_fee_auto_permissions
           SET status='CONSENT_SIGNED',owner_signed_at=now(),updated_at=now()
           WHERE privy_did=%s AND asset=%s AND status='PENDING_INTENT'
           RETURNING status""",(did,asset))
    written=cur.fetchone()
    if type(written) is not dict or written.get("status")!="CONSENT_SIGNED":
        raise HTTPException(409,"Concurrent fee-owner enrollment changed")
    return {
        "state":"CONSENT_SIGNED",
        "owner_fee_intent_verified":True,
        "policy_sha256":digest,"policy_published":False,
        "privy_owner_grant_still_required":True,
        "automatic_fee_transfer_enabled":False,
    }


async def begin_owner_fee_auto_consent(
    *,did:str,session_id:str,asset:str,
    max_fee_atomic:str,expires_at:int,
)->dict:
    did,digest=_require_identity(did,session_id)
    asset=_asset(asset)
    user=await fetch_privy_user(did)
    owned=_owned_eth_wallet(user,did)
    owner,signer=_require_policy_roles(asset)
    db=_db_url();_ensure_schema(db)
    stable=None
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _prepare_rows(cur,did)
            stable=_locked_coin_settings(cur,did=did,asset=asset)["stable"]
    treasury=owner_fee_treasury(asset=asset,stable=stable)
    now=int(time.time())
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _begin_fee_intent_locked(
                cur,did=did,session_sha256=digest,asset=asset,
                owned=owned,treasury=treasury,owner_id=owner,
                signer_id=signer,max_atomic=max_fee_atomic,
                expires_at=expires_at,now=now,nonce=secrets.token_hex(32))


async def confirm_owner_fee_auto_consent(
    *,did:str,session_id:str,asset:str,signature:str,
)->dict:
    did,digest=_require_identity(did,session_id)
    asset=_asset(asset)
    owned=_owned_eth_wallet(await fetch_privy_user(did),did)
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _confirm_fee_intent_locked(
                cur,did=did,asset=asset,session_sha256=digest,
                owned=owned,signature=signature,now=int(time.time()))
