"""Opt-in native AUTO LIVE worker for five original Privy-owned coins.

This runtime uses the same actual market Strategist and original staged
THORChain source executor. It uses the existing database and only the
owner's ACTIVE trade session plus a real independent current Privy grant
auditor. There are no extra operator-side LIVE-enable switches.
Merely signing in or editing a local status cannot grant spending power.

A once-submitted Ethereum source has its own passive finality regardless of
whether the user later pauses this live worker.
"""
from __future__ import annotations

import asyncio
import logging
import os
from threading import Lock

import httpx
import psycopg
from psycopg.rows import dict_row

from .native_live_auto_tick import (
    OriginalNativeMarket,run_owner_native_live_batch,
)
from .storage import _db_url,_ensure_schema

LOG=logging.getLogger("mininex.native_auto_live")
_PAGE_LOCK=Lock()
_PAGE_OFFSET=0
_INTERVAL_SECONDS=30


def native_auto_worker_enabled()->bool:
    """No additional LIVE switches: only the existing DB is needed.

    Actual source sends still need owner-started ACTIVE sessions, unchanged
    coin settings and fresh independently audited Privy spending consent.
    """
    return bool(os.getenv("DATABASE_URL","").strip())


def _native_worker_heartbeat()->None:
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """INSERT INTO mininex_live_worker_heartbeats
                       (worker_name,last_poll_at,status)
                   VALUES ('native_auto',now(),'RUNNING')
                   ON CONFLICT(worker_name) DO UPDATE
                   SET last_poll_at=now(),status='RUNNING'""")


def owner_started_native_sessions(limit:int=30)->list[dict]:
    """Owner ACTIVE sessions only, still rechecking real Privy on each tick.

    BTC's existing budget percent is read from the BTC preference; the
    other four use their independent USD budgets and exact stable.
    The rotating page does not limit a user's number of trades.
    """
    if not native_auto_worker_enabled():
        return []
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Invalid original owner native session work page")
    global _PAGE_OFFSET
    db=_db_url();_ensure_schema(db)
    sql="""SELECT s.privy_did,s.asset,s.stable,s.budget_revision,
                  s.status,s.started_by_owner_at,
                  COALESCE(e.mode,t.mode) AS mode,
                  COALESCE(e.horizon,t.horizon) AS horizon,
                  e.budget_usd,b.percent AS budget_percent
           FROM mininex_live_native_auto_sessions s
           LEFT JOIN mininex_extra_coin_preferences e
             ON s.asset!='BTC'
              AND e.privy_did=s.privy_did AND e.asset=s.asset
           LEFT JOIN mininex_budget_preferences b
             ON s.asset='BTC'
              AND b.privy_did=s.privy_did AND b.asset='BTC'
           LEFT JOIN mininex_trade_preferences t
             ON s.asset='BTC'
              AND t.privy_did=s.privy_did AND t.asset='BTC'
           WHERE (s.status='ACTIVE' OR (s.status='EXIT_PENDING' AND s.asset IN ('BTC','SOL')))
             AND s.started_by_owner_at IS NOT NULL
             AND (
               (s.asset='BTC' AND b.revision=s.budget_revision
                AND b.percent>0 AND t.stable=s.stable)
               OR (s.asset!='BTC' AND e.revision=s.budget_revision
                   AND e.budget_usd>0 AND e.stable=s.stable)
             )
           ORDER BY s.updated_at,s.privy_did,s.asset
           LIMIT %s OFFSET %s"""
    with _PAGE_LOCK:
        offset=_PAGE_OFFSET
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,offset))
                rows=list(cur.fetchall())
                if not rows and offset:
                    offset=0
                    cur.execute(sql,(limit,offset))
                    rows=list(cur.fetchall())
        _PAGE_OFFSET=offset+len(rows)
    return rows


class NativeLiveWorker:
    """No auditor means NO worker; last-stage Privy policy is not assumed."""
    def __init__(self,btc_market,extra_market,*,grant_auditor=None):
        self.market=OriginalNativeMarket(btc_market,extra_market)
        self.grant_auditor=grant_auditor
        self._task=None

    async def start(self):
        if (self._task is None and native_auto_worker_enabled()
                and callable(self.grant_auditor)):
            self._task=asyncio.create_task(
                self._run(),name="mininex-native-owner-auto-live")

    async def stop(self):
        task,self._task=self._task,None
        if task is not None:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def _run(self):
        while True:
            await asyncio.sleep(_INTERVAL_SECONDS)
            if (not native_auto_worker_enabled()
                    or not callable(self.grant_auditor)):
                continue
            try:
                await asyncio.to_thread(_native_worker_heartbeat)
                owners=await asyncio.to_thread(owner_started_native_sessions)
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12),follow_redirects=False,
                ) as client:
                    await run_owner_native_live_batch(
                        owners,market=self.market,client=client,
                        grant_auditor=self.grant_auditor)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Original native LIVE owner tick unavailable")
