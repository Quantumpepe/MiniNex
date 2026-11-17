"""Stage one immutable per-coin Privy policy from an actual signed owner intent.

Internal candidate preparation only: user sign-in and signed budget consent
are verified before a deterministic chain/Router02 blueprint is persisted.
An independently verified live quote/wallet balance adapter must provide
caps; no client HTTP can supply them. No Privy policy is created by this
module, no wallet delegate is granted and NO real transaction is signed.
"""
from __future__ import annotations

from datetime import datetime,timezone
import hashlib
import json
import logging
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .shared_evm_privy_user_policy import build_user_evm_policy_blueprint
from .shared_evm_owner_consent import _asset,_locked_coin_settings
from .shared_evm_swap_receipts import _pair
from .shared_evm_policy_reference_rates import (
    derive_user_budget_evm_atomic_caps,fetch_evm_policy_reference_rates,
)
from .extra_coin_preferences import get_coin_preferences
from .privy_live_signer import _owned_eth_wallet
from .privy_wallet_verification import fetch_privy_user
from .shared_evm_trade_policy_roles import require_coin_trade_policy_roles as _require_policy_roles
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.evm_policy_candidate")


def _stage_locked(
    cur,*,did:str,asset:str,owned:dict,
    owner_id:str,signer_id:str,
    buy_source_atomic_cap:str,sell_source_atomic_cap:str,
    native_wrap_atomic_cap:str|None,expires_at:int,now:int,
)->dict:
    """One-shot immutable policy snapshot; no HTTP and no Privy POST."""
    asset=_asset(asset)
    settings=_locked_coin_settings(cur,did=did,asset=asset)
    stable=settings["stable"]
    pair=_pair(asset,stable,"BUY")
    cur.execute(
        """SELECT status,wallet_id,wallet_address,chain_id,stable,
                  budget_revision,budget_usd,message_sha256,confirmed_at
           FROM mininex_live_evm_consent_challenges
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    intent=cur.fetchone()
    if (type(intent) is not dict
            or intent.get("status")!="SIGNED_INTENT"
            or intent.get("confirmed_at") is None
            or intent.get("wallet_id")!=owned["wallet_id"]
            or intent.get("wallet_address")!=owned["address"]
            or intent.get("chain_id")!=pair["chain_id"]
            or intent.get("stable")!=stable
            or intent.get("budget_revision")!=settings["revision"]
            or int(intent.get("budget_usd",0))!=settings["budget_usd"]
            or type(intent.get("message_sha256")) is not str
            or len(intent["message_sha256"])!=64):
        raise HTTPException(409,"Current owner-signed coin budget intent required")
    cur.execute(
        """SELECT status FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    previous=cur.fetchone()
    if previous is not None:
        raise HTTPException(409,"Already staged EVM owner policy requires audit")
    candidate=build_user_evm_policy_blueprint(
        asset=asset,stable=stable,wallet=owned["address"],
        owner_id=owner_id,executor_id=signer_id,
        buy_source_atomic_cap=buy_source_atomic_cap,
        sell_source_atomic_cap=sell_source_atomic_cap,
        native_wrap_atomic_cap=native_wrap_atomic_cap,
        expires_at=expires_at,now=now)
    digest=hashlib.sha256(json.dumps(
        candidate,sort_keys=True,separators=(",",":")
    ).encode("utf-8")).hexdigest()
    cur.execute(
        """INSERT INTO mininex_live_evm_owner_permissions
           (privy_did,asset,chain_id,stable,wallet_id,wallet_address,
            owner_id,signer_id,policy_sha256,
            consent_sha256,consent_verified_at,expires_at,
            status,budget_revision,candidate_json)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),
                   to_timestamp(%s),'DRAFT',%s,%s)
           ON CONFLICT(privy_did,asset) DO NOTHING
           RETURNING status""",
        (did,asset,pair["chain_id"],stable,owned["wallet_id"],
         owned["address"],owner_id,signer_id,digest,
         intent["message_sha256"],expires_at,
         settings["revision"],Jsonb(candidate)))
    inserted=cur.fetchone()
    if type(inserted) is not dict or inserted.get("status")!="DRAFT":
        raise HTTPException(409,"Concurrent coin policy candidate conflict")
    return {
        "mode":"OWNER_SIGNED_EVM_PRIVY_POLICY_DRAFT_ONLY",
        "asset":asset,"chain_id":pair["chain_id"],
        "policy_sha256":digest,"rules_count":len(candidate["rules"]),
        "signed_budget_revision":settings["revision"],
        "policy_published_in_privy":False,
        "owner_delegation_granted":False,
        "live_auto_enabled":False,
    }


async def stage_internal_evm_policy_candidate(
    *,did:str,asset:str,buy_source_atomic_cap:str,
    sell_source_atomic_cap:str,native_wrap_atomic_cap:str|None,
    expires_at:int,
)->dict:
    """For a future trusted independently priced server worker only.

    No public endpoint is attached. Caps must be bound to real wallet
    balances and the user's selected coin budget by the caller before
    this method can be enabled. A prepared policy grants no spending.
    """
    did=_safe_did(did)
    asset=_asset(asset)
    owner_id,signer_id=_require_policy_roles(asset)
    user=await fetch_privy_user(did)
    owned=_owned_eth_wallet(user,did)
    now=int(time.time())
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _stage_locked(
                    cur,did=did,asset=asset,owned=owned,
                    owner_id=owner_id,signer_id=signer_id,
                    buy_source_atomic_cap=buy_source_atomic_cap,
                    sell_source_atomic_cap=sell_source_atomic_cap,
                    native_wrap_atomic_cap=native_wrap_atomic_cap,
                    expires_at=expires_at,now=now)
    except psycopg.Error:
        LOG.warning("Coin policy candidate persistence unavailable")
        raise HTTPException(503,"Could not stage EVM policy draft") from None


def _stage_from_prices_locked(
    cur,*,did:str,asset:str,owned:dict,
    owner_id:str,signer_id:str,
    snapshot:dict,now:int,
)->dict:
    """Recompute caps INSIDE locked budget transaction; no stale settings."""
    settings=_locked_coin_settings(cur,did=did,asset=asset)
    caps=derive_user_budget_evm_atomic_caps(
        asset=asset,stable=settings["stable"],
        budget_usd=settings["budget_usd"],
        snapshot=snapshot,now=now)
    return _stage_locked(
        cur,did=did,asset=asset,owned=owned,
        owner_id=owner_id,signer_id=signer_id,
        buy_source_atomic_cap=caps["buy_source_atomic_cap"],
        sell_source_atomic_cap=caps["sell_source_atomic_cap"],
        native_wrap_atomic_cap=caps["native_wrap_atomic_cap"],
        expires_at=now+24*3600,now=now)


async def stage_owner_evm_policy_from_budget(did:str,asset:str)->dict:
    """Explicit owner-triggered local policy PREPARE; no Privy policy POST.

    Reads only authenticated coin settings and fresh fixed-provider market
    references, never UI-provided USD prices, limits, caps or token targets.
    """
    did=_safe_did(did)
    asset=_asset(asset)
    settings=get_coin_preferences(did)["settings"][asset]
    if (type(settings) is not dict
            or type(settings.get("revision")) is not int
            or settings["revision"]<1
            or type(settings.get("stable")) is not str):
        raise HTTPException(409,"Save your coin settings before LIVE enrollment")
    snapshot=await fetch_evm_policy_reference_rates(
        asset,settings["stable"])
    owner_id,signer_id=_require_policy_roles(asset)
    user=await fetch_privy_user(did)
    owned=_owned_eth_wallet(user,did)
    now=int(time.time())
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _stage_from_prices_locked(
                    cur,did=did,asset=asset,owned=owned,
                    owner_id=owner_id,signer_id=signer_id,
                    snapshot=snapshot,now=now)
    except psycopg.Error:
        LOG.warning("Signed owner EVM policy preparation unavailable")
        raise HTTPException(503,"Cannot prepare coin Privy policy") from None
