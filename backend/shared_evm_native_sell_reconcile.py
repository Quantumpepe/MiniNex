"""Passive completion of one owner-approved native EVM SELL.

BNB/POL/AVAX SELL is DEPOSIT(native -> wrapped) then Router02 SELL.
A separately mined DEPOSIT does not itself create an order, a fill or PnL.
Only the original immutable event, verified native receipt and current
user-started session may initiate the one downstream swap. No retransmit
after an uncertain Privy response or a known order claim.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal,InvalidOperation
import logging

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_orders import valid_order_key
from .shared_evm_native_cycle_finality import reconcile_internal_native_evm_cycle
from .shared_evm_strategy_execution import execute_internal_evm_strategy_signal
from .shared_evm_swap_receipts import _pair
from .shared_native_evm_receipts import check_user_native_evm_conversion
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.native_evm_sell_reconcile")
_NATIVE=frozenset(("BNB","POL","AVAX"))


def pending_native_sell_deposits(limit:int=20)->list[dict]:
    """Only already submitted original hashes; never retry an ambiguous POST."""
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Invalid native EVM SELL scan size")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT privy_did,asset,intent_key,chain_id,tx_hash
                   FROM mininex_live_evm_native_sell_intents
                   WHERE status='SUBMITTED' AND tx_hash IS NOT NULL
                     AND asset IN ('BNB','POL','AVAX')
                   ORDER BY created_at ASC LIMIT %s""",(limit,))
            return list(cur.fetchall())


def _finalize_native_sell_deposit_locked(
    cur,*,did:str,asset:str,key:str,proof:dict,
)->str:
    """Owner, chain, exact amount and original hash checked under SQL lock."""
    if asset not in _NATIVE or type(proof) is not dict:
        raise ValueError("Unsupported native SELL receipt proof")
    pair=_pair(asset,"USDT" if asset=="BNB" else "USDC","SELL")
    if (proof.get("mode")!="SHARED_FINALIZED_NATIVE_EVM_CONVERSION"
            or proof.get("asset")!=asset
            or proof.get("operation")!="DEPOSIT"
            or proof.get("chain_id")!=pair["chain_id"]
            or proof.get("network")!=pair["network"]
            or proof.get("transaction_finalized") is not True
            or proof.get("state") not in
                  ("FINALIZED_NATIVE_CONVERSION","FINALIZED_REVERTED")):
        raise ValueError("Owner native SELL DEPOSIT not finalized")
    success=proof["state"]=="FINALIZED_NATIVE_CONVERSION"
    cur.execute(
        """SELECT chain_id,wallet_address,amount_wei,tx_hash,status,stable
           FROM mininex_live_evm_native_sell_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict
            or row.get("status")!="SUBMITTED"
            or row.get("chain_id")!=pair["chain_id"]
            or row.get("stable")!=pair["stable"]
            or row.get("wallet_address")!=proof.get("wallet_address")
            or row.get("tx_hash")!=proof.get("tx_hash")):
        raise HTTPException(409,"Native SELL receipt differs from durable deposit")
    if success:
        try:
            amount=Decimal(str(proof.get("amount_native")))*Decimal(10**18)
            if (not amount.is_finite() or amount<=0
                    or amount!=amount.to_integral_value()
                    or amount!=Decimal(str(row["amount_wei"]))
                    or proof.get("confirmed_native_conversion") is not True):
                raise ValueError("Original wrapped SELL amount differs")
        except (InvalidOperation,ArithmeticError,TypeError,ValueError):
            raise HTTPException(409,"Native SELL amount not independently verified") from None
    new_state="FINALIZED_SUCCESS" if success else "FINALIZED_REVERTED"
    cur.execute(
        """UPDATE mininex_live_evm_native_sell_intents
           SET status=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status='SUBMITTED' AND tx_hash=%s
           RETURNING status""",
        (new_state,did,asset,key,row["tx_hash"]))
    result=cur.fetchone()
    if type(result) is not dict or result.get("status")!=new_state:
        raise HTTPException(409,"Concurrent native SELL finalization conflict")
    return new_state


async def reconcile_original_sell_deposit(row:dict)->str:
    did=_safe_did(row["privy_did"])
    asset=row["asset"]
    if asset not in _NATIVE:
        raise ValueError("Unsupported original native SELL")
    pair=_pair(asset,"USDT" if asset=="BNB" else "USDC","SELL")
    if row.get("chain_id")!=pair["chain_id"]:
        raise ValueError("Wrong native SELL network")
    proof=await check_user_native_evm_conversion(
        did,row["tx_hash"],asset=asset,operation="DEPOSIT")
    if proof.get("transaction_finalized") is not True:
        return "AWAITING_FINALITY"
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _finalize_native_sell_deposit_locked(
                cur,did=did,asset=asset,key=row["intent_key"],proof=proof)


def original_native_sells_ready_for_swap(limit:int=20)->list[dict]:
    """Never broadcast a second Router02 transaction for an existing key.

    A DEPOSIT can be finalized while the user has paused the session.
    Respect that pause, and only continue when consent and policy are
    independently reverified by the downstream once-only order claim.
    """
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT n.privy_did,n.asset,n.intent_key,n.strategy_run_id,
                          n.signal_event_id,n.stable,n.chain_id,
                          n.budget_revision,n.amount_wei
                   FROM mininex_live_evm_native_sell_intents n
                   JOIN mininex_live_evm_auto_sessions s
                     ON s.privy_did=n.privy_did AND s.asset=n.asset
                   LEFT JOIN mininex_live_evm_auto_broadcasts b
                     ON b.privy_did=n.privy_did AND b.asset=n.asset
                    AND b.intent_key=n.intent_key
                   WHERE n.asset IN ('BNB','POL','AVAX')
                     AND n.status='FINALIZED_SUCCESS'
                     AND s.status='ACTIVE'
                     AND s.budget_revision=n.budget_revision
                     AND s.stable=n.stable AND s.chain_id=n.chain_id
                     AND s.wallet_address=n.wallet_address
                     AND b.intent_key IS NULL
                   ORDER BY n.updated_at ASC LIMIT %s""",(limit,))
            return list(cur.fetchall())


