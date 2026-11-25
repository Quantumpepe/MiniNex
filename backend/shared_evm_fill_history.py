"""Owner-only REAL EVM fill history, strictly separated by coin.

Only durable finalized on-chain ERC-20 swap legs are returned. No PAPER
events, open positions, native-conversion settlement, realized profit, or
fee-payment claim may be inferred from this history.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .storage import _db_url,_ensure_schema,_safe_did
from .shared_evm_swap_receipts import _pair
from .ethereum_settlement_plan import settlement_route

LOG=logging.getLogger("mininex.evm_real_fill_history")
_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB","BTC","XRP","SOL","TRX"))


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


def _cross_fill_row(row,asset):
    if row["asset"]!=asset or row["outcome"]!="FINALIZED":
        raise ValueError("Unverified cross-chain fill")
    route=settlement_route(asset,row["side"],row["stable"])
    if (route["family"]!="CROSS_EVM" or row["source_chain_id"]!=route["source_chain_id"]
        or row["destination_chain_id"]!=route["destination_chain_id"]
        or row["input_atomic"]<=0 or row["received_atomic"]<=0 or row["source_gas_atomic"]<0):
        raise ValueError("Original Ethereum settlement differs")
    with localcontext() as ctx:
        ctx.prec=100
        spent=format(Decimal(row["input_atomic"]).scaleb(-route["source_decimals"]),"f")
        received=format(Decimal(row["received_atomic"]).scaleb(-route["destination_decimals"]),"f")
        gas=format(Decimal(row["source_gas_atomic"]).scaleb(-18),"f")
    return {"asset":asset,"chain_id":row["source_chain_id"],"network":route["source_network"],
        "destination_chain_id":row["destination_chain_id"],"destination_network":route["destination_network"],
        "intent_key":str(row["quote_id"]),"tx_hash":row["source_tx_hash"],
        "destination_tx_hash":row["destination_tx_hash"],"side":row["side"],"stable":row["stable"],
        "spent_asset":route["source_asset"],"received_asset":route["destination_asset"],
        "spent_amount":spent,"received_amount":received,"network_fee_native":gas,
        "created_at":row["verified_at"].astimezone(timezone.utc).isoformat(),
        "onchain_finalized":True,"ethereum_settlement_verified":True,
        "native_coin_cycle_complete":False,"realized_net_usd":None,"performance_fee_paid":False}


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
                cross=[]
                if asset in ("BNB","POL","AVAX","ARB"):
                    cur.execute("""SELECT q.quote_id,q.asset,q.side,q.stable,q.input_atomic,
                         f.source_chain_id,f.destination_chain_id,f.source_tx_hash,
                         f.destination_tx_hash,f.received_atomic,f.source_gas_atomic,
                         f.outcome,f.verified_at
                         FROM mininex_ethereum_settlement_finality f
                         JOIN mininex_ethereum_settlement_quotes q
                           ON q.quote_id=f.quote_id AND q.privy_did=f.privy_did
                         WHERE f.privy_did=%s AND q.asset=%s AND f.outcome='FINALIZED'
                         ORDER BY f.verified_at DESC,f.quote_id DESC LIMIT %s""",
                         (did,asset,limit+1))
                    cross=list(cur.fetchall())
        parsed=[_verified_fill_row(row,asset) for row in rows]+[_cross_fill_row(row,asset) for row in cross]
        parsed.sort(key=lambda row:(row["created_at"],row["intent_key"]),reverse=True)
        truncated=len(parsed)>limit
        parsed=parsed[:limit]
        return {
            "mode":"FINALIZED_EVM_REAL_FILLS_ONLY",
            "asset":asset,"network":_pair(
                asset,"USDT" if asset=="BNB" else "USDC","BUY")["network"],
            "fills":parsed,
            "history_truncated":truncated,
            "includes_simulation":False,
            "wallet_signing_authorized":False,
            "full_native_order_settlement_verified":False,
            "realized_net_pnl_calculated":False,
            "performance_fee_settled":False,
        }
    except psycopg.Error:
        LOG.warning("Verified EVM fill history unavailable")
        raise HTTPException(503,"Finalized EVM activity unavailable") from None
