"""Passively close original native BTC/SOL/XRP BUY, SELL and refund cycles.

A source-chain receipt alone is not a trade fill. Once the original source
was independently finalized, inspect the real outbound chain and update
the original immutable order only on confirmed owner-wallet delivery or
confirmed owner-wallet refund. No signer, broadcast, new order or fee here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import re
from threading import Lock

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .native_live_buy_refund import (
    get_private_original_native_buy_refund,
    _persist_original_native_buy_refund_locked,
)
from .native_live_buy_destination import (
    get_private_original_native_buy_destination,
    _persist_original_native_buy_locked,
)
from .native_live_destination_finality import (
    get_private_native_ethereum_destination,
    _persist_native_destination_locked,
)
from .native_live_sell_refund import (
    get_private_original_native_sell_refund,
    _persist_original_native_sell_refund_locked,
)
from .storage import _db_url, _ensure_schema, _safe_did

LOG = logging.getLogger("mininex.native_original_destination_finality")
_ASSETS = frozenset(("BTC", "SOL", "XRP", "TRX", "DOGE"))
_KEY = re.compile(r"^native_[0-9a-f]{64}$")
_WORK_PAGE_LOCK = Lock()
_WORK_PAGE_CURSOR: tuple[datetime,str,str,str]|None = None


def original_native_sources_awaiting_destination(limit: int = 24) -> list[dict]:
    """Fair keyset pages across already-finalized original native sources.

    The worker's own successful settlements remove rows from this filtered
    table. OFFSET would skip newly shifted rows after each processed page,
    sometimes holding real owner payouts for multiple extra polls.
    Cursor pagination follows immutable (finalized time, owner, asset, key).
    This is not a trading, wallet, order-count or daily usage limit.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid original native destination work page")
    global _WORK_PAGE_CURSOR
    db = _db_url()
    _ensure_schema(db)
    head = """SELECT privy_did,asset,intent_key,side,stable,
                     source_wallet,destination_wallet,source_tx_hash,
                     payload_sha256,source_proof_sha256,status,
                     source_finalized_at
              FROM mininex_live_native_order_intents
              WHERE status='SOURCE_FINALIZED' AND side IN ('BUY','SELL')
                AND asset IN ('BTC','SOL','XRP','TRX','DOGE')
                AND source_proof_sha256 IS NOT NULL
                AND source_tx_hash IS NOT NULL
                AND source_finalized_at IS NOT NULL"""
    tail = """ ORDER BY source_finalized_at ASC,privy_did,asset,intent_key
               LIMIT %s"""
    after = """ AND (source_finalized_at,privy_did,asset,intent_key)
                 > (%s,%s,%s,%s)"""
    with _WORK_PAGE_LOCK:
        previous = _WORK_PAGE_CURSOR
        with psycopg.connect(db, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    head + (after if previous is not None else "") + tail,
                    (*previous, limit) if previous is not None else (limit,),
                )
                page = list(cur.fetchall())
                if not page and previous is not None:
                    # End of the immutable key range: revisit older rows
                    # whose outbound is still genuinely pending.
                    cur.execute(head + tail, (limit,))
                    page = list(cur.fetchall())
        if page:
            last = page[-1]
            stamp = last.get("source_finalized_at")
            did = last.get("privy_did")
            asset = last.get("asset")
            key = last.get("intent_key")
            if (not isinstance(stamp, datetime) or stamp.tzinfo is None
                    or type(did) is not str or type(asset) is not str
                    or type(key) is not str or not _KEY.fullmatch(key)):
                raise ValueError("Original native source paging key unverified")
            _WORK_PAGE_CURSOR = (stamp, did, asset, key)
        else:
            _WORK_PAGE_CURSOR = None
    return page


