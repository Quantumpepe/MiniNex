"""Passive owner-token historical price capture and exact 5% HWM worker.

No PAPER prices, no client-supplied net, no automatic transaction sender.
The worker stores prices near original finalized LINK/UNI BUY/SELL blocks,
then runs whole-custody FIFO proof before recording realized SELL deltas
on the existing idempotent High-Water-Mark accounting ledger. User fee
payment/signing remains a completely separate permission and action.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_token_net_reconciliation import (
    _verified_account_fill,reconcile_owner_erc20_hwm_after_verified_sell,
)
from .shared_evm_historical_fill_bridge import independently_price_stored_evm_fill
from .storage import _db_url,_ensure_schema

LOG=logging.getLogger("mininex.evm_token_fee_reconciliation")


def _read_pending_original_usd_fills(limit:int=40)->list[dict]:
    """Work page only; never a per-trade/per-day artificial system cap."""
    if type(limit) is not int or not 1<=limit<=500:
        raise ValueError("Invalid temporary audit batch size")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT f.privy_did,f.asset,f.chain_id,f.intent_key,
                          f.tx_hash,f.wallet_address,f.side,f.stable,
                          f.source_amount,f.destination_amount,
                          f.network_fee_native,f.created_at,
                          b.status AS broadcast_status,
                          b.tx_hash AS broadcast_tx_hash,
                          b.side AS broadcast_side,
                          b.stable AS broadcast_stable,
                          b.wallet_address AS broadcast_wallet_address
                   FROM mininex_live_evm_verified_fills f
                   JOIN mininex_live_evm_auto_broadcasts b
                     ON b.privy_did=f.privy_did AND b.asset=f.asset
                    AND b.intent_key=f.intent_key
                   LEFT JOIN mininex_evm_immutable_fill_usd_prices s
                     ON s.privy_did=f.privy_did AND s.asset=f.asset
                    AND s.tx_hash=f.tx_hash AND s.quoted_asset=f.stable
                   LEFT JOIN mininex_evm_immutable_fill_usd_prices n
                     ON n.privy_did=f.privy_did AND n.asset=f.asset
                    AND n.tx_hash=f.tx_hash AND n.quoted_asset='ETH'
                   WHERE f.asset IN ('LINK','UNI')
                     AND f.created_at >= now() - INTERVAL '20 hours'
                     AND b.status='FINALIZED_SUCCESS'
                     AND (s.tx_hash IS NULL OR n.tx_hash IS NULL)
                   ORDER BY f.created_at ASC,f.privy_did ASC
                   LIMIT %s""",(limit,))
            rows=list(cur.fetchall())
    return [dict(did=row["privy_did"],
                 fill=_verified_account_fill(row,row["asset"])) for row in rows]


def _read_unaccounted_token_sell_owners(limit:int=20)->list[dict]:
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT f.privy_did,f.asset
                   FROM mininex_live_evm_verified_fills f
                   JOIN mininex_live_evm_auto_broadcasts b
                     ON b.privy_did=f.privy_did AND b.asset=f.asset
                    AND b.intent_key=f.intent_key
                   LEFT JOIN mininex_evm_fee_events e
                     ON e.privy_did=f.privy_did AND e.asset=f.asset
                    AND e.execution_id=f.intent_key
                   WHERE f.asset IN ('LINK','UNI')
                     AND f.side='SELL' AND b.status='FINALIZED_SUCCESS'
                     AND e.execution_id IS NULL
                   GROUP BY f.privy_did,f.asset
                   ORDER BY MIN(f.created_at) ASC
                   LIMIT %s""",(limit,))
            return list(cur.fetchall())


async def reconcile_verified_erc20_fees_once(
    *,client:httpx.AsyncClient,
)->dict:
    """Keep trading fast: this does not touch the wallet send loop."""
    if type(client) is not httpx.AsyncClient:
        raise ValueError("Original internal HTTP client required")
    result={"prices_recorded":0,"token_accounts_reconciled":0,
            "accounts_needing_manual_evidence":0,
            "wallet_transactions_sent":0}
    try:
        pending=await asyncio.to_thread(_read_pending_original_usd_fills)
    except psycopg.Error:
        LOG.warning("Historical real owner fill scan unavailable")
        return result
    for item in pending:
        try:
            proof=await independently_price_stored_evm_fill(
                did=item["did"],row=item["fill"],client=client,
                durable_rates=True)
            if proof.get("historic_usd_priced") is True:
                result["prices_recorded"]+=1
        except asyncio.CancelledError:
            raise
        except (ValueError,HTTPException,psycopg.Error):
            result["accounts_needing_manual_evidence"]+=1
    try:
        owners=await asyncio.to_thread(_read_unaccounted_token_sell_owners)
    except psycopg.Error:
        LOG.warning("Unaccounted finalized EVM SELL scan unavailable")
        return result
    for owner in owners:
        try:
            await reconcile_owner_erc20_hwm_after_verified_sell(
                did=owner["privy_did"],asset=owner["asset"],client=client)
            result["token_accounts_reconciled"]+=1
        except asyncio.CancelledError:
            raise
        except (ValueError,HTTPException,psycopg.Error):
            result["accounts_needing_manual_evidence"]+=1
    return result
