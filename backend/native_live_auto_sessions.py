"""Owner-started BTC/SOL/XRP/TRX/DOGE AUTO session controls.

An active session is granted by the signed-in owner only after an independently
audited real Privy wallet policy. This control never creates a signer, invents
consent, sends money, starts shadow mode, or adds a system capital limit.
A paused session only prevents future NEW trades; passive receipt finality
continues independently.
"""
from __future__ import annotations

from datetime import datetime,timezone
import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .storage import _db_url,_ensure_schema,_safe_did
from .native_live_budget import _NATIVE

LOG=logging.getLogger("mininex.native_owner_sessions")
_NATIVE_ASSETS=frozenset(_NATIVE)


def _asset(asset:object)->str:
    if type(asset) is not str or asset not in _NATIVE_ASSETS:
        raise HTTPException(404,"Unsupported original native AUTO coin")
    return asset


def _original_grant_for_start(report:object,*,did:str,asset:str)->dict:
    """An app checkbox or database DRAFT is never a LIVE owner grant."""
    if (type(report) is not dict
            or report.get("mode")!="NATIVE_PRIVY_REMOTE_GRANT_INDEPENDENT_AUDIT"
            or report.get("asset")!=asset or report.get("did")!=did
            or any(report.get(k) is not True for k in (
                "owner_wallet_current","exact_remote_policy_verified",
                "backend_quorum_current","owner_signed_budget_current",
                "user_consent_current"))
            or type(report.get("budget_revision")) is not int
            or report["budget_revision"]<=0
            or report.get("stable") not in _NATIVE[asset][2]
            or type(report.get("source_wallet")) is not str
            or not report["source_wallet"].startswith("0x")):
        raise HTTPException(409,"Original native Privy owner grant not verified")
    return report


def _current_coin_settings_locked(cur,*,did:str,asset:str,stable:str,
                                  revision:int)->dict:
    """Owner's BTC percent versus independent additional-coin USD budget."""
    if asset=="BTC":
        cur.execute(
            """SELECT b.percent,b.revision,t.stable,t.mode,t.horizon
               FROM mininex_budget_preferences b
               JOIN mininex_trade_preferences t
                 ON t.privy_did=b.privy_did AND t.asset=b.asset
               WHERE b.privy_did=%s AND b.asset='BTC'
               FOR UPDATE OF b,t""",(did,))
        row=cur.fetchone()
        ok=(type(row) is dict and type(row.get("percent")) is int
            and 0<row["percent"]<=100)
    else:
        cur.execute(
            """SELECT stable,revision,budget_usd,mode,horizon
               FROM mininex_extra_coin_preferences
               WHERE privy_did=%s AND asset=%s FOR UPDATE""",
            (did,asset))
        row=cur.fetchone()
        ok=(type(row) is dict and type(row.get("budget_usd")) is int
            and row["budget_usd"]>0)
    if (not ok or row.get("revision")!=revision or row.get("stable")!=stable
            or row.get("mode") not in ("LONG","AUTO","SPOT_SHORT")
            or row.get("horizon") not in ("5m","15m","1h","24h")):
        raise HTTPException(409,"Original coin budget changed since owner grant")
    return row


def _start_locked(cur,*,did:str,asset:str,grant:dict)->dict:
    grant=_original_grant_for_start(grant,did=did,asset=asset)
    cur.execute(
        "SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",
        (did,))
    owner=cur.fetchone()
    if type(owner) is not dict or owner.get("privy_did")!=did:
        raise HTTPException(409,"Original owner account unavailable")
    coin=_current_coin_settings_locked(
        cur,did=did,asset=asset,stable=grant["stable"],
        revision=grant["budget_revision"])
    cur.execute(
        """INSERT INTO mininex_live_native_auto_sessions
             (privy_did,asset,stable,budget_revision,status,
              wallet_address,started_by_owner_at)
           VALUES (%s,%s,%s,%s,'ACTIVE',%s,now())
           ON CONFLICT (privy_did,asset) DO UPDATE
           SET stable=EXCLUDED.stable,budget_revision=EXCLUDED.budget_revision,
               wallet_address=EXCLUDED.wallet_address,
               status='ACTIVE',started_by_owner_at=now(),
               updated_at=now()
           WHERE mininex_live_native_auto_sessions.status<>'EXIT_PENDING'
           RETURNING status""",
        (did,asset,grant["stable"],coin["revision"],
         grant["source_wallet"]))
    saved=cur.fetchone()
    if type(saved) is not dict or saved.get("status")!="ACTIVE":
        raise HTTPException(409,"Owner native session could not start")
    return {"mode":"OWNER_NATIVE_LIVE_SESSION_CONTROL",
            "asset":asset,"status":"ACTIVE",
            "stable":grant["stable"],"started_by_owner":True,
            "trading_execution_confirmed":False,
            "original_blockchain_sends":0}


async def start_owner_native_auto(did:str,asset:str,*,grant_auditor)->dict:
    """Only call after the single original owner Privy signing step.

    The auditor must independently read owner wallet/policy/signer and
    current budget. The app may never manufacture this evidence from UI.
    """
    did=_safe_did(did);asset=_asset(asset)
    if not callable(grant_auditor):
        raise HTTPException(409,"Native remote Privy owner auditor unavailable")
    grant=await grant_auditor(did,asset)
    _original_grant_for_start(grant,did=did,asset=asset)
    try:
        db=_db_url()
        _ensure_schema(db)
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _start_locked(cur,did=did,asset=asset,grant=grant)
    except psycopg.Error:
        LOG.warning("Original native start session persistence unavailable")
        raise HTTPException(503,"Original native session database unavailable") from None


