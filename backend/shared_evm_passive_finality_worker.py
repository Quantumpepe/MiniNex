"""Passive original Router02 EVM finality for all six additional MiniNex EVM coins.

A successfully submitted real wallet transaction must become a finalized
durable fill without requiring a new market signal or a user-active session.
Only the exact original known transaction hash is polled; ambiguous unknown
SENDING/RECONCILIATION_REQUIRED records are NEVER rebroadcast or released.
"""
from __future__ import annotations

import asyncio
import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_order_finality import reconcile_evm_internal_order
from .storage import _db_url,_ensure_schema

LOG=logging.getLogger("mininex.evm_passive_order_finality")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI","ARB"))


def original_submitted_evm_orders(limit:int=80)->list[dict]:
    """Bounded RPC work page only; never a trading-order or daily limit."""
    if type(limit) is not int or not 1<=limit<=500:
        raise ValueError("Invalid finality scan batch")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT privy_did,asset,chain_id,intent_key,tx_hash,
                          stable,side,wallet_address
                   FROM mininex_live_evm_auto_broadcasts
                   WHERE status='SUBMITTED'
                     AND tx_hash IS NOT NULL
                     AND asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')
                   ORDER BY created_at ASC,privy_did,asset LIMIT %s""",
                (limit,))
            return list(cur.fetchall())


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
            or not re.fullmatch(r"0x[0-9a-f]{40}",row["wallet_address"])):
        raise ValueError("Unverified persisted original EVM submission")
    return row


async def reconcile_submitted_evm_swaps_once()->dict:
    """Only read chain receipts, finalize exact previously submitted orders."""
    results={"submitted_checked":0,"finalized_swaps":0,
             "awaiting_finality":0,"unresolved":0,
             "wallet_transactions_sent":0}
    try:
        rows=await asyncio.to_thread(original_submitted_evm_orders)
    except psycopg.Error:
        LOG.warning("Original submitted EVM scan unavailable")
        return results
    for raw in rows:
        try:
            row=validate_original_submitted_swap_record(raw)
            status=await reconcile_evm_internal_order(
                did=row["privy_did"],asset=row["asset"],
                intent_key=row["intent_key"],tx_hash=row["tx_hash"],
                side=row["side"],stable=row["stable"])
            results["submitted_checked"]+=1
            if status.get("state")=="AWAITING_FINALITY":
                results["awaiting_finality"]+=1
            elif status.get("state") in ("FINALIZED_SUCCESS","FINALIZED_REVERTED"):
                results["finalized_swaps"]+=1
            else:
                results["unresolved"]+=1
        except asyncio.CancelledError:
            raise
        except (HTTPException,ValueError,psycopg.Error):
            # Never try another wallet broadcast to repair finality.
            results["unresolved"]+=1
    return results
