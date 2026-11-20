"""Automatic fees are sent only for original HWM debts after owner Privy grant.

One pending original fee, one owner wallet, one restricted chain/token policy;
nothing is derived from PAPER/market quotes as a profit claim. This worker
does not retry uncertainty and does not impose trade/day/capital limits.
Actual fee amounts come from 5% of independently verified net gains.
"""
from __future__ import annotations

import asyncio
import logging
from threading import Lock

import httpx
import psycopg
from psycopg.rows import dict_row

from .shared_evm_auto_fee_submit import (
    automatic_fee_transfer_worker_enabled,submit_original_owner_auto_fee_once,
)
from .shared_evm_auto_fee_finality import reconcile_auto_fee_receipts_once
from .storage import _db_url,_ensure_schema

LOG=logging.getLogger("mininex.evm_verified_auto_fees")

# A database work-page cursor, not a trading limit or retry authorization.
# Every send must still pass the original atomic one-shot claim.
_PENDING_FEE_SCAN_LOCK=Lock()
_PENDING_FEE_SCAN_OFFSET=0


def _pending_owner_auto_fee_obligations(limit:int=25)->list[dict]:
    """Work page, never arbitrary per-owner/day fee or trading throttle."""
    if type(limit) is not int or not 1<=limit<=200:
        raise ValueError("Invalid transient fee processing batch")
    if not automatic_fee_transfer_worker_enabled():
        return []
    global _PENDING_FEE_SCAN_OFFSET
    db=_db_url();_ensure_schema(db)
    query="""SELECT o.privy_did,o.asset,o.execution_id
                   FROM mininex_evm_fee_obligations o
                   JOIN mininex_evm_fee_events e
                     ON e.privy_did=o.privy_did AND e.asset=o.asset
                    AND e.execution_id=o.execution_id
                   JOIN mininex_live_evm_auto_broadcasts b
                     ON b.privy_did=o.privy_did AND b.asset=o.asset
                    AND b.intent_key=o.execution_id
                   JOIN mininex_evm_fee_auto_permissions g
                     ON g.privy_did=o.privy_did AND g.asset=o.asset
                   LEFT JOIN mininex_evm_verified_fee_payments p
                     ON p.privy_did=o.privy_did AND p.asset=o.asset
                    AND p.execution_id=o.execution_id
                   LEFT JOIN mininex_evm_auto_fee_sends a
                     ON a.privy_did=o.privy_did AND a.asset=o.asset
                    AND a.execution_id=o.execution_id
                   WHERE o.settlement_status='AWAITING_VERIFIED_TRANSFER'
                     AND o.settlement_tx_hash IS NULL
                     AND o.fee_due_usd=e.fee_assessed_usd
                     AND o.fee_due_usd>0
                     AND b.status='FINALIZED_SUCCESS' AND b.side='SELL'
                     AND e.source_tx_hash=b.tx_hash
                     AND b.wallet_address=g.wallet_address
                     AND b.stable=g.stable AND b.chain_id=g.chain_id
                     AND g.status='GRANT_VERIFIED'
                     AND g.grant_verified_at IS NOT NULL
                     AND g.owner_signed_at IS NOT NULL
                     AND g.revoked_at IS NULL
                     AND g.expires_at>now()
                     AND g.policy_id IS NOT NULL
                     AND g.remote_policy_sha256=g.policy_sha256
                     AND p.execution_id IS NULL AND a.execution_id IS NULL
                   ORDER BY o.created_at ASC,o.privy_did,o.asset,o.execution_id
                   LIMIT %s OFFSET %s"""
    # Missing remote Privy grants or unavailable networks can keep a row
    # pending. Cycle through all eligible owners instead of retrying only
    # the oldest page forever; the permanent SENDING journal still forbids
    # another wallet transaction for an already-claimed original fee.
    with _PENDING_FEE_SCAN_LOCK:
        offset=_PENDING_FEE_SCAN_OFFSET
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query,(limit,offset))
                rows=list(cur.fetchall())
                if not rows and offset:
                    offset=0
                    cur.execute(query,(limit,0))
                    rows=list(cur.fetchall())
        _PENDING_FEE_SCAN_OFFSET=offset+len(rows)
    return rows


_COMBINED_FEE_PAGE_OFFSET=0


