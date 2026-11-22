"""Durable settlement of a complete native EVM BUY/SELL cycle.

Only independent confirmed SwapRouter02 AND wrapped-native conversion
receipts can release the budget of BNB/POL/AVAX orders. The original
one-shot signal cannot be replayed, and this does not compute unrealized
profits, assess a fee, or make further Privy transactions.
"""
from __future__ import annotations

import re

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_orders import valid_order_key
from .shared_evm_swap_receipts import _pair,check_user_evm_swap
from .shared_native_evm_receipts import check_user_native_evm_conversion
from .shared_evm_trade_cycle_proof import verify_evm_trade_cycle
from .storage import _db_url,_ensure_schema,_safe_did

_NATIVE=frozenset(("ETH","BNB","POL","AVAX"))
_HASH=re.compile(r"^0x[0-9a-f]{64}$")


def _complete_native_cycle_locked(
    cur,*,did:str,asset:str,key:str,proof:dict,
)->dict:
    """Atomic complete-cycle insert and final budget release."""
    if (asset not in _NATIVE or type(proof) is not dict
            or proof.get("mode")!="READ_ONLY_FINALIZED_EVM_TRADE_CYCLE_PROOF"
            or proof.get("asset")!=asset
            or proof.get("required_transaction_legs_complete") is not True
            or proof.get("native_conversion_verified") is not True
            or proof.get("inventory_or_cost_basis_settled") is not False
            or proof.get("realized_net_profit_verified") is not False
            or proof.get("five_percent_fee_assessed") is not False
            or type(proof.get("swap_tx_hash")) is not str
            or not _HASH.fullmatch(proof["swap_tx_hash"])
            or type(proof.get("native_conversion_tx_hash")) is not str
            or not _HASH.fullmatch(proof["native_conversion_tx_hash"])
            or proof["native_conversion_tx_hash"]==proof["swap_tx_hash"]):
        raise ValueError("Complete owner-native EVM receipt proof required")
    side=proof.get("side")
    stable=proof.get("stable")
    pair=_pair(asset,stable,side)
    if (proof.get("chain_id")!=pair["chain_id"]
            or proof.get("network")!=pair["network"]
            or type(proof.get("wallet_address")) is not str
            or len(proof["wallet_address"])!=42):
        raise ValueError("Wrong native cycle identity")
    cur.execute(
        """SELECT status,tx_hash,wallet_address,chain_id,side,stable
           FROM mininex_live_evm_auto_broadcasts
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,key))
    order=cur.fetchone()
    if (type(order) is not dict
            or order.get("status")!="FINALIZED_SUCCESS"
            or order.get("tx_hash")!=proof["swap_tx_hash"]
            or order.get("wallet_address")!=proof["wallet_address"]
            or order.get("chain_id")!=pair["chain_id"]
            or order.get("side")!=side
            or order.get("stable")!=stable):
        raise HTTPException(409,"Native cycle does not match finalized owner swap")
    cur.execute(
        """SELECT tx_hash,wallet_address,side,stable
           FROM mininex_live_evm_verified_fills
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,key))
    fill=cur.fetchone()
    if (type(fill) is not dict
            or fill.get("tx_hash")!=proof["swap_tx_hash"]
            or fill.get("wallet_address")!=proof["wallet_address"]
            or fill.get("side")!=side or fill.get("stable")!=stable):
        raise HTTPException(409,"Missing actual finalized EVM swap fill")
    cur.execute(
        """INSERT INTO mininex_live_evm_completed_native_cycles
           (privy_did,asset,intent_key,chain_id,wallet_address,
            swap_tx_hash,conversion_tx_hash,side,stable)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (did,asset,key,pair["chain_id"],proof["wallet_address"],
         proof["swap_tx_hash"],proof["native_conversion_tx_hash"],
         side,stable))
    cur.execute(
        """UPDATE mininex_live_evm_budget_reservations
           SET state='FINALIZED',updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND state='RECONCILIATION_REQUIRED' RETURNING state""",
        (did,asset,key))
    updated=cur.fetchone()
    if type(updated) is not dict or updated.get("state")!="FINALIZED":
        raise HTTPException(409,"Native conversion budget remains unreconciled")
    return {
        "mode":"OWNER_FINALIZED_NATIVE_EVM_TRADE_CYCLE",
        "asset":asset,"intent_key":key,"swap_tx_hash":proof["swap_tx_hash"],
        "native_conversion_tx_hash":proof["native_conversion_tx_hash"],
        "required_transaction_legs_complete":True,
        "budget_reservation_reconciled":True,
        "realized_net_usd":None,"performance_fee_assessed":False,
        "automatic_new_trade_sent":False,
    }


async def reconcile_internal_native_evm_cycle(
    *,did:str,asset:str,intent_key:str,swap_tx_hash:str,
    native_tx_hash:str,side:str,stable:str,
)->dict:
    """Trusted backend-only on-chain reader; never trusts client proof flags."""
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    if (asset not in _NATIVE or type(swap_tx_hash) is not str
            or not _HASH.fullmatch(swap_tx_hash)
            or type(native_tx_hash) is not str
            or not _HASH.fullmatch(native_tx_hash)):
        raise ValueError("Invalid native EVM reconciliation target")
    swap=await check_user_evm_swap(
        did,swap_tx_hash,asset=asset,side=side,stable=stable)
    conversion=await check_user_native_evm_conversion(
        did,native_tx_hash,asset=asset,
        operation="WITHDRAW" if side=="BUY" else "DEPOSIT")
    wallet=swap.get("wallet_address")
    try:
        proof=verify_evm_trade_cycle(
            asset=asset,side=side,stable=stable,wallet=wallet,
            swap_proof=swap,native_proof=conversion)
    except (TypeError,ValueError,KeyError):
        raise HTTPException(409,"Native EVM trade is not fully finalized") from None
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _complete_native_cycle_locked(
                    cur,did=did,asset=asset,key=key,proof=proof)
    except psycopg.Error:
        raise HTTPException(503,"Native EVM cycle settlement unavailable") from None
