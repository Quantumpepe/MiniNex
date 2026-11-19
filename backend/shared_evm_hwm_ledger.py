"""One 5% high-water-mark accounting ledger for five additional EVM coins.

Accounting is PER OWNER and PER ASSET. This module does not compute a
cost basis from a quote; it MUST only be invoked by a future trusted
reconciler after verified wallet swaps, FIFO inventory, fees, and
native gas costs have produced an immutable realized NET USD result.

No public API can write to it. A fee obligation is NOT a token transfer.
"""
from __future__ import annotations

from decimal import Decimal
import logging
import re

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .fees import RATE,assess_realized_gain,decimal_amount
from .live_orders import valid_order_key
from .shared_evm_swap_receipts import _pair
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.shared_evm_hwm")
_EVM=frozenset(("BNB","POL","AVAX","LINK","UNI","ARB"))
_HASH=re.compile(r"^0x[0-9a-f]{64}$")


def _assess_locked(
    cur,*,did:str,asset:str,execution_id:str,
    source_tx_hash:str,realized_net_usd:object,
)->dict:
    """Atomically assess at most once against the asset's persistent HWM."""
    if (asset not in _EVM or type(source_tx_hash) is not str
            or not _HASH.fullmatch(source_tx_hash)):
        raise ValueError("Unverified additional EVM closing transaction")
    delta=decimal_amount(realized_net_usd)
    cur.execute(
        """SELECT status,side,tx_hash,chain_id,stable
           FROM mininex_live_evm_auto_broadcasts
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,execution_id))
    order=cur.fetchone()
    if (type(order) is not dict
            or order.get("status")!="FINALIZED_SUCCESS"
            or order.get("side")!="SELL"
            or order.get("tx_hash")!=source_tx_hash
            or order.get("chain_id")!=_pair(
                asset,order.get("stable"),"SELL")["chain_id"]):
        raise HTTPException(409,"Only finalized owner SELL swap may enter HWM")
    # A journal FINALIZED_SUCCESS row alone is not sufficient. Only a
    # separately stored, receipt-verified SELL fill belonging to this exact
    # owner, asset, intent, chain and hash can enter the accounting path.
    cur.execute(
        """SELECT chain_id,tx_hash,side,stable,source_amount,
                  destination_amount,network_fee_native
           FROM mininex_live_evm_verified_fills
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,execution_id))
    fill=cur.fetchone()
    if (type(fill) is not dict
            or fill.get("chain_id")!=order["chain_id"]
            or fill.get("tx_hash")!=source_tx_hash
            or fill.get("side")!="SELL"
            or fill.get("stable")!=order["stable"]
            or any(fill.get(field) is None for field in (
                "source_amount","destination_amount","network_fee_native"))
            or any(Decimal(str(fill[field]))<=0 for field in (
                "source_amount","destination_amount"))
            or Decimal(str(fill["network_fee_native"]))<0):
        raise HTTPException(409,"Finalized EVM SELL token fill not verified")
    # Native EVM SELL is not economically settled by its Router02 swap
    # alone. Require the separately verified preceding WETH9-style DEPOSIT
    # and completed native cycle under the same owner, chain and signal.
    if asset in ("BNB","POL","AVAX"):
        cur.execute(
            """SELECT swap_tx_hash,conversion_tx_hash,chain_id,side,stable
               FROM mininex_live_evm_completed_native_cycles
               WHERE privy_did=%s AND asset=%s AND intent_key=%s
               FOR UPDATE""",(did,asset,execution_id))
        cycle=cur.fetchone()
        if (type(cycle) is not dict
                or cycle.get("swap_tx_hash")!=source_tx_hash
                or cycle.get("chain_id")!=order["chain_id"]
                or cycle.get("side")!="SELL"
                or cycle.get("stable")!=order["stable"]
                or type(cycle.get("conversion_tx_hash")) is not str
                or not _HASH.fullmatch(cycle["conversion_tx_hash"])
                or cycle["conversion_tx_hash"]==source_tx_hash):
            raise HTTPException(
                409,"Complete finalized native EVM SELL cycle required")
    cur.execute(
        """INSERT INTO mininex_evm_asset_hwm(privy_did,asset)
           VALUES (%s,%s) ON CONFLICT(privy_did,asset) DO NOTHING""",
        (did,asset))
    cur.execute(
        """SELECT cumulative_realized_net_usd,high_water_mark_usd
           FROM mininex_evm_asset_hwm
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",
        (did,asset))
    row=cur.fetchone()
    if (type(row) is not dict
            or row.get("cumulative_realized_net_usd") is None
            or row.get("high_water_mark_usd") is None):
        raise HTTPException(503,"Persistent EVM asset HWM not available")
    cur.execute(
        """SELECT realized_net_delta_usd,fee_assessed_usd,
                  cumulative_realized_net_usd,high_water_mark_usd,
                  source_tx_hash
           FROM mininex_evm_fee_events
           WHERE privy_did=%s AND asset=%s AND execution_id=%s""",
        (did,asset,execution_id))
    prev=cur.fetchone()
    if prev is not None:
        if (type(prev) is not dict
                or Decimal(str(prev["realized_net_delta_usd"]))!=delta
                or prev["source_tx_hash"]!=source_tx_hash):
            raise HTTPException(409,"Conflicting immutable EVM profit event")
        return {
            "duplicate":True,
            "asset":asset,"source_tx_hash":source_tx_hash,
            "realized_net_usd":str(delta),
            "fee_assessed_usd":str(prev["fee_assessed_usd"]),
            "high_water_mark_usd":str(prev["high_water_mark_usd"]),
            "cumulative_realized_net_usd":str(prev["cumulative_realized_net_usd"]),
            "fee_token_transferred":False,
        }
    assessment=assess_realized_gain(
        row["cumulative_realized_net_usd"],
        row["high_water_mark_usd"],delta)
    cur.execute(
        """INSERT INTO mininex_evm_fee_events
           (privy_did,asset,execution_id,realized_net_delta_usd,
            cumulative_realized_net_usd,high_water_mark_usd,fee_rate,
            fee_assessed_usd,source_tx_hash)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (did,asset,execution_id,delta,assessment.cumulative_usd,
         assessment.high_water_mark_usd,RATE,assessment.fee_assessed_usd,
         source_tx_hash))
    cur.execute(
        """UPDATE mininex_evm_asset_hwm
           SET cumulative_realized_net_usd=%s,high_water_mark_usd=%s,
               total_fees_assessed_usd=total_fees_assessed_usd+%s,
               updated_at=now()
           WHERE privy_did=%s AND asset=%s""",
        (assessment.cumulative_usd,assessment.high_water_mark_usd,
         assessment.fee_assessed_usd,did,asset))
    if assessment.fee_assessed_usd>0:
        cur.execute(
            """INSERT INTO mininex_evm_fee_obligations
               (privy_did,asset,execution_id,fee_due_usd)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT(privy_did,asset,execution_id) DO NOTHING""",
            (did,asset,execution_id,assessment.fee_assessed_usd))
    return {
        "duplicate":False,"asset":asset,
        "source_tx_hash":source_tx_hash,
        "realized_net_usd":str(delta),
        "fee_assessed_usd":str(assessment.fee_assessed_usd),
        "high_water_mark_usd":str(assessment.high_water_mark_usd),
        "cumulative_realized_net_usd":str(assessment.cumulative_usd),
        "fee_token_transferred":False,
    }


def record_internal_evm_realized_close(
    *,did:str,asset:str,intent_key:str,
    source_tx_hash:str,realized_net_usd:object,
)->dict:
    """Only a trusted reconciler can provide true net USD with full FIFO costs.

    This function is intentionally not connected to client routes or paper
    sessions; the missing on-chain cost-basis reconciler cannot be bypassed.
    """
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _assess_locked(
                    cur,did=did,asset=asset,execution_id=key,
                    source_tx_hash=source_tx_hash,
                    realized_net_usd=realized_net_usd)
    except psycopg.Error:
        LOG.warning("EVM performance fee persistence unavailable")
        raise HTTPException(503,"Trusted EVM HWM persistence unavailable") from None
