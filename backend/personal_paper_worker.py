"""Bounded, fail-closed, durable per-account simulation scheduler.

The API process ticks existing ACTIVE sessions and only pending liquidation
of already PAUSED sessions. PostgreSQL row locks serialize evaluations across
workers/restarts; no session auto-starts or new entries while paused.
Paper events stay outside the verified real-activity/tax ledger.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .storage import _db_url, _ensure_schema
from .user_paper import step_user_paper
from .extra_paper import tick as tick_extra_paper

LOG=logging.getLogger("mininex.personal_paper_worker")
INTERVAL_SECONDS=15
BATCH_LIMIT=30
EXTRA_BATCH_LIMIT=24


def auto_worker_allowed() -> bool:
    return (
        bool(os.environ.get("DATABASE_URL","").strip())
        and os.environ.get("MININEX_PAPER_AUTO_ENABLED","true").lower()=="true"
        and os.environ.get("MININEX_LIVE_TRADING_ENABLED","false").lower()!="true"
    )


def active_session_keys(limit: int = BATCH_LIMIT) -> list[tuple[str,str]]:
    if not auto_worker_allowed():
        return []
    url=_db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # Oldest processed sessions first => fairness for multiple users.
            cur.execute("""SELECT privy_did,asset
                FROM mininex_user_paper_sessions
                WHERE status='ACTIVE'
                   OR (status='PAUSED' AND state_json->>'pause_exit_pending'='true')
                ORDER BY updated_at ASC,privy_did,asset
                LIMIT %s""",(min(max(1,limit),BATCH_LIMIT),))
            return [(row["privy_did"],row["asset"]) for row in cur.fetchall()]


def process_active_sessions(market: Any) -> int:
    """Only one bounded scan; exceptions do not authorize a fallback fill."""
    if not auto_worker_allowed():
        return 0
    evaluated=0
    for did,asset in active_session_keys():
        # Check again if deployment switched mode during a batch.
        if not auto_worker_allowed():
            break
        try:
            step_user_paper(did,asset,market,time.time())
            evaluated+=1
        except Exception:
            # A failing session cannot stop other users or trigger real orders.
            LOG.exception("Personal paper tick failed for %s",asset)
    return evaluated



def active_extra_keys(limit: int = EXTRA_BATCH_LIMIT) -> list[tuple[str,str]]:
    """Fair, bounded scans only for explicitly started extra-coin PAPER rows."""
    if not auto_worker_allowed():
        return []
    url=_db_url();_ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT privy_did,asset
                FROM mininex_extra_paper_sessions
                WHERE status='ACTIVE'
                   OR (status='PAUSED' AND state_json->>'pause_exit_pending'='true')
                ORDER BY updated_at ASC,privy_did,asset
                LIMIT %s""",(min(max(1,limit),EXTRA_BATCH_LIMIT),))
            return [(row["privy_did"],row["asset"]) for row in cur.fetchall()]


def process_extra_sessions(source: Any) -> int:
    """Reuse the strategy per coin, but never start accounts automatically."""
    if not auto_worker_allowed():
        return 0
    processed=0
    for did,asset in active_extra_keys():
        if not auto_worker_allowed():
            break
        try:
            tick_extra_paper(did,asset,source,time.time())
            processed+=1
        except Exception:
            LOG.exception("Extra-coin PAPER tick failed for %s",asset)
    return processed


class PersonalPaperWorker:
    def __init__(self,market: Any,extra_market: Any | None = None):
        self.market=market
        self.extra_market=extra_market
        self._task:asyncio.Task | None=None

    async def start(self):
        if self._task is None and auto_worker_allowed():
            self._task=asyncio.create_task(self._loop(),name="mininex-personal-paper")

    async def stop(self):
        task,self._task=self._task,None
        if task is not None:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def _loop(self):
        while True:
            await asyncio.sleep(INTERVAL_SECONDS)
            if not auto_worker_allowed():
                continue
            try:
                # Never block the API event loop with PostgreSQL or signal work.
                await asyncio.to_thread(process_active_sessions,self.market)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("BTC/ETH paper scheduler cycle failed")
            if self.extra_market is not None:
                try:
                    # Extra coins use their OWN rows and cannot stop BTC/ETH.
                    await asyncio.to_thread(process_extra_sessions,self.extra_market)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Extra-coin paper scheduler cycle failed")