def pause_owner_native_auto(did:str,asset:str)->dict:
    did=_safe_did(did);asset=_asset(asset)
    try:
        db=_db_url();_ensure_schema(db)
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """UPDATE mininex_live_native_auto_sessions
                       SET status='PAUSED',updated_at=now()
                       WHERE privy_did=%s AND asset=%s AND status IN ('ACTIVE','EXIT_PENDING')
                       RETURNING status""",(did,asset))
                value=cur.fetchone()
    except psycopg.Error:
        raise HTTPException(503,"Owner native session pause unavailable") from None
    return {"mode":"OWNER_NATIVE_LIVE_SESSION_CONTROL",
            "asset":asset,"status":"PAUSED",
            "changed":type(value) is dict and value.get("status")=="PAUSED",
            "original_source_transaction_not_retried":True,
            "new_transactions_sent":0}


def get_owner_native_auto(did:str,asset:str)->dict:
    did=_safe_did(did);asset=_asset(asset)
    try:
        db=_db_url();_ensure_schema(db)
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT status,stable,budget_revision
                       FROM mininex_live_native_auto_sessions
                       WHERE privy_did=%s AND asset=%s""",(did,asset))
                session=cur.fetchone()
                cur.execute(
                    """SELECT COUNT(*) AS submitted,
                              COUNT(*) FILTER (
                                WHERE status='DESTINATION_FINALIZED'
                              ) AS finalized
                       FROM mininex_live_native_order_intents
                       WHERE privy_did=%s AND asset=%s
                         AND side IN ('BUY','SELL') AND source_tx_hash IS NOT NULL""",
                    (did,asset))
                sends=cur.fetchone()
                cur.execute(
                    """SELECT last_poll_at FROM mininex_live_worker_heartbeats
                       WHERE worker_name='native_auto'
                         AND status='RUNNING'
                         AND last_poll_at>now()-INTERVAL '90 seconds'""")
                heartbeat=cur.fetchone()
    except psycopg.Error:
        raise HTTPException(503,"Owner native session status unavailable") from None
    return {
        "mode":"OWNER_NATIVE_LIVE_SESSION_CONTROL",
        "asset":asset,
        "status":session["status"] if type(session) is dict else "NOT_STARTED",
        "stable":session["stable"] if type(session) is dict else None,
        "worker_attached":type(heartbeat) is dict
            and heartbeat.get("last_poll_at") is not None,
        "original_blockchain_sources_sent":int(sends.get("submitted") or 0)
            if type(sends) is dict else 0,
        "original_native_payouts_finalized":int(sends.get("finalized") or 0)
            if type(sends) is dict else 0,
        "new_transactions_sent":0,
    }


NATIVE_EXIT_ASSETS=frozenset(("BTC","SOL"))


def _request_native_exit_locked(cur,did,asset):
    cur.execute("""SELECT privy_did FROM mininex_accounts
        WHERE privy_did=%s FOR UPDATE""",(did,))
    if cur.fetchone()!={"privy_did":did}:
        raise HTTPException(409,"Original owner account unavailable")
    cur.execute("""UPDATE mininex_live_native_auto_sessions
        SET status='EXIT_PENDING',updated_at=now()
        WHERE privy_did=%s AND asset=%s AND started_by_owner_at IS NOT NULL
          AND status IN ('ACTIVE','PAUSED','EXIT_PENDING')
        RETURNING status""",(did,asset))
    row=cur.fetchone()
    if type(row) is not dict or row.get("status")!="EXIT_PENDING":
        raise HTTPException(409,"No owner-started native session to exit")
    return {"mode":"OWNER_NATIVE_LIVE_SESSION_CONTROL","asset":asset,
            "status":"EXIT_PENDING","new_transactions_sent":0}


def request_owner_native_exit(did,asset):
    did=_safe_did(did);asset=_asset(asset)
    if asset not in NATIVE_EXIT_ASSETS:
        raise HTTPException(409,"Automatic native exit route unavailable")
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _request_native_exit_locked(cur,did,asset)


def complete_native_exit_if_flat(did,asset,started,wallet,eth_wallet):
    """Called only by worker after verifying original Privy wallet addresses."""
    from .native_live_source_evidence import native_position_locked
    did=_safe_did(did)
    if asset not in NATIVE_EXIT_ASSETS:
        return False
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT privy_did FROM mininex_accounts
                WHERE privy_did=%s FOR UPDATE""",(did,))
            if cur.fetchone()!={"privy_did":did}:
                return False
            cur.execute("""SELECT status,wallet_address,started_by_owner_at
                FROM mininex_live_native_auto_sessions
                WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
            row=cur.fetchone()
            if (not row or row["status"]!="EXIT_PENDING"
                    or row["started_by_owner_at"]!=started
                    or row["wallet_address"].lower()!=eth_wallet.lower()):
                return False
            if native_position_locked(cur,did,asset,wallet,require_single_wallet=True)!=0:
                return False
            cur.execute("""UPDATE mininex_live_native_auto_sessions
                SET status='STOPPED',updated_at=now()
                WHERE privy_did=%s AND asset=%s AND status='EXIT_PENDING'
                RETURNING status""",(did,asset))
            saved=cur.fetchone()
            return bool(saved and saved["status"]=="STOPPED")
