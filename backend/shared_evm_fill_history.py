"""Owner-only REAL EVM fill history, strictly separated by coin.

Only durable finalized on-chain ERC-20 swap legs are returned. No PAPER
events, open positions, native-conversion settlement, realized profit, or
fee-payment claim may be inferred from this history.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .storage import _db_url,_ensure_schema,_safe_did
from .shared_evm_swap_receipts import _pair

LOG=logging.getLogger("mininex.evm_real_fill_history")
_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"))


def _verified_fill_row(row: object,asset:str) -> dict:
    if type(row) is not dict or row.get("asset")!=asset:
        raise ValueError("Untrusted owner/coin fill")
    side=row.get("side")
    stable=row.get("stable")
    expected=_pair(asset,stable,side)
    if (type(row.get("chain_id")) is not int
            or row["chain_id"]!=expected["chain_id"]
            or type(row.get("tx_hash")) is not str
            or len(row["tx_hash"])!=66
            or type(row.get("created_at")) is not datetime
            or row["created_at"].tzinfo is None
            or type(row.get("wallet_address")) is not str
            or len(row["wallet_address"])!=42):
        raise ValueError("Untrusted finalized fill identity")
    for field in ("source_amount","destination_amount","network_fee_native"):
        value=row.get(field)
        if not isinstance(value,Decimal) or not value.is_finite():
            raise ValueError("Untrusted finalized chain quantity")
        if value<0 or (field!="network_fee_native" and value<=0):
            raise ValueError("Invalid finalized chain quantity")
    if type(row.get("intent_key")) is not str or not row["intent_key"]:
        raise ValueError("Untrusted journal intent")
    return {
        "asset":asset,"chain_id":expected["chain_id"],
        "network":expected["network"],"intent_key":row["intent_key"],
        "wallet_address":row["wallet_address"].lower(),
        "tx_hash":row["tx_hash"],"side":side,"stable":stable,
        "spent_asset":stable if side=="BUY" else asset,
        "received_asset":asset if side=="BUY" else stable,
        "spent_amount":format(row["source_amount"],"f"),
        "received_amount":format(row["destination_amount"],"f"),
        "network_fee_native":format(row["network_fee_native"],"f"),
        "created_at":row["created_at"].astimezone(timezone.utc).isoformat(),
        "onchain_finalized":True,
        "native_coin_cycle_complete":False,
        "realized_net_usd":None,
        "performance_fee_paid":False,
    }


def get_owner_evm_fill_history(did:str,*,asset:str,limit:int=30)->dict:
    did=_safe_did(did)
    if type(asset) is not str or asset not in _ASSETS:
        raise HTTPException(404,"No finalized EVM trade history for asset")
    if type(limit) is not int or not 1<=limit<=50:
        raise HTTPException(422,"Invalid fill history limit")
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT asset,chain_id,intent_key,tx_hash,wallet_address,
                              side,stable,source_amount,destination_amount,
                              network_fee_native,created_at
                       FROM mininex_live_evm_verified_fills
                       WHERE privy_did=%s AND asset=%s
                       ORDER BY created_at DESC,intent_key DESC LIMIT %s""",
                    (did,asset,limit+1))
                rows=list(cur.fetchall())
        parsed=[_verified_fill_row(row,asset) for row in rows[:limit]]
        return {
            "mode":"FINALIZED_EVM_REAL_FILLS_ONLY",
            "asset":asset,"network":_pair(
                asset,"USDT" if asset=="BNB" else "USDC","BUY")["network"],
            "fills":parsed,
            "history_truncated":len(rows)>limit,
            "includes_simulation":False,
            "wallet_signing_authorized":False,
            "full_native_order_settlement_verified":False,
            "realized_net_pnl_calculated":False,
            "performance_fee_settled":False,
        }
    except psycopg.Error:
        LOG.warning("Verified EVM fill history unavailable")
        raise HTTPException(503,"Finalized EVM activity unavailable") from None
