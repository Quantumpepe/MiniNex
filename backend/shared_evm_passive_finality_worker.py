"""Passive original Router02 EVM finality for all six additional MiniNex EVM coins.

A successfully submitted real wallet transaction must become a finalized
durable fill without requiring a new market signal or a user-active session.
Only the exact original known transaction hash is polled; ambiguous unknown
SENDING/RECONCILIATION_REQUIRED records are NEVER rebroadcast or released.
"""
from __future__ import annotations

import asyncio
import logging
from threading import Lock

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_order_finality import reconcile_evm_internal_order
from .storage import _db_url,_ensure_schema

LOG=logging.getLogger("mininex.evm_passive_order_finality")
_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"))
_FINALITY_PAGE_LOCK=Lock()
_FINALITY_PAGE_OFFSET=0


def original_submitted_evm_orders(limit:int=80)->list[dict]:
    """Bounded RPC work page only; never a trading-order or daily limit."""
    if type(limit) is not int or not 1<=limit<=500:
        raise ValueError("Invalid finality scan batch")
    global _FINALITY_PAGE_OFFSET
    db=_db_url()
    _ensure_schema(db)
    sql="""SELECT privy_did,asset,chain_id,intent_key,tx_hash,
                          stable,side,wallet_address
                   FROM mininex_live_evm_auto_broadcasts
                   WHERE status='SUBMITTED'
                     AND tx_hash IS NOT NULL
                     AND asset IN ('ETH','BNB','POL','AVAX','LINK','QNT','UNI','ARB')
                   ORDER BY created_at ASC,privy_did,asset,intent_key
                   LIMIT %s OFFSET %s"""
    # A long-pending receipt cannot prevent later completed transactions
    # from being checked. This only paginates chain reads; it never resends.
    with _FINALITY_PAGE_LOCK:
        offset=_FINALITY_PAGE_OFFSET
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,offset))
                rows=list(cur.fetchall())
                if not rows and offset:
                    offset=0
                    cur.execute(sql,(limit,0))
                    rows=list(cur.fetchall())
        _FINALITY_PAGE_OFFSET=offset+len(rows)
    return rows


def validate_original_submitted_swap_record(row:object)->dict:
    """An original DB row, not a UI-requested arbitrary hash."""
    from .shared_evm_swap_receipts import _pair
    if type(row) is not dict or row.get("asset") not in _ASSETS:
        raise ValueError("Noncanonical EVM submitted order")
    asset,stable,side=row["asset"],row.get("stable"),row.get("side")
    route=_pair(asset,stable,side)
    import re
    if (row.get("chain_id")!=route["chain_id"]
            or type(row.get("privy_did")) is not str
            or not row["privy_did"].startswith("did:privy:")
            or type(row.get("intent_key")) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,96}",row["intent_key"])
            or type(row.get("tx_hash")) is not str
            or not re.fullmatch(r"0x[0-9a-f]{64}",row["tx_hash"])
            or type(row.get("wallet_address")) is not str
            or not re.fullmatch(r"0x[0-9a-fA-F]{40}",row["wallet_address"])):
        raise ValueError("Unverified persisted original EVM submission")
    return row


async def reconcile_submitted_evm_swaps_once()->dict:
    """Finalize original hashes concurrently across independent wallet chains.

    One owner's same-chain fills retain original order; different wallets
    should not wait behind an unmined receipt or slow public RPC. This is
    receipt-only reconciliation, never a new Privy transaction submission.
    """
    results={"submitted_checked":0,"finalized_swaps":0,
             "awaiting_finality":0,"unresolved":0,
             "wallet_transactions_sent":0}
    try:
        rows=await asyncio.to_thread(original_submitted_evm_orders)
    except psycopg.Error:
        LOG.warning("Original submitted EVM scan unavailable")
        return results
    groups:dict[tuple[str,int],list[dict]]={}
    for raw in rows:
        try:
            row=validate_original_submitted_swap_record(raw)
            groups.setdefault(
                (row["privy_did"],row["chain_id"]),[]
            ).append(row)
        except (ValueError,TypeError,KeyError):
            results["unresolved"]+=1
    async def one_wallet_chain(items:list[dict])->dict:
        count={"submitted_checked":0,"finalized_swaps":0,
               "awaiting_finality":0,"unresolved":0}
        for row in items:
            try:
                status=await reconcile_evm_internal_order(
                    did=row["privy_did"],asset=row["asset"],
                    intent_key=row["intent_key"],tx_hash=row["tx_hash"],
                    side=row["side"],stable=row["stable"])
                count["submitted_checked"]+=1
                if status.get("state")=="AWAITING_FINALITY":
                    count["awaiting_finality"]+=1
                elif status.get("state") in (
                        "FINALIZED_SUCCESS","FINALIZED_REVERTED"):
                    count["finalized_swaps"]+=1
                else:
                    count["unresolved"]+=1
            except asyncio.CancelledError:
                raise
            except Exception:
                # Reinspect this original hash later; never send again.
                count["unresolved"]+=1
                LOG.exception("Original owner EVM receipt not yet reconciled")
        return count
    completed=await asyncio.gather(*(
        one_wallet_chain(items) for items in groups.values()
    ))
    for part in completed:
        for key,value in part.items():
            results[key]+=value
    return results
