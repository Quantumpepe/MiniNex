"""Strict ORIGINAL-wallet, ONE-signer grant adapter for real EVM execution.

A per-coin LIVE worker may use this only after fresh Privy remote readback,
the user's explicit original-wallet approval, and a SQL-locked unchanged
coin budget. Never manufacture legacy per-coin grants or consent records.
The same original user retains control of every separate coin budget.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timezone
import re
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_wallet_policy_prepare import _locked_selected_budget
from .shared_evm_wallet_policy_status import read_owner_central_wallet_policy_status
from .shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,verified_wallet_policy_digest,
)
from .shared_evm_swap_receipts import _pair
from .storage import _db_url,_ensure_schema,_safe_did

_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"))
_ADDR=re.compile(r"^0x[0-9a-f]{40}$")
_SHA=re.compile(r"^[0-9a-f]{64}$")
_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_SCOPE_FIELDS=frozenset((
    "asset","stable","treasury","buy_source_atomic_cap",
    "sell_source_atomic_cap","native_wrap_atomic_cap",
    "owner_max_fee_atomic","expires_at",
))


def _locked_original_evm_coin(cur,*,did:str,asset:str,
                              stable:str|None=None,
                              revision:int|None=None,now:int|None=None)->dict:
    """Lock ONE wallet policy, all included coin scopes and this coin budget.

    The same lock is taken at an actual claim AFTER remote Privy GET.
    A revoked, uncertain, legacy-needing or expired combined grant cannot
    authorize any new order. Returning a per-coin view does not create
    a separate signer or DB permission.
    """
    if asset not in _ASSETS:
        raise HTTPException(404,"Unsupported original wallet EVM coin")
    now=int(time.time()) if now is None else now
    cur.execute(
        """SELECT wallet_id,wallet_address,owner_id,signer_id,
                  policy_id,policy_sha256,status,scopes_json,
                  revisions_json,legacy_reconciliation_required,
                  original_owner_granted_at,remote_verified_at,revoked_at
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    grant=cur.fetchone()
    if (type(grant) is not dict
            or grant.get("status")!="GRANT_VERIFIED"
            or grant.get("legacy_reconciliation_required") is not False
            or grant.get("revoked_at") is not None
            or grant.get("original_owner_granted_at") is None
            or grant.get("remote_verified_at") is None
            or type(grant.get("scopes_json")) is not dict
            or type(grant.get("revisions_json")) is not dict
            or set(grant["scopes_json"])!=set(grant["revisions_json"])
            or not 1<=len(grant["scopes_json"])<=len(_ASSETS)
            or asset not in grant["scopes_json"]
            or type(grant.get("wallet_address")) is not str
            or not _ADDR.fullmatch(grant["wallet_address"])
            or type(grant.get("wallet_id")) is not str
            or not _ID.fullmatch(grant["wallet_id"])
            or type(grant.get("signer_id")) is not str
            or not _ID.fullmatch(grant["signer_id"])
            or type(grant.get("owner_id")) is not str
            or not _ID.fullmatch(grant["owner_id"])
            or grant["signer_id"]==grant["owner_id"]
            or type(grant.get("policy_id")) is not str
            or not _ID.fullmatch(grant["policy_id"])
            or type(grant.get("policy_sha256")) is not str
            or not _SHA.fullmatch(grant["policy_sha256"])):
        raise HTTPException(409,"Original one-signer Privy wallet not authorized")
    selected=grant["scopes_json"][asset]
    ver=grant["revisions_json"][asset]
    if (type(selected) is not dict or set(selected)!=_SCOPE_FIELDS
            or selected.get("asset")!=asset
            or selected.get("stable") not in ("USDC","USDT")
            or (stable is not None and selected["stable"]!=stable)
            or type(ver) is not int or ver<1
            or (revision is not None and revision!=ver)
            or type(selected.get("expires_at")) is not int
            or selected["expires_at"]<=now):
        raise HTTPException(409,"Selected original wallet coin scope changed")
    pair=_pair(asset,selected["stable"],"BUY")
    # The bank of all sibling scopes is preserved by this one wallet policy;
    # the current asset is never allowed to mutate or spend sibling budgets.
    ordered=[]
    for coin in sorted(grant["scopes_json"]):
        scope=grant["scopes_json"][coin]
        v=grant["revisions_json"][coin]
        if (coin not in _ASSETS or type(scope) is not dict
                or set(scope)!=_SCOPE_FIELDS
                or scope.get("asset")!=coin
                or scope.get("stable") not in ("USDC","USDT")
                or type(v) is not int or v<=0
                or type(scope.get("expires_at")) is not int
                or scope["expires_at"]<=now):
            raise HTTPException(409,"Sibling original wallet scope changed")
        # Exact original selected capital and revision, never a platform cap.
        _locked_selected_budget(
            cur,did=did,asset=coin,stable=scope["stable"],
            expected_revision=v,
        )
        ordered.append(scope)
    kwargs=dict(
        wallet=grant["wallet_address"],owner_id=grant["owner_id"],
        executor_id=grant["signer_id"],scopes=ordered,now=now,
    )
    try:
        policy=compose_scoped_evm_wallet_policy(**kwargs)
        digest=verified_wallet_policy_digest(policy,**kwargs)
    except (ValueError,TypeError,KeyError,OverflowError):
        raise HTTPException(409,"Combined Privy policy scope expired or changed") from None
    if digest!=grant["policy_sha256"]:
        raise HTTPException(409,"Saved combined Privy policy digest changed")
    return {
        "authorization_method":"PRIVY_CENTRAL_ONE_SIGNER",
        "asset":asset,"chain_id":pair["chain_id"],
        "stable":selected["stable"],"wallet_id":grant["wallet_id"],
        "wallet_address":grant["wallet_address"],
        "owner_id":grant["owner_id"],"signer_id":grant["signer_id"],
        "policy_id":grant["policy_id"],"policy_sha256":digest,
        "remote_policy_sha256":digest,"status":"GRANT_VERIFIED",
        "budget_revision":ver,
        "scope":selected,"scopes_json":grant["scopes_json"],
        "revisions_json":grant["revisions_json"],
        "candidate_json":policy,
        "expires_at":datetime.fromtimestamp(
            selected["expires_at"],timezone.utc),
        "grant_verified_at":grant["original_owner_granted_at"],
        "consent_verified_at":grant["original_owner_granted_at"],
        "owner_signed_at":grant["original_owner_granted_at"],
        "revoked_at":None,
    }