def _persist_original_verified_destination(*, original: dict,
                                          proof: dict, kind: str) -> str:
    """Persist only a verified original output under a fresh SQL row lock."""
    if (type(original) is not dict or type(proof) is not dict
            or original.get("asset") not in _ASSETS
            or original.get("status") != "SOURCE_FINALIZED"
            or (original.get("asset") =="DOGE" and
                original.get("side")!="BUY")
            or type(original.get("intent_key")) is not str
            or not _KEY.fullmatch(original["intent_key"])
            or kind not in ("BUY", "SELL", "REFUND", "BUY_REFUND")):
        raise ValueError("Original native destination cannot be trusted")
    if ((kind in ("BUY","BUY_REFUND") and original.get("side") != "BUY")
            or (kind in ("SELL","REFUND") and original.get("side") != "SELL")):
        raise ValueError("Original native destination side changed")
    _safe_did(original["privy_did"])
    db = _db_url()
    _ensure_schema(db)
    with psycopg.connect(db, connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if kind == "BUY":
                return _persist_original_native_buy_locked(
                    cur, original=original, verified=proof)
            if kind == "BUY_REFUND":
                return _persist_original_native_buy_refund_locked(
                    cur, original=original, verified=proof)
            if kind == "REFUND":
                return _persist_original_native_sell_refund_locked(
                    cur, original=original, verified=proof)
            return _persist_native_destination_locked(
                cur, original=original, verified=proof)


async def reconcile_original_native_destinations_once(
    limit: int = 24,
) -> dict:
    """Reconcile completed source trades to actual payouts or native refunds.

    Entirely passive. Original BUY/SELL/refund getters independently re-read
    both Privy wallet ownership and canonical outbound blockchain finality.
    Unavailable protocol/chain data never changes the original journal.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid native destination work page")
    rows = await asyncio.to_thread(original_native_sources_awaiting_destination,
                                   limit)
    result = {
        "scanned": len(rows),
        "destination_finalized": 0,
        "refunds_verified": 0,
        "unverified_or_unavailable": 0,
        "new_transactions_sent": 0,
        "fees_paid": False,
    }
    by_owner: dict[str, list[dict]] = {}
    for row in rows:
        try:
            if (type(row) is not dict or row.get("asset") not in _ASSETS
                    or row.get("status") != "SOURCE_FINALIZED"
                    or row.get("side") not in ("BUY", "SELL")
                    or (row.get("asset") =="DOGE" and
                        row.get("side")!="BUY")
                    or type(row.get("intent_key")) is not str
                    or not _KEY.fullmatch(row["intent_key"])):
                raise ValueError("Unverified original native source row")
            by_owner.setdefault(_safe_did(row["privy_did"]), []).append(row)
        except (ValueError, TypeError, KeyError):
            result["unverified_or_unavailable"] += 1

    async def finalize_owner(originals: list[dict]) -> dict:
        totals = {"destination_finalized": 0, "refunds_verified": 0,
                  "unverified_or_unavailable": 0}
        for row in originals:
            did, asset, key = row["privy_did"], row["asset"], row["intent_key"]
            try:
                if row["side"] == "BUY":
                    try:
                        proof = await get_private_original_native_buy_destination(
                            did, asset=asset, intent_key=key)
                    except asyncio.CancelledError:
                        raise
                    except (HTTPException, ValueError, TypeError, KeyError):
                        proof = None
                    if proof is not None:
                        status = await asyncio.to_thread(
                            _persist_original_verified_destination,
                            original=row, proof=proof, kind="BUY")
                        if status != "DESTINATION_FINALIZED":
                            raise ValueError("Original BUY destination not finalized")
                        totals["destination_finalized"] += 1
                    else:
                        proof = await get_private_original_native_buy_refund(
                            did, asset=asset, intent_key=key)
                        status = await asyncio.to_thread(
                            _persist_original_verified_destination,
                            original=row, proof=proof, kind="BUY_REFUND")
                        if status != "REFUNDED_VERIFIED":
                            raise ValueError("Original BUY stablecoin refund not finalized")
                        totals["refunds_verified"] += 1
                    continue
                # SELL can either pay the original Ethereum stablecoin wallet
                # or return the original native asset. Both outcomes require
                # an independently confirmed real receiving-chain transfer.
                try:
                    proof = await get_private_native_ethereum_destination(
                        did, asset=asset, intent_key=key)
                except asyncio.CancelledError:
                    raise
                except (HTTPException, ValueError, TypeError, KeyError):
                    proof = None
                if proof is not None:
                    status = await asyncio.to_thread(
                        _persist_original_verified_destination,
                        original=row, proof=proof, kind="SELL")
                    if status != "DESTINATION_FINALIZED":
                        raise ValueError("Original SELL destination not finalized")
                    totals["destination_finalized"] += 1
                else:
                    proof = await get_private_original_native_sell_refund(
                        did, asset=asset, intent_key=key)
                    status = await asyncio.to_thread(
                        _persist_original_verified_destination,
                        original=row, proof=proof, kind="REFUND")
                    if status != "REFUNDED_VERIFIED":
                        raise ValueError("Original SELL refund not finalized")
                    totals["refunds_verified"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                # No retry or new source transaction. Another poll may have a
                # finalized outbound, or an operator may inspect a conflict.
                totals["unverified_or_unavailable"] += 1
                LOG.debug("Original native output awaiting independent finality",
                          exc_info=True)
        return totals

    completed = await asyncio.gather(*(
        finalize_owner(originals) for originals in by_owner.values()))
    for owner_total in completed:
        for k, v in owner_total.items():
            result[k] += v
    return result
