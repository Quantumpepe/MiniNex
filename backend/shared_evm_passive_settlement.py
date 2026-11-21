"""Passive, chain-scoped settlement worker for six additional EVM assets.

This worker NEVER sends or signs a transaction, creates a policy, activates
AUTO, or assesses a fee. It automatically accompanies an explicitly enabled
LIVE EVM trader, without a second operator switch. It only reconciles durably SUBMITTED
wallet transactions against independently verified finalized blockchain
receipts. Ambiguous SENDING/RECONCILIATION_REQUIRED states are untouched.
"""
from __future__ import annotations

import asyncio
import logging
import os
from threading import Lock

import psycopg
from psycopg.rows import dict_row

from .shared_evm_order_finality import reconcile_evm_internal_order
from .storage import _db_url, _ensure_schema

LOG=logging.getLogger("mininex.evm_passive_settlement")
_INTERVAL_SECONDS=30
_SUPPORTED=frozenset(("BNB","POL","AVAX","LINK","UNI","ARB"))
_PASSIVE_PAGE_LOCK=Lock()
_PASSIVE_PAGE_OFFSET=0


def _live_worker_configured()->bool:
    """Compatibility API: not an operator-controlled LIVE permission."""
    return bool(os.getenv("DATABASE_URL","").strip())


def _settlement_enabled()->bool:
    """Reconcile every submitted original receipt whenever DB is available.

    The passive receipt observer sends no transactions and must NEVER be
    stopped by a missing operator switch or an owner's paused session.
    """
    return bool(os.getenv("DATABASE_URL","").strip())


def _pending_submitted(limit:int=25)->list[dict]:
    """Rotate every submitted owner hash, also in recovery-only mode.

    Never rebroadcast; this is a work page, not an owner trade cap.
    """
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Invalid settlement scan limit")
    if not _settlement_enabled():
        return []
    global _PASSIVE_PAGE_OFFSET
    url=_db_url()
    _ensure_schema(url)
    sql="""SELECT privy_did,asset,intent_key,tx_hash,side,stable,chain_id
           FROM mininex_live_evm_auto_broadcasts
           WHERE status='SUBMITTED' AND tx_hash IS NOT NULL
             AND asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')
           ORDER BY created_at ASC,privy_did,asset,intent_key
           LIMIT %s OFFSET %s"""
    with _PASSIVE_PAGE_LOCK:
        offset=_PASSIVE_PAGE_OFFSET
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,offset))
                page=list(cur.fetchall())
                if not page and offset:
                    offset=0
                    cur.execute(sql,(limit,0))
                    page=list(cur.fetchall())
        _PASSIVE_PAGE_OFFSET=offset+len(page)
        return page


def _valid_pending_row(row:object)->bool:
    if type(row) is not dict or row.get("asset") not in _SUPPORTED:
        return False
    from .shared_evm_swap_receipts import _pair
    try:
        pair=_pair(row["asset"],row["stable"],row["side"])
    except (KeyError,TypeError,ValueError):
        return False
    return (
        row.get("chain_id")==pair["chain_id"]
        and type(row.get("tx_hash")) is str
        and len(row["tx_hash"])==66
        and row["tx_hash"].startswith("0x")
        and type(row.get("intent_key")) is str
        and len(row["intent_key"])>=8
        and type(row.get("privy_did")) is str
        and row["privy_did"].startswith("did:privy:")
    )


async def reconcile_submitted_evm_once(*,limit:int=25)->dict:
    """Observe submitted sends; never retry or accelerate an old broadcast."""
    if not _settlement_enabled():
        return {"worker_enabled":False,"scanned":0,
                "finalized":0,"pending":0,"errors":0,
                "new_transactions_sent":0}
    try:
        rows=await asyncio.to_thread(_pending_submitted,limit)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.exception("Read-only EVM pending order scan failed")
        return {"worker_enabled":True,"scanned":0,
                "finalized":0,"pending":0,"errors":1,
                "new_transactions_sent":0}
    result={"worker_enabled":True,"scanned":len(rows),
            "finalized":0,"pending":0,"errors":0,
            "new_transactions_sent":0}
    for row in rows:
        if not _valid_pending_row(row):
            result["errors"]+=1
            LOG.error("Rejected invalid EVM journal pending row")
            continue
        try:
            observed=await reconcile_evm_internal_order(
                did=row["privy_did"],asset=row["asset"],
                intent_key=row["intent_key"],tx_hash=row["tx_hash"],
                side=row["side"],stable=row["stable"])
            if observed.get("state") in ("FINALIZED_SUCCESS","FINALIZED_REVERTED"):
                result["finalized"]+=1
            elif observed.get("state")=="AWAITING_FINALITY":
                result["pending"]+=1
            else:
                result["errors"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("EVM submitted order unresolved; will inspect original hash")
            result["errors"]+=1
    return result


class PassiveEvmSettlementWorker:
    """Standalone read-only worker; LIVE worker already polls these receipts."""
    def __init__(self):
        self._task=None

    async def start(self)->None:
        if (_settlement_enabled() and not _live_worker_configured()
                and self._task is None):
            self._task=asyncio.create_task(
                self._run(),name="mininex-evm-passive-settlement")

    async def stop(self)->None:
        task,self._task=self._task,None
        if task is not None:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def _run(self)->None:
        while True:
            await asyncio.sleep(_INTERVAL_SECONDS)
            # Avoid a duplicate network/SQL finality scan when LIVE worker
            # already calls reconcile_submitted_evm_swaps_once itself.
            if _settlement_enabled() and not _live_worker_configured():
                await reconcile_submitted_evm_once()
