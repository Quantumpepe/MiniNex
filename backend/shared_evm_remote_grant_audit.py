"""Independent remote Privy owner-grant readback for five MiniNex EVM coins.

This verifies the stored server-generated candidate against the exact policy
factory, an EIP191 signed budget/coin intent, current wallet metadata and the
actual Privy policy GET. It cannot create a policy or signer, initiate trades,
or turn a read-only review into execution authority.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timezone
import hashlib
import json
import re

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_privy_user_policy import build_user_evm_policy_blueprint
from .shared_evm_privy_grant_review import verify_exact_remote_evm_policy
from .shared_evm_swap_receipts import _pair
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import (
    _owned_eth_wallet,fetch_privy_wallet_metadata,_scoped_signer,
)
from .eth_privy_user_grant import _fetch_policy
from .shared_evm_trade_policy_roles import check_coin_trade_backend_quorum as check_privy_backend_quorum
from .storage import _db_url,_ensure_schema,_safe_did

_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI","ARB"))
_SHA=re.compile(r"^[0-9a-f]{64}$")


def _lte(rule:dict,field:str)->str:
    conditions=rule.get("conditions")
    if type(conditions) is not list:
        raise ValueError("Invalid reviewed Privy rule")
    match=[x.get("value") for x in conditions
           if type(x) is dict and x.get("field")==field
           and x.get("operator")=="lte"]
    if len(match)!=1 or type(match[0]) is not str:
        raise ValueError("Missing exact atomic amount ceiling")
    return match[0]


def _expiry(policy:dict)->int:
    found=[]
    for rule in policy.get("rules",[]):
        conditions=rule.get("conditions")
        if type(conditions) is not list:
            raise ValueError("Invalid reviewed policy conditions")
        exp=[x.get("value") for x in conditions
             if type(x) is dict and
             x.get("field_source")=="system" and
             x.get("field")=="current_unix_timestamp"
             and x.get("operator")=="lt"]
        if len(exp)!=1 or type(exp[0]) is not str or not exp[0].isdigit():
            raise ValueError("Exact policy expiry required on each allow rule")
        found.append(int(exp[0]))
    if not found or len(set(found))!=1:
        raise ValueError("Cannot audit mismatched policy expiry")
    return found[0]


def validate_pinned_evm_policy(record:object,*,asset:str,now:int)->dict:
    """Rebuild approved rules, rather than trusting arbitrary database JSON."""
    if type(record) is not dict or asset not in _ASSETS:
        raise ValueError("Untrusted pinned owner permission")
    candidate=record.get("candidate_json")
    if type(candidate) is not dict or type(candidate.get("rules")) is not list:
        raise ValueError("No previously reviewed EVM policy candidate")
    rules=candidate["rules"]
    if len(rules)!=(4 if asset in ("BNB","POL","AVAX") else 2):
        raise ValueError("Noncanonical per-coin Privy policy actions")
    expires=_expiry(candidate)
    if (type(now) is not int or not now<expires<=now+30*86400
            or record.get("expires_at") is None
            or not isinstance(record["expires_at"],datetime)
            or record["expires_at"].tzinfo is None
            or int(record["expires_at"].timestamp())!=expires):
        raise ValueError("Expired or inconsistent Privy policy")
    if record.get("asset")!=asset:
        raise ValueError("Wrong asset policy")
    expected=build_user_evm_policy_blueprint(
        asset=asset,stable=record["stable"],
        wallet=record["wallet_address"],
        owner_id=record["owner_id"],executor_id=record["signer_id"],
        buy_source_atomic_cap=_lte(rules[0],"exactInputSingle.params.amountIn"),
        sell_source_atomic_cap=_lte(rules[1],"exactInputSingle.params.amountIn"),
        native_wrap_atomic_cap=(
            _lte(rules[2],"value") if len(rules)==4 else None),
        expires_at=expires,now=now)
    if candidate!=expected:
        raise ValueError("Pinned policy differs from current strict factory")
    digest=hashlib.sha256(json.dumps(
        expected,sort_keys=True,separators=(",",":")
    ).encode("utf-8")).hexdigest()
    if record.get("policy_sha256")!=digest:
        raise ValueError("Stored EVM policy SHA256 changed")
    return expected


def _read_grant_evidence_locked(cur,*,did:str,asset:str)->dict:
    cur.execute(
        """SELECT asset,chain_id,stable,wallet_id,wallet_address,
                  owner_id,signer_id,policy_id,policy_sha256,
                  remote_policy_sha256,consent_sha256,
                  consent_verified_at,grant_verified_at,revoked_at,
                  expires_at,status,budget_revision,candidate_json
           FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    record=cur.fetchone()
    if (type(record) is not dict or record.get("status")!="GRANT_VERIFIED"
            or record.get("revoked_at") is not None
            or record.get("consent_verified_at") is None
            or record.get("grant_verified_at") is None
            or record.get("remote_policy_sha256")!=record.get("policy_sha256")
            or type(record.get("policy_id")) is not str
            or type(record.get("consent_sha256")) is not str
            or not _SHA.fullmatch(record["consent_sha256"])):
        raise HTTPException(409,"No independently confirmed EVM wallet grant")
    pair=_pair(asset,record["stable"],"BUY")
    if record["chain_id"]!=pair["chain_id"]:
        raise HTTPException(409,"Wrong EVM network in permission record")
    cur.execute(
        """SELECT status,wallet_id,wallet_address,chain_id,stable,
                  budget_revision,message_sha256,confirmed_at
           FROM mininex_live_evm_consent_challenges
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    intent=cur.fetchone()
    if (type(intent) is not dict
            or intent.get("status")!="SIGNED_INTENT"
            or intent.get("confirmed_at") is None
            or intent.get("wallet_id")!=record["wallet_id"]
            or intent.get("wallet_address")!=record["wallet_address"]
            or intent.get("chain_id")!=record["chain_id"]
            or intent.get("stable")!=record["stable"]
            or intent.get("budget_revision")!=record["budget_revision"]
            or intent.get("message_sha256")!=record["consent_sha256"]):
        raise HTTPException(409,"Coin policy and owner's signed budget differ")
    return record


def _read_grant_record(did:str,asset:str)->dict:
    url=_db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _read_grant_evidence_locked(cur,did=did,asset=asset)


async def get_owner_evm_remote_grant_audit(did:str,asset:str)->dict:
    did=_safe_did(did)
    if asset not in _ASSETS:
        raise HTTPException(404,"Unsupported per-coin Privy policy")
    try:
        record=await asyncio.to_thread(_read_grant_record,did,asset)
    except psycopg.Error:
        raise HTTPException(503,"EVM signed grant audit unavailable") from None
    try:
        from time import time
        expected=validate_pinned_evm_policy(record,asset=asset,now=int(time()))
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"Pinned per-coin Privy policy is not current") from None
    user=await fetch_privy_user(did)
    try:
        owned=_owned_eth_wallet(user,did)
        if (owned["wallet_id"]!=record["wallet_id"]
                or owned["address"]!=record["wallet_address"]
                or not owned.get("delegated")):
            raise ValueError("Owner Privy wallet changed")
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Privy owner wallet no longer matches consent") from None
    metadata=await fetch_privy_wallet_metadata(owned["wallet_id"])
    policy=await _fetch_policy(record["policy_id"])
    try:
        remote=verify_exact_remote_evm_policy(policy,expected)
        signer=_scoped_signer(
            metadata,did=did,address=owned["address"],
            config={"signer_id":record["signer_id"],
                    "policy_id":record["policy_id"]})
        matched=(remote["policy_id"]==record["policy_id"]
                 and remote["policy_sha256"]==record["policy_sha256"])
    except (ValueError,TypeError,KeyError):
        matched=False
        signer=False
    quorum=await check_privy_backend_quorum(asset)
    return {
        "mode":"EVM_PRIVY_REMOTE_GRANT_INDEPENDENT_AUDIT",
        "asset":asset,"chain_id":record["chain_id"],
        "owner_wallet_current":True,
        "exact_remote_policy_rules_verified":matched,
        "privy_additional_signer_scoped":signer is True,
        "backend_quorum_current":(
            quorum.get("remote_quorum_public_key_verified") is True),
        "signed_coin_budget_intent_in_ledger":True,
        "unresolved_budget_reservations_checked":False,
        "live_session_verified":False,
        "can_broadcast":False,
        "live_execution_enabled_by_this_audit":False,
    }
