"""Owner-scoped durable LIVE EVM order states, never paper or invented fills.

Read-only display of existing order journal facts for the six additional
EVM assets. No transfers, account balances, position PnL or fee settlement
are inferred from SUBMITTED or finalized ERC20 swap status.
"""
from __future__ import annotations

from datetime import datetime,timezone
import re

import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException

from .storage import _db_url,_ensure_schema,_safe_did
from .shared_evm_swap_receipts import _pair

_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB","BTC","XRP","SOL"))
_STATES=frozenset((
    "SENDING","SUBMITTED","RECONCILIATION_REQUIRED",
    "FINALIZED_SUCCESS","FINALIZED_REVERTED",
))
_HASH=re.compile(r"^0x[0-9a-f]{64}$")


def _verified_order_row(row:object,asset:str)->dict:
    if type(row) is not dict or row.get("asset")!=asset:
        raise ValueError("Unmatched LIVE EVM order asset")
    pair=_pair(asset,row.get("stable"),row.get("side"))
    if (row.get("chain_id")!=pair["chain_id"]
            or row.get("status") not in _STATES
            or type(row.get("intent_key")) is not str
            or not row["intent_key"]
            or (row.get("tx_hash") is not None
                and (type(row["tx_hash"]) is not str
                     or not _HASH.fullmatch(row["tx_hash"])))
            or row["status"] in (
                "SUBMITTED","FINALIZED_SUCCESS","FINALIZED_REVERTED")
                and row.get("tx_hash") is None
            or not isinstance(row.get("created_at"),datetime)
            or not isinstance(row.get("updated_at"),datetime)
            or row["created_at"].tzinfo is None
            or row["updated_at"].tzinfo is None
            or type(row.get("native_cycle_verified")) is not bool
            or type(row.get("hwm_fee_recorded")) is not bool
            or type(row.get("fee_payment_verified")) is not bool
            or (row["fee_payment_verified"] and not row["hwm_fee_recorded"])
            or (row["native_cycle_verified"] and (
                asset not in ("BNB","POL","AVAX")
                or row["status"]!="FINALIZED_SUCCESS"))
            or (row["hwm_fee_recorded"] and (
                row.get("side")!="SELL"
                or row["status"]!="FINALIZED_SUCCESS"))):
        raise ValueError("Invalid durable EVM order state")
    state=row["status"]
    return {
        "asset":asset,"chain_id":pair["chain_id"],"network":pair["network"],
        "intent_key":row["intent_key"],"side":row["side"],
        "stable":row["stable"],"status":state,
        "tx_hash":row["tx_hash"],
        "swap_erc20_leg_finalized":state=="FINALIZED_SUCCESS",
        "swap_reverted":state=="FINALIZED_REVERTED",
        "pending_or_ambiguous":state in (
            "SENDING","SUBMITTED","RECONCILIATION_REQUIRED"),
        "automatic_retry_allowed":False,
        "native_trade_cycle_settled":row["native_cycle_verified"],
        "five_percent_hwm_accounting_recorded":row["hwm_fee_recorded"],
        "realized_net_pnl_usd":None,
        "fee_transferred":row["fee_payment_verified"],
        "created_at":row["created_at"].astimezone(timezone.utc).isoformat(),
        "updated_at":row["updated_at"].astimezone(timezone.utc).isoformat(),
    }


def get_owner_live_evm_order_status(
    did:str,*,asset:str,limit:int=20,
)->dict:
    did=_safe_did(did)
    if type(asset) is not str or asset not in _ASSETS:
        raise HTTPException(404,"Unsupported live EVM order asset")
    if type(limit) is not int or not 1<=limit<=50:
        raise HTTPException(422,"Invalid LIVE order history limit")
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT b.asset,b.chain_id,b.intent_key,b.side,b.stable,
                              b.status,b.tx_hash,b.created_at,b.updated_at,
                              EXISTS(
                                  SELECT 1
                                  FROM mininex_live_evm_completed_native_cycles c
                                  WHERE c.privy_did=b.privy_did
                                    AND c.asset=b.asset
                                    AND c.intent_key=b.intent_key
                                    AND c.chain_id=b.chain_id
                                    AND c.swap_tx_hash=b.tx_hash
                                    AND c.side=b.side AND c.stable=b.stable
                              ) AS native_cycle_verified,
                              EXISTS(
                                  SELECT 1 FROM mininex_evm_fee_events f
                                  WHERE f.privy_did=b.privy_did
                                    AND f.asset=b.asset
                                    AND f.execution_id=b.intent_key
                                    AND f.source_tx_hash=b.tx_hash
                              ) AS hwm_fee_recorded,
                              EXISTS(
                                  SELECT 1
                                  FROM mininex_evm_verified_fee_payments p
                                  WHERE p.privy_did=b.privy_did
                                    AND p.asset=b.asset
                                    AND p.execution_id=b.intent_key
                                    AND p.state='PAID_VERIFIED'
                                    AND p.tx_hash IS NOT NULL
                              ) AS fee_payment_verified
                       FROM mininex_live_evm_auto_broadcasts b
                       WHERE b.privy_did=%s AND b.asset=%s
                       ORDER BY b.created_at DESC,b.intent_key DESC
                       LIMIT %s""",
                    (did,asset,limit+1))
                rows=list(cur.fetchall())
    except psycopg.Error:
        raise HTTPException(503,"LIVE EVM journal unavailable") from None
    verified=[_verified_order_row(row,asset) for row in rows[:limit]]
    return {
        "mode":"OWNER_DURABLE_EVM_ORDER_STATUS_ONLY",
        "asset":asset,"orders":verified,
        "history_truncated":len(rows)>limit,
        "includes_simulation":False,
        "wallet_signing_enabled_by_this_api":False,
        "automatic_trade_execution_enabled_by_this_api":False,
        "profit_fee_settled":any(r["fee_transferred"] for r in verified),
    }
