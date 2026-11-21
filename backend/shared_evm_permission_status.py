"""Owner-scoped, read-only per-asset Privy consent/grant journal for MiniNex.

A verified remote Privy policy is not by itself a signed user intent. This
ledger records independent identity and permission milestones, but does not
issue policy, delegate signer, activate a session, sign or broadcast money.
Actual permission verification writers are intentionally not public.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import re

import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException

from .storage import _db_url, _ensure_schema, _safe_did
from .shared_evm_swap_receipts import _pair

LOG=logging.getLogger("mininex.evm_grant_ledger")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI","ARB"))
_STATES=frozenset(("DRAFT","CONSENT_VERIFIED","GRANT_VERIFIED","REVOKED"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_ADDR=re.compile(r"^0x[0-9a-f]{40}$")
_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")


def assess_evm_permission_record(record:object, *,
                                 asset:str,now:datetime)->dict:
    """No frontend-reported grant or expired policy is ever sufficient."""
    if asset not in _ASSETS or type(now) is not datetime or now.tzinfo is None:
        raise ValueError("Unverified owner EVM permission query")
    stable=(record.get("stable") if type(record) is dict else
            ("USDT" if asset=="BNB" else "USDC"))
    try:
        pair=_pair(asset,stable,"BUY")
    except (ValueError,HTTPException):
        raise ValueError("Unsupported selected stablecoin route") from None
    result={
        "mode":"OWNER_EVM_PRIVY_PERMISSION_STATUS_ONLY",
        "asset":asset,"chain_id":pair["chain_id"],"network":pair["network"],
        "status":"NOT_PREPARED",
        "stored_wallet_address":None,
        "stored_wallet_id":None,
        "owner_wallet_bound":False,
        "user_consent_recorded":False,
        "authorization_method":None,
        "server_policy_digest_matched":False,
        "policy_published":False,
        "signer_grant_recorded":False,
        "policy_current":False,
        "privy_live_readback_completed":False,
        "live_worker_permission_verified":False,
        "auto_execution_enabled":False,
        "can_submit":False,
    }
    if record is None:
        return result
    if type(record) is not dict:
        raise ValueError("Malformed EVM permission ledger")
    state=record.get("status")
    expires=record.get("expires_at")
    if (state not in _STATES
            or record.get("asset")!=asset
            or record.get("chain_id")!=pair["chain_id"]
            or record.get("stable")!=stable
            or type(record.get("wallet_address")) is not str
            or not _ADDR.fullmatch(record["wallet_address"])
            or type(record.get("wallet_id")) is not str
            or not _ID.fullmatch(record["wallet_id"])
            or type(record.get("owner_id")) is not str
            or not _ID.fullmatch(record["owner_id"])
            or type(record.get("signer_id")) is not str
            or not _ID.fullmatch(record["signer_id"])
            or record["owner_id"]==record["signer_id"]
            or type(record.get("policy_sha256")) is not str
            or not _SHA.fullmatch(record["policy_sha256"])
            or not isinstance(expires,datetime)
            or expires.tzinfo is None):
        raise ValueError("Incorrect wallet/chain/permission record")
    result["status"]=state
    result["authorization_method"]=record.get(
        "authorization_method","EIP191_AND_PRIVY")
    result["stored_wallet_address"]=record["wallet_address"]
    result["stored_wallet_id"]=record["wallet_id"]
    result["owner_wallet_bound"]=True  # Ledger field match; live GET still needed
    result["policy_current"]=expires>now and state!="REVOKED"
    consent=(record.get("consent_verified_at") is not None
             and record.get("consent_sha256") is not None
             and type(record["consent_sha256"]) is str
             and _SHA.fullmatch(record["consent_sha256"]) is not None)
    if state in ("CONSENT_VERIFIED","GRANT_VERIFIED") and not consent:
        raise ValueError("Missing independent EVM user consent")
    result["user_consent_recorded"]=bool(consent and state in (
        "CONSENT_VERIFIED","GRANT_VERIFIED"))
    matching=(record.get("remote_policy_sha256")==record["policy_sha256"]
              and type(record.get("policy_id")) is str
              and _ID.fullmatch(record["policy_id"]) is not None)
    result["policy_published"]=bool(matching)
    if state=="GRANT_VERIFIED" and (
            not matching or record.get("grant_verified_at") is None):
        raise ValueError("Unverified remote EVM grant")
    result["server_policy_digest_matched"]=bool(matching and state=="GRANT_VERIFIED")
    result["signer_grant_recorded"]=bool(
        state=="GRANT_VERIFIED" and matching and consent
        and record.get("grant_verified_at") is not None
        and expires>now)
    # A stored grant does not prove current Privy metadata, revocation,
    # spend balance, remote signer quorum or the current user session.
    return result


def read_owner_evm_permission_status(did:str,*,asset:str)->dict:
    did=_safe_did(did)
    if asset not in _ASSETS:
        raise HTTPException(404,"Unsupported EVM trading asset")
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT asset,chain_id,stable,wallet_id,wallet_address,
                              owner_id,signer_id,policy_id,policy_sha256,
                              remote_policy_sha256,consent_sha256,
                              consent_verified_at,grant_verified_at,
                              revoked_at,expires_at,status,authorization_method
                       FROM mininex_live_evm_owner_permissions
                       WHERE privy_did=%s AND asset=%s""",(did,asset))
                record=cur.fetchone()
    except psycopg.Error:
        LOG.warning("Owner EVM grant ledger unavailable")
        raise HTTPException(503,"Owner EVM permission ledger unavailable") from None
    return assess_evm_permission_record(
        record,asset=asset,now=datetime.now(timezone.utc))