def original_evm_coin_record(did:str,asset:str)->dict:
    did=_safe_did(did)
    url=_db_url();_ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _locked_original_evm_coin(cur,did=did,asset=asset)


async def audit_original_evm_coin(did:str,asset:str)->dict:
    """Re-fetch real Privy wallet+policy+quorum, then local exact scope.

    A stale frontend status, locally 'ACTIVE' session, or local policy hash
    alone is never adequate permission for an actual wallet RPC.
    """
    did=_safe_did(did)
    if asset not in _ASSETS:
        raise HTTPException(404,"Unsupported central EVM coin")
    status=await read_owner_central_wallet_policy_status(did)
    if (status.get("status")!="REMOTE_VERIFIED_READONLY"
            or asset not in status.get("scope_coins",[])
            or status.get("legacy_reconciliation_required") is not False
            or any(status.get(k) is not True for k in (
                "one_scoped_signer_verified",
                "remote_policy_exactly_verified",
                "current_user_budgets_verified",
                "backend_quorum_current",
                "policy_owner_quorum_current",
            ))):
        raise HTTPException(409,"Original wallet signer or combined Privy policy not verified")
    try:
        record=await asyncio.to_thread(original_evm_coin_record,did,asset)
    except psycopg.Error:
        raise HTTPException(503,"Original wallet coin scope unavailable") from None
    return {
        "mode":"EVM_PRIVY_REMOTE_GRANT_INDEPENDENT_AUDIT",
        "grant_source":"PRIVY_CENTRAL_ONE_SIGNER",
        "asset":asset,"chain_id":record["chain_id"],
        "owner_wallet_current":True,
        "exact_remote_policy_rules_verified":True,
        "privy_additional_signer_scoped":True,
        "backend_quorum_current":True,
        "signed_coin_budget_intent_in_ledger":True,
        "current_user_budget_revision":record["budget_revision"],
        "unresolved_budget_reservations_checked":False,
        "live_session_verified":False,
        "can_broadcast":False,
        "live_execution_enabled_by_this_audit":False,
    }


def central_policy_rule(record:dict,*,asset:str,kind:str,
                        stable:str,side:str|None=None,now:int)->dict:
    """Select exact named rule, never assume the first two are this coin."""
    if (type(record) is not dict
            or record.get("authorization_method")!="PRIVY_CENTRAL_ONE_SIGNER"
            or record.get("status")!="GRANT_VERIFIED"
            or record.get("asset")!=asset
            or record.get("stable")!=stable
            or record.get("revoked_at") is not None
            or type(record.get("scope")) is not dict
            or type(record.get("candidate_json")) is not dict
            or type(now) is not int or now<=0
            or record["scope"].get("expires_at",0)<=now):
        raise ValueError("Original combined wallet policy not current")
    if kind=="swap" and side in ("BUY","SELL"):
        name=f"MiniNex {asset} {side} {stable} exactInputSingle"
    elif kind=="approval" and side in ("BUY","SELL"):
        name=f"MiniNex {asset} {side} finite Router02 allowance"
    elif kind=="fee" and side is None:
        name=f"MiniNex {asset} {stable} owner-approved net-profit fee"
    else:
        raise ValueError("No such original-wallet scoped operation")
    matched=[r for r in record["candidate_json"].get("rules",[])
             if type(r) is dict and r.get("name")==name]
    if len(matched)!=1 or matched[0].get("action")!="ALLOW":
        raise ValueError("Original wallet policy rule not uniquely scoped")
    return matched[0]
