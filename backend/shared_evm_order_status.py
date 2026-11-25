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
from .ethereum_settlement_plan import settlement_route

_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB","BTC","XRP","SOL","TRX"))
_STATES=frozenset((
    "SENDING","SUBMITTED","RECONCILIATION_REQUIRED",
    "FINALIZED_SUCCESS","FINALIZED_REVERTED",
))
_HASH=re.compile(r"^0x[0-9a-f]{64}$")


def _verified_order_row(row:object,asset:str)->dict:
    if type(row) is not dict or row.get("asset")!=asset:
        raise ValueError("Unmatched LIVE EVM order asset")
    cross=row.get("ethereum_settlement_order") is True
    if cross:
        route=settlement_route(asset,row.get("side"),row.get("stable"))
        if route["family"]!="CROSS_EVM":
            raise ValueError("Unmatched cross-chain order asset")
        pair={"chain_id":route["source_chain_id"],"network":route["source_network"]}
    else:
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
        "ethereum_settlement_order":cross,
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
                if asset in ("BNB","POL","AVAX","ARB"):
                    # Completed staged parents and direct automatic submissions
                    # share the same canonical parent ID as verified fill history.
                    cur.execute("""SELECT q.asset,q.source_chain_id AS chain_id,q.quote_id::text AS intent_key,
                        q.side,q.stable,
                        CASE WHEN f.outcome='FINALIZED' THEN 'FINALIZED_SUCCESS'
                             WHEN f.outcome='SOURCE_REVERTED' THEN 'FINALIZED_REVERTED'
                             ELSE s.state END AS status,
                        s.source_tx_hash AS tx_hash,s.created_at,COALESCE(f.verified_at,s.updated_at) AS updated_at,
                        FALSE AS native_cycle_verified,FALSE AS hwm_fee_recorded,
                        FALSE AS fee_payment_verified,TRUE AS ethereum_settlement_order
                        FROM mininex_ethereum_settlement_submissions s
                        JOIN mininex_ethereum_settlement_quotes q ON q.quote_id=s.quote_id AND q.privy_did=s.privy_did
                        LEFT JOIN mininex_ethereum_settlement_finality f ON f.quote_id=q.quote_id AND f.privy_did=q.privy_did
                        WHERE s.privy_did=%s AND q.asset=%s
                        ORDER BY s.created_at DESC,q.quote_id DESC LIMIT %s""",(did,asset,limit+1))
                    rows.extend(cur.fetchall())
                    # Until the original parent is finalized, show the durable
                    # staged order once, not every approval or bridge leg.
                    table="mininex_avax_staged" if asset=="AVAX" else "mininex_across_staged"
                    asset_expr="'AVAX'" if asset=="AVAX" else "o.asset"
                    chain=settlement_route(asset,"SELL","USDC")["source_chain_id"]
                    cur.execute(f"""SELECT {asset_expr} AS asset,
                        CASE WHEN o.side='BUY' THEN 1 ELSE {chain} END AS chain_id,
                        o.order_id::text AS intent_key,o.side,o.stable,
                        CASE WHEN o.state='REVERTED' THEN 'FINALIZED_REVERTED'
                             WHEN pending.state='UNCERTAIN' THEN 'RECONCILIATION_REQUIRED'
                             WHEN first.tx_hash IS NOT NULL THEN 'SUBMITTED'
                             ELSE 'SENDING' END AS status,
                        first.tx_hash,o.created_at,o.updated_at,
                        FALSE AS native_cycle_verified,FALSE AS hwm_fee_recorded,
                        FALSE AS fee_payment_verified,TRUE AS ethereum_settlement_order
                        FROM {table}_orders o
                        LEFT JOIN LATERAL (SELECT t.tx_hash FROM {table}_steps t
                            WHERE t.order_id=o.order_id AND t.privy_did=o.privy_did AND t.stage=1
                              AND t.kind<>'APPROVAL' AND t.tx_hash IS NOT NULL
                            ORDER BY t.created_at,t.step_id LIMIT 1) first ON TRUE
                        LEFT JOIN LATERAL (SELECT t.state FROM {table}_steps t
                            WHERE t.order_id=o.order_id AND t.privy_did=o.privy_did
                              AND t.state IN ('SENDING','SUBMITTED','SOURCE_FINALIZED','UNCERTAIN')
                            LIMIT 1) pending ON TRUE
                        WHERE o.privy_did=%s AND {asset_expr}=%s AND o.state IN ('OPEN','REVERTED')
                          AND (o.state<>'REVERTED' OR first.tx_hash IS NOT NULL)
                        ORDER BY o.created_at DESC,o.order_id DESC LIMIT %s""",(did,asset,limit+1))
                    rows.extend(cur.fetchall())
    except psycopg.Error:
        raise HTTPException(503,"LIVE EVM journal unavailable") from None
    rows.sort(key=lambda row:(row["created_at"],row["intent_key"]),reverse=True)
    # A concurrent parent completion between read statements cannot create
    # two copies of the same original trade in the display.
    unique={}
    for row in rows:
        key=row["intent_key"]
        if key not in unique or row["status"]=="FINALIZED_SUCCESS":
            unique[key]=row
    rows=list(unique.values())
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