def _pending_combined_grant_fee_obligations(limit:int=25)->list[dict]:
    """Original one-signature trading grants also permit exact HWM fees.

    Exclude ANY separate fee enrollment: its revocation must never be
    bypassed by falling back to the trading grant.
    """
    global _COMBINED_FEE_PAGE_OFFSET
    if not automatic_fee_transfer_worker_enabled():
        return []
    if type(limit) is not int or not 1<=limit<=200:
        raise ValueError("Invalid transient combined-fee processing page")
    db=_db_url();_ensure_schema(db)
    sql="""SELECT o.privy_did,o.asset,o.execution_id
              FROM mininex_evm_fee_obligations o
              JOIN mininex_evm_fee_events e
                ON e.privy_did=o.privy_did AND e.asset=o.asset
               AND e.execution_id=o.execution_id
              JOIN mininex_live_evm_auto_broadcasts b
                ON b.privy_did=o.privy_did AND b.asset=o.asset
               AND b.intent_key=o.execution_id
              JOIN mininex_live_evm_owner_permissions g
                ON g.privy_did=o.privy_did AND g.asset=o.asset
              LEFT JOIN mininex_evm_fee_auto_permissions separate
                ON separate.privy_did=o.privy_did
               AND separate.asset=o.asset
              LEFT JOIN mininex_evm_verified_fee_payments p
                ON p.privy_did=o.privy_did AND p.asset=o.asset
               AND p.execution_id=o.execution_id
              LEFT JOIN mininex_evm_auto_fee_sends a
                ON a.privy_did=o.privy_did AND a.asset=o.asset
               AND a.execution_id=o.execution_id
              WHERE o.settlement_status='AWAITING_VERIFIED_TRANSFER'
                AND o.settlement_tx_hash IS NULL
                AND o.fee_due_usd=e.fee_assessed_usd
                AND o.fee_due_usd>0
                AND b.status='FINALIZED_SUCCESS' AND b.side='SELL'
                AND e.source_tx_hash=b.tx_hash
                AND b.wallet_address=g.wallet_address
                AND b.stable=g.stable AND b.chain_id=g.chain_id
                AND g.authorization_method='PRIVY_SCOPED_GRANT'
                AND g.status='GRANT_VERIFIED'
                AND g.grant_verified_at IS NOT NULL
                AND g.consent_verified_at IS NOT NULL
                AND g.revoked_at IS NULL
                AND g.expires_at>now()
                AND g.policy_id IS NOT NULL
                AND g.remote_policy_sha256=g.policy_sha256
                AND separate.privy_did IS NULL
                AND p.execution_id IS NULL AND a.execution_id IS NULL
              ORDER BY o.created_at,o.privy_did,o.asset,o.execution_id
              LIMIT %s OFFSET %s"""
    with _PENDING_FEE_SCAN_LOCK:
        offset=_COMBINED_FEE_PAGE_OFFSET
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,offset))
                rows=list(cur.fetchall())
                if not rows and offset:
                    offset=0
                    cur.execute(sql,(limit,0))
                    rows=list(cur.fetchall())
        _COMBINED_FEE_PAGE_OFFSET=offset+len(rows)
    return rows


async def run_owner_approved_auto_fee_batch(
    *,client:httpx.AsyncClient,
)->dict:
    """Actual sends only after feature activation AND verified per-wallet grant."""
    result={"verified_fee_candidates":0,"new_fee_sends":0,
            "need_owner_or_network_reconciliation":0}
    if not automatic_fee_transfer_worker_enabled():
        return result
    if type(client) is not httpx.AsyncClient:
        raise ValueError("Verified server HTTP client required")
    try:
        owners=await asyncio.to_thread(_pending_owner_auto_fee_obligations)
        owners+=await asyncio.to_thread(_pending_combined_grant_fee_obligations)
    except psycopg.Error:
        LOG.warning("Cannot scan original unpaid five-percent HWM debt")
        return result
    for row in owners:
        result["verified_fee_candidates"]+=1
        try:
            sent=await submit_original_owner_auto_fee_once(
                did=row["privy_did"],asset=row["asset"],
                execution_id=row["execution_id"],client=client)
            if sent.get("state")=="SUBMITTED":
                result["new_fee_sends"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unknown submission is intentionally permanently reserved.
            result["need_owner_or_network_reconciliation"]+=1
    return result
