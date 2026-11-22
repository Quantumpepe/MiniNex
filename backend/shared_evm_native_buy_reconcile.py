"""Passive finality and one-shot completion for already owned native EVM BUY.

Never guesses transaction hashes, repeats SENDING sends, invents wrap legs,
assesses fees or starts owner sessions. Newly finalized BUYs remain locked
until the independently finalized native WITHDRAW and linked cycle proof.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import logging
from threading import Lock

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_native_evm_receipts import check_user_native_evm_conversion
from .shared_evm_native_cycle_finality import reconcile_internal_native_evm_cycle
from .shared_evm_native_buy_completion import submit_internal_native_evm_buy_completion
from .shared_evm_swap_receipts import _pair
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.native_evm_buy_reconcile")
_NATIVE=frozenset(("ETH","BNB","POL","AVAX"))
_NATIVE_BUY_UNWRAP_PAGE_LOCK=Lock()
_NATIVE_BUY_UNWRAP_PAGE_OFFSET=0
_NATIVE_BUY_CYCLE_PAGE_LOCK=Lock()
_NATIVE_BUY_CYCLE_PAGE_OFFSET=0
_NATIVE_BUY_RECEIPT_PAGE_LOCK=Lock()
_NATIVE_BUY_RECEIPT_PAGE_OFFSET=0


def pending_native_steps(limit:int=20)->list[dict]:
    """Only durable known-hash SUBMITTED conversions can be finalized."""
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Invalid native EVM journal scan size")
    global _NATIVE_BUY_RECEIPT_PAGE_OFFSET
    db=_db_url()
    _ensure_schema(db)
    query="""SELECT privy_did,asset,intent_key,operation,chain_id,
                          wallet_address,amount_wei,tx_hash
                   FROM mininex_live_evm_native_auto_steps
                   WHERE status='SUBMITTED' AND tx_hash IS NOT NULL
                     AND operation='WITHDRAW'
                     AND asset IN ('ETH','BNB','POL','AVAX')
                   ORDER BY created_at ASC,privy_did,asset,intent_key LIMIT %s OFFSET %s"""
    # Unfinalized original conversions must not starve newer owner receipts;
    # scanning never authorizes or retries a wallet transaction.
    with _NATIVE_BUY_RECEIPT_PAGE_LOCK:
        offset=_NATIVE_BUY_RECEIPT_PAGE_OFFSET
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query,(limit,offset))
                rows=list(cur.fetchall())
                if not rows and offset:
                    offset=0
                    cur.execute(query,(limit,0))
                    rows=list(cur.fetchall())
        _NATIVE_BUY_RECEIPT_PAGE_OFFSET=offset+len(rows)
    return rows


def _finalize_native_step_locked(
    cur,*,did:str,asset:str,key:str,proof:dict,
)->str:
    """Owner/chain/amount/transaction must match original SENDING claim."""
    if asset not in _NATIVE or type(proof) is not dict:
        raise ValueError("Untrusted completed EVM native receipt")
    pair=_pair(asset,"USDT" if asset=="BNB" else "USDC","BUY")
    if (proof.get("mode")!="SHARED_FINALIZED_NATIVE_EVM_CONVERSION"
            or proof.get("asset")!=asset
            or proof.get("operation")!="WITHDRAW"
            or proof.get("chain_id")!=pair["chain_id"]
            or proof.get("network")!=pair["network"]
            or proof.get("transaction_finalized") is not True
            or proof.get("state") not in
                 ("FINALIZED_NATIVE_CONVERSION","FINALIZED_REVERTED")):
        raise ValueError("Onchain native conversion not finalized")
    success=proof["state"]=="FINALIZED_NATIVE_CONVERSION"
    cur.execute(
        """SELECT chain_id,wallet_address,amount_wei,tx_hash,status
           FROM mininex_live_evm_native_auto_steps
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND operation='WITHDRAW' FOR UPDATE""",(did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict
            or row.get("status")!="SUBMITTED"
            or row.get("chain_id")!=pair["chain_id"]
            or row.get("wallet_address")!=proof.get("wallet_address")
            or row.get("tx_hash")!=proof.get("tx_hash")):
        raise HTTPException(409,"Native receipt does not match original send")
    if success:
        try:
            amount=Decimal(str(proof.get("amount_native")))*Decimal(10**18)
            if (not amount.is_finite() or amount!=amount.to_integral_value()
                    or amount!=Decimal(str(row["amount_wei"]))
                    or proof.get("confirmed_native_conversion") is not True):
                raise ValueError("Actual wrapped-native quantity differs")
        except (ArithmeticError,ValueError,TypeError):
            raise HTTPException(409,"Native receipt quantity unverified") from None
    state="FINALIZED_SUCCESS" if success else "FINALIZED_REVERTED"
    cur.execute(
        """UPDATE mininex_live_evm_native_auto_steps
           SET status=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND operation='WITHDRAW' AND status='SUBMITTED'
             AND tx_hash=%s RETURNING status""",
        (state,did,asset,key,row["tx_hash"]))
    updated=cur.fetchone()
    if type(updated) is not dict or updated.get("status")!=state:
        raise HTTPException(409,"Concurrent EVM native finalization conflict")
    return state


async def reconcile_original_native_step(row:dict)->str:
    did=_safe_did(row["privy_did"])
    asset=row["asset"]
    if asset not in _NATIVE or row.get("operation")!="WITHDRAW":
        raise ValueError("Unsupported native receipt journal")
    stable="USDT" if asset=="BNB" else "USDC"
    if row.get("chain_id")!=_pair(asset,stable,"BUY")["chain_id"]:
        raise ValueError("Wrong native EVM journal network")
    proof=await check_user_native_evm_conversion(
        did,row["tx_hash"],asset=asset,operation="WITHDRAW")
    if proof.get("transaction_finalized") is not True:
        return "AWAITING_FINALITY"
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _finalize_native_step_locked(
                cur,did=did,asset=asset,key=row["intent_key"],proof=proof)


def completed_native_buys_needing_cycle(limit:int=20)->list[dict]:
    """Only already-converted onchain BUYs whose original budget is locked."""
    global _NATIVE_BUY_CYCLE_PAGE_OFFSET
    db=_db_url()
    _ensure_schema(db)
    query="""SELECT b.privy_did,b.asset,b.intent_key,b.tx_hash AS swap_tx_hash,
                          n.tx_hash AS native_tx_hash,b.side,b.stable
                   FROM mininex_live_evm_auto_broadcasts b
                   JOIN mininex_live_evm_native_auto_steps n
                     ON n.privy_did=b.privy_did AND n.asset=b.asset
                    AND n.intent_key=b.intent_key AND n.operation='WITHDRAW'
                   JOIN mininex_live_evm_budget_reservations r
                     ON r.privy_did=b.privy_did AND r.asset=b.asset
                    AND r.intent_key=b.intent_key
                   WHERE b.asset IN ('ETH','BNB','POL','AVAX')
                     AND b.status='FINALIZED_SUCCESS' AND b.side='BUY'
                     AND n.status='FINALIZED_SUCCESS'
                     AND r.state='RECONCILIATION_REQUIRED'
                   ORDER BY n.updated_at ASC,n.privy_did,n.asset,n.intent_key LIMIT %s OFFSET %s"""
    # Rotate across all owner-approved native steps; a stalled older receipt
    # or unresolved downstream operation must not starve newer owners.
    with _NATIVE_BUY_CYCLE_PAGE_LOCK:
        selected=_NATIVE_BUY_CYCLE_PAGE_OFFSET
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query,(limit,selected))
                items=list(cur.fetchall())
                if not items and selected:
                    selected=0
                    cur.execute(query,(limit,0))
                    items=list(cur.fetchall())
        _NATIVE_BUY_CYCLE_PAGE_OFFSET=selected+len(items)
    return items


def original_native_buys_without_step(limit:int=20)->list[dict]:
    """Only approved, owner-active, finalized BUYs eligible for once-only unwrap."""
    global _NATIVE_BUY_UNWRAP_PAGE_OFFSET
    db=_db_url()
    _ensure_schema(db)
    query="""SELECT b.privy_did,b.asset,b.intent_key,b.grant_source
                   FROM mininex_live_evm_auto_broadcasts b
                   JOIN mininex_live_evm_verified_fills f
                     ON f.privy_did=b.privy_did AND f.asset=b.asset
                    AND f.intent_key=b.intent_key AND f.tx_hash=b.tx_hash
                   JOIN mininex_live_evm_auto_sessions s
                     ON s.privy_did=b.privy_did AND s.asset=b.asset
                   JOIN mininex_live_evm_budget_reservations r
                     ON r.privy_did=b.privy_did AND r.asset=b.asset
                    AND r.intent_key=b.intent_key
                   LEFT JOIN mininex_live_evm_native_auto_steps n
                     ON n.privy_did=b.privy_did AND n.asset=b.asset
                    AND n.intent_key=b.intent_key AND n.operation='WITHDRAW'
                   WHERE b.asset IN ('ETH','BNB','POL','AVAX')
                     AND b.status='FINALIZED_SUCCESS' AND b.side='BUY'
                     AND f.side='BUY'
                     AND s.status='ACTIVE' AND s.started_by_owner_at IS NOT NULL
                     AND r.state='RECONCILIATION_REQUIRED'
                     AND n.intent_key IS NULL
                   ORDER BY b.created_at ASC,b.privy_did,b.asset,b.intent_key LIMIT %s OFFSET %s"""
    # Rotate across all owner-approved native steps; a stalled older receipt
    # or unresolved downstream operation must not starve newer owners.
    with _NATIVE_BUY_UNWRAP_PAGE_LOCK:
        selected=_NATIVE_BUY_UNWRAP_PAGE_OFFSET
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query,(limit,selected))
                items=list(cur.fetchall())
                if not items and selected:
                    selected=0
                    cur.execute(query,(limit,0))
                    items=list(cur.fetchall())
        _NATIVE_BUY_UNWRAP_PAGE_OFFSET=selected+len(items)
    return items


async def reconcile_native_buy_cycle_once(*,client:httpx.AsyncClient,
                                          limit:int=20)->dict:
    """Never resubmit a claimed/ambiguous order. Distinct scan phases."""
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Invalid native completion scan size")
    result={"finalized_native_steps":0,"settled_full_cycles":0,
            "new_native_sends":0,"pending":0,"errors":0}
    for row in await asyncio.to_thread(pending_native_steps,limit):
        try:
            state=await reconcile_original_native_step(row)
            if state in ("FINALIZED_SUCCESS","FINALIZED_REVERTED"):
                result["finalized_native_steps"]+=1
            else:
                result["pending"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Original native EVM send receipt unresolved")
            result["errors"]+=1
    for row in await asyncio.to_thread(completed_native_buys_needing_cycle,limit):
        try:
            cycle=await reconcile_internal_native_evm_cycle(
                did=row["privy_did"],asset=row["asset"],
                intent_key=row["intent_key"],
                swap_tx_hash=row["swap_tx_hash"],
                native_tx_hash=row["native_tx_hash"],
                side="BUY",stable=row["stable"])
            if cycle.get("budget_reservation_reconciled") is True:
                result["settled_full_cycles"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Verified native EVM BUY cycle not yet settled")
            result["errors"]+=1
    for row in await asyncio.to_thread(original_native_buys_without_step,limit):
        try:
            submitted=await submit_internal_native_evm_buy_completion(
                did=row["privy_did"],asset=row["asset"],
                intent_key=row["intent_key"],client=client,
                **({"central":True} if row.get("grant_source")==
                    "PRIVY_CENTRAL_ONE_SIGNER" else {}))
            if submitted.get("state")=="SUBMITTED":
                result["new_native_sends"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Native EVM BUY completion not submitted")
            result["errors"]+=1
    return result
