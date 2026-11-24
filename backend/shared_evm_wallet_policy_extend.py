"""Append ONE user-approved EVM coin to an existing wallet-wide Privy policy.

Prepare and confirm only create an auditable, one-use original-wallet
EIP-191 scope intent. No remote PATCH, addSigners, live order or transfer
is performed. A separate owner-signed remote update must be implemented
before any new permissions are effective.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .eth_user_consent import _require_policy_roles
from .eth_privy_remote_quorum import check_privy_backend_quorum
from .eth_privy_user_grant import _fetch_policy
from .extra_coin_preferences import get_coin_preferences
from .trade_preferences import get_trade_preferences
from .live_eth_controls import get_live_eth_limits
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import fetch_privy_wallet_metadata
from .shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,is_direct_settlement_upgrade,changed_wallet_coin,
)
from .shared_evm_policy_reference_rates import fetch_evm_policy_reference_rates
from .shared_evm_wallet_policy_status import _read_saved_and_current_preferences
from .shared_evm_wallet_policy_remote_review import review_original_one_signer_wallet
from .shared_evm_wallet_policy_prepare import _ASSETS, _locked_selected_budget, build_selected_wallet_scope
from .shared_evm_wallet_scope_transition import plan_wallet_policy_transition
from .shared_evm_wallet_policy_intent_ledger import (
    begin_locked_user_policy_update_intent, confirm_locked_user_policy_update_intent,
)
from .storage import _db_url, _ensure_schema, _safe_did

LOG=logging.getLogger("mininex.evm_wallet_scope_extension")
_NONCE=re.compile(r"^[0-9a-f]{64}$")
_SIG=re.compile(r"^0x[0-9a-fA-F]{130}$")


def _desired_user_settings(did,asset):
    if asset=="ETH":
        stable=get_trade_preferences(did)["settings"]["ETH"]["stable"]
        revision=get_live_eth_limits(did)["revision"]
    else:
        item=get_coin_preferences(did)["settings"][asset]
        stable=item["stable"]
        revision=item["revision"]
    if stable not in ("USDC","USDT") or type(revision) is not int or revision<1:
        raise HTTPException(409,"Save your original coin budget before Start Trade")
    return stable,revision


async def _verified_previous_grant(did,asset):
    record,scopes=await asyncio.to_thread(_read_saved_and_current_preferences,did)
    if (type(record) is not dict or record.get("status")!="GRANT_VERIFIED"
            or record.get("legacy_reconciliation_required") is not False
            or type(scopes) is not list or not scopes
            or any(x["asset"]==asset and not is_direct_settlement_upgrade(asset,x)
                   for x in scopes)):
        raise HTTPException(409,"Existing verified wallet and a new or migratable coin required")
    owner,signer=_require_policy_roles()
    if record.get("owner_id")!=owner or record.get("signer_id")!=signer:
        raise HTTPException(409,"Original owner or signer quorum changed")
    user=await fetch_privy_user(did)
    wallet=await fetch_privy_wallet_metadata(record["wallet_id"])
    remote=await _fetch_policy(record["policy_id"])
    try:
        review_original_one_signer_wallet(
            did=did,user=user,wallet_metadata=wallet,
            remote_policy=remote,saved_grant=record,scopes=scopes,
            now=int(time.time()),
        )
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"Original wallet or remote policy changed") from None
    owner_proof=await check_privy_backend_quorum(expected_quorum_id=owner)
    signer_proof=await check_privy_backend_quorum(expected_quorum_id=signer)
    if (owner_proof.get("remote_quorum_public_key_verified") is not True
            or signer_proof.get("remote_quorum_public_key_verified") is not True):
        raise HTTPException(409,"Original Privy policy owner/signer not verified")
    return record,scopes


def _stage_wallet_coin_extension_locked(
    cur,*,did,asset,stable,revision,record,scopes,snapshot,nonce,now,
):
    """Lock wallet and ALL coin budgets before persisting signed intent."""
    cur.execute(
        "SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",
        (did,),
    )
    if cur.fetchone() is None:
        raise HTTPException(409,"Original Privy account unavailable")
    cur.execute(
        """SELECT wallet_id,wallet_address,owner_id,signer_id,policy_id,
                  policy_sha256,status,revision,legacy_reconciliation_required,
                  scopes_json,revisions_json
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    current=cur.fetchone()
    pinned=("wallet_id","wallet_address","owner_id","signer_id",
            "policy_id","policy_sha256","status","legacy_reconciliation_required")
    if (type(current) is not dict
            or any(current.get(key)!=record.get(key) for key in pinned)
            or current.get("status")!="GRANT_VERIFIED"
            or type(current.get("revision")) is not int
            or current["revision"]<1
            or current.get("legacy_reconciliation_required") is not False
            or type(current.get("scopes_json")) is not dict
            or type(current.get("revisions_json")) is not dict
            or current["scopes_json"]!={s["asset"]:s for s in scopes}
            or set(current["scopes_json"])!=set(current["revisions_json"])
            or (asset in current["scopes_json"] and not is_direct_settlement_upgrade(
                asset,current["scopes_json"][asset]))):
        raise HTTPException(409,"Original wallet grant changed during preparation")
    original=[]
    for coin in sorted(current["scopes_json"]):
        original_scope=current["scopes_json"][coin]
        prior_revision=current["revisions_json"][coin]
        if type(original_scope) is not dict or type(prior_revision) is not int:
            raise HTTPException(409,"Original coin scope snapshot invalid")
        _locked_selected_budget(
            cur,did=did,asset=coin,stable=original_scope["stable"],
            expected_revision=prior_revision,
        )
        original.append(original_scope)
    selected_budget,_=_locked_selected_budget(
        cur,did=did,asset=asset,stable=stable,expected_revision=revision,
    )
    cur.execute(
        """SELECT asset FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s AND asset=%s LIMIT 1 FOR UPDATE""",(did,asset),
    )
    if cur.fetchone() is not None:
        raise HTTPException(409,"Legacy signer requires explicit reconciliation")
    if asset=="ETH":
        cur.execute(
            """SELECT status FROM mininex_live_eth_policy_enrollments
               WHERE privy_did=%s LIMIT 1 FOR UPDATE""",(did,),
        )
        if cur.fetchone() is not None:
            raise HTTPException(409,"Legacy ETH signer requires reconciliation")
    proposed=build_selected_wallet_scope(asset=asset,stable=stable,amount=selected_budget,
        snapshot=snapshot,now=now)
    target=[s for s in original if s["asset"]!=asset]+[proposed]
    try:
        changed_wallet_coin(current["scopes_json"],{s["asset"]:s for s in target},
            current["revisions_json"],{**current["revisions_json"],asset:revision})
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None
    old_candidate=compose_scoped_evm_wallet_policy(
        wallet=current["wallet_address"],owner_id=current["owner_id"],
        executor_id=current["signer_id"],scopes=original,now=now,
    )
    plan=plan_wallet_policy_transition(
        did=did,wallet=current["wallet_address"],wallet_id=current["wallet_id"],
        owner_id=current["owner_id"],executor_id=current["signer_id"],
        existing_scopes=original,desired_scopes=target,
        existing_policy=old_candidate,now=now,nonce=nonce,
    )
    if (plan["state"]!="OWNER_SIGNED_POLICY_UPDATE_INTENT_REQUIRED"
            or plan["coins_changed"]!=[asset]):
        raise HTTPException(409,"Original wallet coin scope not changed exactly once")
    response=begin_locked_user_policy_update_intent(
        cur,did=did,plan=plan,now=now,
    )
    revisions={**current["revisions_json"],asset:revision}
    cur.execute(
        """UPDATE mininex_live_evm_wallet_policy_intents
           SET target_scopes_json=%s,target_revisions_json=%s,
               source_grant_revision=%s
           WHERE privy_did=%s AND nonce=%s AND status='PENDING'
           RETURNING status""",(
            Jsonb({x["asset"]:x for x in target}),Jsonb(revisions),
            current["revision"],did,nonce,
        ),
    )
    result=cur.fetchone()
    if type(result) is not dict or result.get("status")!="PENDING":
        raise HTTPException(409,"Cannot bind user approval to new original coin scope")
    return {
        **response,"asset":asset,"nonce":nonce,
        "scope_coins":plan["selected_coins"],
        "expires_at_unix":plan["expires_at_unix"],
        "previous_policy_sha256":plan["previous_policy_sha256"],
        "target_policy_sha256":plan["target_policy_sha256"],
        "one_user_message_signature_required":True,
        "additional_privy_signer_required":False,
    }


