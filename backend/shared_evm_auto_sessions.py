"""Owner-driven per-coin EVM AUTO session state, no automatic sends.

Only a user-authenticated POST may start the session, and ONLY after fresh
remote Privy policy/signer/quorum audit and durable signed-owner intent.
The start endpoint does not send coins. Pause does not retry/cancel any
already committed blockchain transaction. Capital remains per-coin.
"""
from __future__ import annotations

import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_remote_grant_audit import (
    get_owner_evm_remote_grant_audit,_read_grant_evidence_locked,
)
from .shared_evm_swap_receipts import _pair
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.evm_live_auto_sessions")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI"))


def _require_asset(asset:object)->str:
    if type(asset) is not str or asset not in _ASSETS:
        raise HTTPException(404,"Unsupported live EVM coin session")
    return asset


def _remote_ready(report:object,asset:str)->None:
    if (type(report) is not dict
            or report.get("mode")!="EVM_PRIVY_REMOTE_GRANT_INDEPENDENT_AUDIT"
            or report.get("asset")!=asset
            or any(report.get(field) is not True for field in (
                "owner_wallet_current","exact_remote_policy_rules_verified",
                "privy_additional_signer_scoped","backend_quorum_current",
                "signed_coin_budget_intent_in_ledger"))):
        raise HTTPException(409,"Current approved Privy owner grant is missing")


def _start_locked(cur,*,did:str,asset:str)->dict:
    """Lock independently verified consent/policy and current user budget."""
    record=_read_grant_evidence_locked(cur,did=did,asset=asset)
    cur.execute(
        """SELECT stable,revision,budget_usd
           FROM mininex_extra_coin_preferences
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    settings=cur.fetchone()
    pair=_pair(asset,record["stable"],"BUY")
    if (type(settings) is not dict
            or settings.get("revision")!=record.get("budget_revision")
            or settings.get("stable")!=record["stable"]
            or type(settings.get("budget_usd")) is not int
            or settings["budget_usd"]<=0
            or record.get("chain_id")!=pair["chain_id"]
            or record.get("wallet_address") is None):
        raise HTTPException(409,"User's coin budget differs from signed approval")
    cur.execute(
        """INSERT INTO mininex_live_evm_auto_sessions
           (privy_did,asset,chain_id,stable,wallet_address,
            budget_revision,status,started_by_owner_at)
           VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE',now())
           ON CONFLICT(privy_did,asset) DO UPDATE SET
             chain_id=EXCLUDED.chain_id,stable=EXCLUDED.stable,
             wallet_address=EXCLUDED.wallet_address,
             budget_revision=EXCLUDED.budget_revision,
             status='ACTIVE',started_by_owner_at=now(),
             updated_at=now()
           RETURNING status""",
        (did,asset,pair["chain_id"],record["stable"],
         record["wallet_address"],settings["revision"]))
    saved=cur.fetchone()
    if type(saved) is not dict or saved.get("status")!="ACTIVE":
        raise HTTPException(409,"Concurrent EVM owner session conflict")
    return {
        "mode":"OWNER_EVM_LIVE_SESSION_CONTROL",
        "asset":asset,"chain_id":pair["chain_id"],
        "stable":record["stable"],"status":"ACTIVE",
        "started_by_owner":True,
        "worker_attached":False,"new_transactions_sent":0,
    }


def _stop_locked(cur,*,did:str,asset:str)->dict:
    cur.execute(
        """UPDATE mininex_live_evm_auto_sessions
           SET status='PAUSED',updated_at=now()
           WHERE privy_did=%s AND asset=%s AND status='ACTIVE'
           RETURNING status""",(did,asset))
    row=cur.fetchone()
    return {
        "mode":"OWNER_EVM_LIVE_SESSION_CONTROL",
        "asset":asset,"status":"PAUSED",
        "changed":type(row) is dict and row.get("status")=="PAUSED",
        "new_transactions_sent":0,
        "already_submitted_transactions_not_retried_or_cancelled":True,
    }


async def start_owner_evm_auto(did:str,asset:str)->dict:
    did=_safe_did(did)
    asset=_require_asset(asset)
    # Must be independent live Privy REST readback, never a cached frontend
    # checkbox or an unverified 'policy ready' database string.
    report=await get_owner_evm_remote_grant_audit(did,asset)
    _remote_ready(report,asset)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _start_locked(cur,did=did,asset=asset)
    except psycopg.Error:
        LOG.warning("Owner EVM live session startup persistence unavailable")
        raise HTTPException(503,"Cannot durably start EVM live session") from None


def pause_owner_evm_auto(did:str,asset:str)->dict:
    did=_safe_did(did)
    asset=_require_asset(asset)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _stop_locked(cur,did=did,asset=asset)
    except psycopg.Error:
        LOG.warning("Owner EVM live session pause unavailable")
        raise HTTPException(503,"Cannot durably pause EVM live session") from None


def get_owner_evm_auto(did:str,asset:str)->dict:
    did=_safe_did(did)
    asset=_require_asset(asset)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT status,chain_id,stable,budget_revision
                       FROM mininex_live_evm_auto_sessions
                       WHERE privy_did=%s AND asset=%s""",(did,asset))
                row=cur.fetchone()
    except psycopg.Error:
        raise HTTPException(503,"EVM live session status unavailable") from None
    return {
        "mode":"OWNER_EVM_LIVE_SESSION_CONTROL",
        "asset":asset,"status":row["status"] if type(row) is dict else "NOT_STARTED",
        "chain_id":row.get("chain_id") if type(row) is dict else None,
        "stable":row.get("stable") if type(row) is dict else None,
        "worker_attached":False,"new_transactions_sent":0,
    }
