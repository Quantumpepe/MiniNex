"""Per-coin EIP-191 owner-signed LIVE enrollment intent for MiniNex EVM.

This is never an authorization signature for a trade. It does not delegate a
Privy wallet, grant an additional signer, publish a policy, change a budget,
create an order, or send money. The wallet owner must sign an exact
challenge. Independent Privy policy and signer approval follow separately.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_user_consent import (
    _require_identity, _require_policy_roles, verify_eoa_eip191_signature,
)
from .privy_live_signer import _owned_eth_wallet
from .privy_wallet_verification import APP_ID, fetch_privy_user
from .shared_evm_swap_receipts import _pair
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG=logging.getLogger("mininex.evm_signed_intent")
_ADDITIONAL=frozenset(("BNB","POL","AVAX","LINK","UNI"))
_TTL=300


def _asset(asset:object)->str:
    if type(asset) is not str or asset not in _ADDITIONAL:
        raise HTTPException(404,"Unsupported EVM consent asset")
    return asset


def _owned_wallet_from_privy(user:object,did:str)->dict:
    try:
        owned=_owned_eth_wallet(user,did)
        if not owned.get("delegated"):
            # No existing delegation is required to SIGN an intent.
            # Privy wallet ownership, not the signer grant, is needed here.
            pass
        return owned
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"An authenticated Privy EVM wallet is required") from None


def _canonical_message(*,did:str,asset:str,stable:str,
                       chain_id:int,wallet:str,wallet_id:str,
                       budget_usd:int,budget_revision:int,
                       session_sha256:str,nonce:str,issued:int,
                       expires:int,owner:str,executor:str)->str:
    pair=_pair(asset,stable,"BUY")
    if pair["chain_id"]!=chain_id:
        raise ValueError("Mismatched chain for owner consent")
    return "\n".join((
        f"MiniNex {asset} - request for future LIVE trading",
        "This personal signature records my enrollment intent ONLY.",
        "It does NOT delegate my Privy wallet or authorize a transfer.",
        "A separate Privy wallet signer approval is required.",
        "The selected budget is my own setting, not a MiniNex fixed cap.",
        "I may revoke my MiniNex enrollment intent.",
        f"Application: {APP_ID}",
        f"Privy User ID: {did}",
        f"Asset: {asset}",
        f"Chain ID: {chain_id}",
        f"Settlement: {stable}",
        f"Owner wallet: {wallet}",
        f"Wallet ID: {wallet_id}",
        f"Coin budget USD: {budget_usd}",
        f"Budget revision: {budget_revision}",
        f"Uniswap SwapRouter02: {pair['router']}",
        f"Session SHA256: {session_sha256}",
        f"Nonce: {nonce}",
        f"Issued Unix: {issued}",
        f"Expires Unix: {expires}",
        f"Policy owner ID: {owner}",
        f"Execution signer ID: {executor}",
        "No approval for arbitrary contracts, seeds, or wallet transfers.",
    ))


def _locked_coin_settings(cur,*,did:str,asset:str)->dict:
    cur.execute(
        """SELECT budget_usd,stable,revision
           FROM mininex_extra_coin_preferences
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    row=cur.fetchone()
    if (type(row) is not dict or type(row.get("budget_usd")) is not int
            or row["budget_usd"]<=0
            or type(row.get("revision")) is not int
            or row["revision"]<1 or type(row.get("stable")) is not str):
        raise HTTPException(409,"Save your own coin budget before enrollment")
    _pair(asset,row["stable"],"BUY")
    return row