async def prepare_existing_wallet_coin_extension(did,asset):
    did=_safe_did(did)
    if asset not in _ASSETS:
        raise HTTPException(404,"Unsupported EVM asset")
    record,scopes=await _verified_previous_grant(did,asset)
    stable,revision=_desired_user_settings(did,asset)
    prices=await fetch_evm_policy_reference_rates(asset,stable)
    now=int(time.time())
    nonce=secrets.token_hex(32)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _stage_wallet_coin_extension_locked(
                    cur,did=did,asset=asset,stable=stable,revision=revision,
                    record=record,scopes=scopes,snapshot=prices,
                    nonce=nonce,now=now,
                )
    except psycopg.Error:
        LOG.warning("Original wallet extension consent storage unavailable")
        raise HTTPException(503,"Original wallet extension consent unavailable") from None


def _confirm_wallet_coin_extension_locked(cur,*,did,nonce,signature,now):
    """Rebuild signed message solely from current locked database records."""
    cur.execute(
        """SELECT wallet_id,wallet_address,owner_id,signer_id,
                  policy_id,policy_sha256,status,revision,
                  legacy_reconciliation_required,scopes_json,revisions_json
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    grant=cur.fetchone()
    if (type(grant) is not dict or grant.get("status")!="GRANT_VERIFIED"
            or grant.get("legacy_reconciliation_required") is not False
            or type(grant.get("revision")) is not int
            or type(grant.get("scopes_json")) is not dict
            or type(grant.get("revisions_json")) is not dict):
        raise HTTPException(409,"Original wallet signer not currently authorized")
    cur.execute(
        """SELECT target_scopes_json,target_revisions_json,source_grant_revision,
                  previous_policy_sha256,target_policy_sha256,
                  challenge_text,challenge_sha256,status,
                  extract(epoch FROM expires_at)::bigint AS expires_at_unix
           FROM mininex_live_evm_wallet_policy_intents
           WHERE privy_did=%s AND nonce=%s FOR UPDATE""",(did,nonce),
    )
    consent=cur.fetchone()
    if (type(consent) is not dict or consent.get("status")!="PENDING"
            or consent.get("source_grant_revision")!=grant["revision"]
            or consent.get("previous_policy_sha256")!=grant["policy_sha256"]
            or type(consent.get("expires_at_unix")) is not int
            or now>=consent["expires_at_unix"]
            or type(consent.get("target_scopes_json")) is not dict
            or type(consent.get("target_revisions_json")) is not dict
            or set(consent["target_scopes_json"])!=set(consent["target_revisions_json"])):
        raise HTTPException(409,"User intent expired or original wallet scope changed")
    try:
        changed_wallet_coin(grant["scopes_json"],consent["target_scopes_json"],
            grant["revisions_json"],consent["target_revisions_json"])
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None
    for coin in sorted(consent["target_scopes_json"]):
        scope=consent["target_scopes_json"][coin]
        revision=consent["target_revisions_json"][coin]
        if type(scope) is not dict or type(revision) is not int or revision<1:
            raise HTTPException(409,"Signed user coin budget is invalid")
        _locked_selected_budget(
            cur,did=did,asset=coin,stable=scope["stable"],
            expected_revision=revision,
        )
    issued=consent["expires_at_unix"]-300
    original=list(grant["scopes_json"].values())
    desired=list(consent["target_scopes_json"].values())
    old_policy=compose_scoped_evm_wallet_policy(
        wallet=grant["wallet_address"],owner_id=grant["owner_id"],
        executor_id=grant["signer_id"],scopes=original,now=issued,
    )
    plan=plan_wallet_policy_transition(
        did=did,wallet=grant["wallet_address"],wallet_id=grant["wallet_id"],
        owner_id=grant["owner_id"],executor_id=grant["signer_id"],
        existing_scopes=original,desired_scopes=desired,
        existing_policy=old_policy,now=issued,nonce=nonce,
    )
    if (plan["state"]!="OWNER_SIGNED_POLICY_UPDATE_INTENT_REQUIRED"
            or plan["intent_challenge"]!=consent["challenge_text"]
            or plan["intent_challenge_sha256"]!=consent["challenge_sha256"]
            or plan["target_policy_sha256"]!=consent["target_policy_sha256"]):
        raise HTTPException(409,"Signed original wallet intent was tampered")
    return confirm_locked_user_policy_update_intent(
        cur,did=did,plan=plan,signature=signature,now=now,
    )


def confirm_existing_wallet_coin_extension(did,nonce,signature):
    did=_safe_did(did)
    if (type(nonce) is not str or not _NONCE.fullmatch(nonce)
            or type(signature) is not str or not _SIG.fullmatch(signature)):
        raise HTTPException(422,"Original wallet nonce and signature required")
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _confirm_wallet_coin_extension_locked(
                    cur,did=did,nonce=nonce,
                    signature=signature,now=int(time.time()),
                )
    except psycopg.Error:
        LOG.warning("Original wallet extension signature storage unavailable")
        raise HTTPException(503,"Original wallet signature unavailable") from None
