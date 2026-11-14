"""Owner-controlled autonomous ETH session and actual background strategy worker.

Every ACTIVE session requires explicit authenticated start with verified Privy
delegation. Nothing runs by default; no PAPER sessions are inherited.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from decimal import Decimal
from time import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_user_grant import get_eth_user_grant_review
from .eth_live_strategy_tick import execute_live_eth_strategy_tick
from .storage import _db_url,_ensure_schema,_prepare_rows,_safe_did

LOG=logging.getLogger("mininex.eth_live_auto_sessions")
_INTERVAL=30


def _worker_approved():
    return (
        os.getenv("MININEX_ETH_AUTO_WORKER_ENABLED")=="explicit-live"
        and os.getenv("MININEX_LIVE_TRADING_ENABLED")=="true"
        and bool(os.getenv("DATABASE_URL",""))
    )


def _start_locked(cur,did,stable,grant):
    """Actual user-started activation with owner-approved verified policy."""
    if (grant.get("user_privy_grant_verified") is not True
            or grant.get("policy_verified") is not True
            or grant.get("wallet_owned_by_user") is not True):
        raise HTTPException(409,"Privy signer grant not verified")
    cur.execute(
        """SELECT trading_paused,revision,planned_capital_usd,max_order_usd,
                  max_daily_turnover_usd FROM mininex_live_eth_limits
           WHERE privy_did=%s FOR UPDATE""",(did,))
    limits=cur.fetchone()
    if (type(limits) is not dict or
            min(Decimal(str(limits["planned_capital_usd"])),
                Decimal(str(limits["max_order_usd"])),
                Decimal(str(limits["max_daily_turnover_usd"])))<Decimal("15")):
        raise HTTPException(409,"ETH AUTO live limits not configured")
    cur.execute(
        """SELECT status,policy_id,wallet_address,limits_revision,
                  remote_policy_sha256,candidate_sha256,
                  extract(epoch FROM policy_expires_at)::bigint AS expires
           FROM mininex_live_eth_policy_enrollments
           WHERE privy_did=%s FOR UPDATE""",(did,))
    policy=cur.fetchone()
    if (type(policy) is not dict or policy.get("status")!="GRANT_VERIFIED"
            or policy.get("policy_id")!=grant.get("policy_id")
            or str(policy.get("wallet_address","")).lower()!=str(grant.get(
                "verified_wallet_address","")).lower()
            or policy.get("limits_revision")!=limits["revision"]
            or policy.get("remote_policy_sha256")!=policy.get("candidate_sha256")
            or type(policy.get("expires")) is not int
            or policy["expires"]<int(time())+120):
        raise HTTPException(409,"Verified restricted wallet policy expired or changed")
    cur.execute(
        """SELECT status,revoked_at,limits_revision
           FROM mininex_live_eth_consent_intents
           WHERE privy_did=%s FOR UPDATE""",(did,))
    consent=cur.fetchone()
    if (type(consent) is not dict or consent.get("status")!="SIGNED_INTENT"
            or consent.get("revoked_at") is not None
            or consent.get("limits_revision")!=limits["revision"]):
        raise HTTPException(409,"User live consent missing or revoked")
    cur.execute(
        """SELECT COUNT(*) AS pending FROM mininex_live_order_intents
           WHERE privy_did=%s AND asset='ETH' AND state IN
           ('RESERVED','SENDING','RECONCILIATION_REQUIRED',
            'SUBMITTED','REORG_PENDING')""",(did,))
    pending=cur.fetchone()
    if type(pending) is not dict or pending["pending"]!=0:
        raise HTTPException(409,"Previous ETH trade must settle before AUTO starts")
    run_id="eth_auto_"+secrets.token_hex(16)
    cur.execute(
        """INSERT INTO mininex_live_eth_auto_sessions
           (privy_did,run_id,stable,status)
           VALUES (%s,%s,%s,'ACTIVE')
           ON CONFLICT(privy_did) DO UPDATE
           SET run_id=EXCLUDED.run_id,stable=EXCLUDED.stable,
               status='ACTIVE',updated_at=now()""",
        (did,run_id,stable))
    cur.execute(
        """UPDATE mininex_live_eth_limits
           SET trading_paused=FALSE,updated_at=now()
           WHERE privy_did=%s""",(did,))
    return {"asset":"ETH","run_id":run_id,"stable":stable,
            "status":"ACTIVE","live_limits_revision":limits["revision"]}


async def start_eth_auto(did:str,stable:str)->dict:
    did=_safe_did(did)
    if stable not in ("USDC","USDT"):
        raise HTTPException(422,"Unsupported settlement token")
    if not _worker_approved():
        raise HTTPException(503,"ETH AUTO worker not deployed and enabled")
    # Re-verify actual wallet owner, attached signers and full remote Privy
    # policy BEFORE releasing trade controls.
    grant=await get_eth_user_grant_review(did)
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                return _start_locked(cur,did,stable,grant)
    except psycopg.Error:
        raise HTTPException(503,"ETH AUTO start unavailable") from None


def _pause_locked(cur,did):
    cur.execute(
        """SELECT trading_paused FROM mininex_live_eth_limits
           WHERE privy_did=%s FOR UPDATE""",(did,))
    row=cur.fetchone()
    if type(row) is not dict:
        raise HTTPException(503,"ETH LIVE switch unavailable")
    # Stop future sends immediately. An already submitted transaction may
    # still finalize; it remains in the durable live intent ledger.
    cur.execute(
        """UPDATE mininex_live_eth_limits
           SET trading_paused=TRUE,updated_at=now()
           WHERE privy_did=%s""",(did,))
    cur.execute(
        """UPDATE mininex_live_eth_auto_sessions
           SET status='PAUSED',updated_at=now()
           WHERE privy_did=%s""",(did,))
    return {"asset":"ETH","status":"PAUSED",
            "future_auto_orders_blocked":True,
            "onchain_pending_orders_not_cancelled":True}


def pause_eth_auto(did:str)->dict:
    did=_safe_did(did)
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                return _pause_locked(cur,did)
    except psycopg.Error:
        raise HTTPException(503,"ETH AUTO pause unavailable") from None


def get_eth_auto_session(did:str)->dict:
    did=_safe_did(did)
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                cur.execute(
                    """SELECT s.run_id,s.stable,s.status,l.trading_paused
                       FROM mininex_live_eth_auto_sessions s
                       JOIN mininex_live_eth_limits l ON l.privy_did=s.privy_did
                       WHERE s.privy_did=%s""",(did,))
                row=cur.fetchone()
        return {"asset":"ETH","status":row["status"] if row else "OFF",
                "run_id":row["run_id"] if row else None,
                "stable":row["stable"] if row else "USDC",
                "trading_paused":row["trading_paused"] if row else True,
                "worker_deployed":_worker_approved()}
    except psycopg.Error:
        raise HTTPException(503,"ETH AUTO state unavailable") from None


def active_sessions(limit:int=10)->list[dict]:
    if not _worker_approved():
        return []
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT s.privy_did,s.run_id,s.stable
                       FROM mininex_live_eth_auto_sessions s
                       JOIN mininex_live_eth_limits l ON l.privy_did=s.privy_did
                       JOIN mininex_live_eth_policy_enrollments p
                         ON p.privy_did=s.privy_did
                       WHERE s.status='ACTIVE' AND l.trading_paused=FALSE
                         AND p.status='GRANT_VERIFIED'
                         AND p.limits_revision=l.revision
                         AND p.policy_expires_at>now()+interval '120 seconds'
                       ORDER BY s.updated_at ASC LIMIT %s""",(max(1,min(20,limit)),))
                return cur.fetchall()
    except psycopg.Error:
        LOG.exception("ETH LIVE session scan failed")
        return []


class EthAutoLiveWorker:
    def __init__(self,market):
        self.market=market
        self._task=None

    async def start(self):
        if _worker_approved() and self._task is None:
            self._task=asyncio.create_task(self._run(),name="mininex-eth-auto-live")

    async def stop(self):
        task,self._task=self._task,None
        if task is not None:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def _run(self):
        while True:
            await asyncio.sleep(_INTERVAL)
            if not _worker_approved():
                continue
            sessions=await asyncio.to_thread(active_sessions)
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12),follow_redirects=False) as client:
                for row in sessions:
                    if not _worker_approved(): break
                    try:
                        await execute_live_eth_strategy_tick(
                            row["privy_did"],market=self.market,
                            run_id=row["run_id"],stable=row["stable"],
                            client=client)
                    except asyncio.CancelledError:raise
                    except Exception:
                        LOG.exception("ETH LIVE AUTO tick failed for an account")