def original_native_sells_needing_cycle(limit:int=20)->list[dict]:
    """Join both originally recorded hashes; require finalized swap fill."""
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT b.privy_did,b.asset,b.intent_key,
                          b.tx_hash AS swap_tx_hash,
                          n.tx_hash AS native_tx_hash,b.side,b.stable
                   FROM mininex_live_evm_auto_broadcasts b
                   JOIN mininex_live_evm_native_sell_intents n
                     ON n.privy_did=b.privy_did AND n.asset=b.asset
                    AND n.intent_key=b.intent_key
                   JOIN mininex_live_evm_verified_fills f
                     ON f.privy_did=b.privy_did AND f.asset=b.asset
                    AND f.intent_key=b.intent_key AND f.tx_hash=b.tx_hash
                   JOIN mininex_live_evm_budget_reservations r
                     ON r.privy_did=b.privy_did AND r.asset=b.asset
                    AND r.intent_key=b.intent_key
                   LEFT JOIN mininex_live_evm_completed_native_cycles c
                     ON c.privy_did=b.privy_did AND c.asset=b.asset
                    AND c.intent_key=b.intent_key
                   WHERE b.asset IN ('BNB','POL','AVAX')
                     AND b.status='FINALIZED_SUCCESS' AND b.side='SELL'
                     AND n.status='FINALIZED_SUCCESS' AND f.side='SELL'
                     AND r.state='RECONCILIATION_REQUIRED'
                     AND c.intent_key IS NULL
                   ORDER BY b.updated_at ASC LIMIT %s""",(limit,))
            return list(cur.fetchall())


def _exact_wrapped_sell_amount(row:dict)->str:
    if type(row) is not dict or row.get("asset") not in _NATIVE:
        raise ValueError("Untrusted finalized native deposit")
    pair=_pair(row["asset"],row.get("stable"),"SELL")
    if row.get("chain_id")!=pair["chain_id"]:
        raise ValueError("Native source chain changed")
    amount=row.get("amount_wei")
    try:
        d=Decimal(str(amount))
        if (not d.is_finite() or d<=0 or d!=d.to_integral_value()
                or d>=Decimal(10)**45):
            raise ValueError("Invalid original wrapped SELL units")
        return format(d/Decimal(10**18),"f")
    except (InvalidOperation,ArithmeticError,TypeError):
        raise ValueError("Original native SELL quantity unavailable") from None


async def reconcile_native_sell_once(
    *,client:httpx.AsyncClient,limit:int=20,
)->dict:
    """Three sequential stages, never auto-retries any claimed send."""
    if type(client) is not httpx.AsyncClient or type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Invalid native SELL worker context")
    result={"finalized_deposits":0,"complete_cycles":0,
            "original_swaps_submitted":0,"pending":0,"errors":0}
    for row in await asyncio.to_thread(pending_native_sell_deposits,limit):
        try:
            state=await reconcile_original_sell_deposit(row)
            if state in ("FINALIZED_SUCCESS","FINALIZED_REVERTED"):
                result["finalized_deposits"]+=1
            else:
                result["pending"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            result["errors"]+=1
            LOG.exception("Original native SELL deposit receipt unresolved")
    for row in await asyncio.to_thread(original_native_sells_needing_cycle,limit):
        try:
            cycle=await reconcile_internal_native_evm_cycle(
                did=row["privy_did"],asset=row["asset"],
                intent_key=row["intent_key"],swap_tx_hash=row["swap_tx_hash"],
                native_tx_hash=row["native_tx_hash"],side="SELL",stable=row["stable"])
            if cycle.get("budget_reservation_reconciled") is True:
                result["complete_cycles"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            result["errors"]+=1
            LOG.exception("Original native SELL complete cycle unresolved")
    for row in await asyncio.to_thread(original_native_sells_ready_for_swap,limit):
        try:
            amount=_exact_wrapped_sell_amount(row)
            sent=await execute_internal_evm_strategy_signal(
                did=row["privy_did"],asset=row["asset"],
                strategy_run_id=row["strategy_run_id"],
                signal_event_id=row["signal_event_id"],
                side="SELL",stable=row["stable"],amount=amount,
                budget_revision=row["budget_revision"],client=client)
            if sent.get("state")=="SUBMITTED":
                result["original_swaps_submitted"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            result["errors"]+=1
            LOG.exception("Original native SELL downstream swap not ready")
    return result