def _begin_locked(cur,*,did:str,asset:str,session_sha256:str,
                  owned:dict,owner:str,executor:str,now:int,nonce:str)->dict:
    settings=_locked_coin_settings(cur,did=did,asset=asset)
    pair=_pair(asset,settings["stable"],"BUY")
    cur.execute(
        """SELECT status FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    active=cur.fetchone()
    if active is not None:
        # Never overwrite a prepared, previously approved or revoked policy.
        raise HTTPException(409,"Existing coin policy requires reconciliation")
    cur.execute(
        """SELECT status FROM mininex_live_evm_consent_challenges
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    prev=cur.fetchone()
    if prev is not None and prev.get("status")!="PENDING":
        raise HTTPException(409,"Existing signed or revoked coin intent")
    expires=now+_TTL
    message=_canonical_message(
        did=did,asset=asset,stable=settings["stable"],
        chain_id=pair["chain_id"],wallet=owned["address"],
        wallet_id=owned["wallet_id"],
        budget_usd=settings["budget_usd"],
        budget_revision=settings["revision"],
        session_sha256=session_sha256,nonce=nonce,issued=now,
        expires=expires,owner=owner,executor=executor)
    digest=hashlib.sha256(message.encode("utf-8")).hexdigest()
    cur.execute(
        """INSERT INTO mininex_live_evm_consent_challenges
           (privy_did,asset,session_sha256,wallet_id,wallet_address,
            chain_id,stable,budget_revision,budget_usd,
            permission_revision,message_text,message_sha256,
            issued_at,expires_at,status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,
                   to_timestamp(%s),to_timestamp(%s),'PENDING')
           ON CONFLICT (privy_did,asset) DO UPDATE SET
             session_sha256=EXCLUDED.session_sha256,
             wallet_id=EXCLUDED.wallet_id,
             wallet_address=EXCLUDED.wallet_address,
             chain_id=EXCLUDED.chain_id,
             stable=EXCLUDED.stable,
             budget_revision=EXCLUDED.budget_revision,
             budget_usd=EXCLUDED.budget_usd,
             message_text=EXCLUDED.message_text,
             message_sha256=EXCLUDED.message_sha256,
             issued_at=EXCLUDED.issued_at,expires_at=EXCLUDED.expires_at,
             status='PENDING',confirmed_at=NULL,revoked_at=NULL
           WHERE mininex_live_evm_consent_challenges.status='PENDING'
           RETURNING status""",
        (did,asset,session_sha256,owned["wallet_id"],
         owned["address"],pair["chain_id"],settings["stable"],
         settings["revision"],settings["budget_usd"],
         message,digest,now,expires))
    saved=cur.fetchone()
    if type(saved) is not dict or saved.get("status")!="PENDING":
        raise HTTPException(409,"Cannot replace signed EVM consent")
    return {
        "mode":"EVM_OWNER_SIGNED_INTENT_ONLY",
        "asset":asset,"chain_id":pair["chain_id"],
        "wallet_address":owned["address"],
        "challenge":message,"expires_at_unix":expires,
        "budget_revision":settings["revision"],
        "user_wallet_signature_required":True,
        "state":"PENDING","privy_signer_granted":False,
        "live_trading_enabled":False,
    }


def _confirm_locked(cur,*,did:str,asset:str,session_sha256:str,
                    owned:dict,signature:str,now:int)->dict:
    settings=_locked_coin_settings(cur,did=did,asset=asset)
    pair=_pair(asset,settings["stable"],"BUY")
    cur.execute(
        """SELECT status,session_sha256,wallet_id,wallet_address,chain_id,
                  stable,budget_revision,budget_usd,
                  message_text,message_sha256,
                  extract(epoch FROM expires_at)::bigint AS expires_at_unix
           FROM mininex_live_evm_consent_challenges
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    record=cur.fetchone()
    if (type(record) is not dict
            or record.get("status")!="PENDING"
            or record.get("session_sha256")!=session_sha256
            or record.get("wallet_id")!=owned["wallet_id"]
            or record.get("wallet_address")!=owned["address"]
            or record.get("chain_id")!=pair["chain_id"]
            or record.get("stable")!=settings["stable"]
            or record.get("budget_revision")!=settings["revision"]
            or record.get("budget_usd")!=settings["budget_usd"]
            or type(record.get("message_text")) is not str
            or type(record.get("expires_at_unix")) is not int
            or now>=record["expires_at_unix"]
            or record.get("message_sha256")!=hashlib.sha256(
                record["message_text"].encode("utf-8")).hexdigest()):
        raise HTTPException(409,"Coin consent changed, expired, or already signed")
    if not verify_eoa_eip191_signature(
        message=record["message_text"],signature=signature,
        wallet=owned["address"]):
        raise HTTPException(403,"Wallet signature does not match EVM owner")
    cur.execute(
        """UPDATE mininex_live_evm_consent_challenges
           SET status='SIGNED_INTENT',confirmed_at=now()
           WHERE privy_did=%s AND asset=%s AND status='PENDING'
           RETURNING status""",(did,asset))
    saved=cur.fetchone()
    if type(saved) is not dict or saved.get("status")!="SIGNED_INTENT":
        raise HTTPException(409,"Concurrent coin consent conflict")
    return {
        "mode":"EVM_OWNER_SIGNED_INTENT_ONLY",
        "asset":asset,"chain_id":pair["chain_id"],
        "state":"SIGNED_INTENT","owner_signature_verified":True,
        "privy_wallet_delegated_by_this_action":False,
        "additional_privy_grant_still_required":True,
        "live_trading_enabled":False,"can_submit":False,
    }


async def prepare_evm_consent_challenge(
    did:str,session_id:str,asset:str,
)->dict:
    did,session_sha256=_require_identity(did,session_id)
    asset=_asset(asset)
    owner,executor=_require_policy_roles()
    owned=_owned_wallet_from_privy(await fetch_privy_user(did),did)
    now=int(time.time())
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                return _begin_locked(cur,did=did,asset=asset,
                    session_sha256=session_sha256,owned=owned,
                    owner=owner,executor=executor,
                    now=now,nonce=secrets.token_hex(32))
    except psycopg.Error:
        LOG.warning("EVM signed intent challenge storage unavailable")
        raise HTTPException(503,"Coin enrollment unavailable") from None


async def confirm_evm_consent_intent(
    did:str,session_id:str,asset:str,signature:str,
)->dict:
    did,session_sha256=_require_identity(did,session_id)
    asset=_asset(asset)
    owned=_owned_wallet_from_privy(await fetch_privy_user(did),did)
    now=int(time.time())
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _confirm_locked(cur,did=did,asset=asset,
                    session_sha256=session_sha256,owned=owned,
                    signature=signature,now=now)
    except psycopg.Error:
        LOG.warning("EVM owner consent signature storage unavailable")
        raise HTTPException(503,"Cannot confirm signed coin intent") from None


def revoke_evm_consent(did:str,asset:str)->dict:
    did=_safe_did(did)
    asset=_asset(asset)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                # Do not erase or replay any previously signed challenge.
                cur.execute(
                    """UPDATE mininex_live_evm_consent_challenges
                       SET status='REVOKED',revoked_at=now()
                       WHERE privy_did=%s AND asset=%s
                         AND status<>'REVOKED'""",(did,asset))
                cur.execute(
                    """UPDATE mininex_live_evm_owner_permissions
                       SET status='REVOKED',revoked_at=now(),
                           revision=revision+1,updated_at=now()
                       WHERE privy_did=%s AND asset=%s
                         AND status<>'REVOKED'""",(did,asset))
        return {
            "mode":"EVM_OWNER_LOCAL_CONSENT_REVOCATION",
            "asset":asset,"state":"REVOKED_LOCALLY",
            "future_mininex_auto_orders_blocked":True,
            "privy_remote_signer_removed":False,
            "privy_remote_revocation_required_if_delegated":True,
            "already_submitted_orders_unaffected":True,
        }
    except psycopg.Error:
        raise HTTPException(503,"Cannot revoke coin consent locally") from None
