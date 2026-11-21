"""Owner-bound BTC/SOL/XRP original native journal visibility.

Show real immutable source/destination transaction states per selected coin,
never PAPER, an estimated market quote, invented PnL or an AUTO permission.
Read-only, no wallet signing or broadcasting, even on an unsettled source.
"""
from __future__ import annotations

from datetime import datetime,timezone
from decimal import Decimal
import re

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row

from .native_live_order_journal import _valid_source_hash
from .storage import _db_url,_ensure_schema,_safe_did

_ASSETS=frozenset(("BTC","SOL","XRP"))
_STATES=frozenset(("PREPARED","SENDING","SUBMITTED",
    "RECONCILIATION_REQUIRED","SOURCE_FINALIZED",
    "DESTINATION_FINALIZED","REFUNDED_VERIFIED","CANCELLED"))
_KEY=re.compile(r"^native_[0-9a-f]{64}$")
_DIGEST=re.compile(r"^[0-9a-f]{64}$")
_ETH_HASH=re.compile(r"^0x[a-fA-F0-9]{64}$")
_NATIVE_HASH=re.compile(r"^[A-Za-z0-9]{64,90}$")


def _original_native_order_row(row:object,asset:str)->dict:
    if type(row) is not dict or row.get("asset")!=asset:
        raise ValueError("Wrong original user native order")
    side,state,stable=row.get("side"),row.get("status"),row.get("stable")
    chain=("ethereum-mainnet" if side=="BUY" else {
        "BTC":"bitcoin-mainnet","SOL":"solana-mainnet","XRP":"xrpl-mainnet",
    }[asset])
    known_hash=row.get("source_tx_hash")
    signed_hash=row.get("source_signed_tx_hash")
    outbound=row.get("destination_tx_hash")
    finalized=state in ("DESTINATION_FINALIZED","REFUNDED_VERIFIED")
    sent=state in ("SUBMITTED","SOURCE_FINALIZED",
                   "DESTINATION_FINALIZED","REFUNDED_VERIFIED")
    when=row.get("created_at")
    updated=row.get("updated_at")
    if (side not in ("BUY","SELL")
            or stable not in ("USDC","USDT")
            or (asset=="XRP" and stable!="USDC")
            or state not in _STATES
            or row.get("chain")!=chain
            or type(row.get("intent_key")) is not str
            or not _KEY.fullmatch(row["intent_key"])
            or type(row.get("budget_revision")) is not int
            or row["budget_revision"]<0
            or not isinstance(when,datetime) or when.tzinfo is None
            or not isinstance(updated,datetime) or updated.tzinfo is None
            or (known_hash is not None and
                not _valid_source_hash(asset,side,known_hash))
            or (signed_hash is not None and
                not _valid_source_hash(asset,"SELL",signed_hash))
            or (sent and known_hash is None)
            or (finalized and (outbound is None
                or type(row.get("destination_proof_sha256")) is not str
                or not _DIGEST.fullmatch(row["destination_proof_sha256"])))
            or (not finalized and outbound is not None)
            or (outbound is not None
                and not ((_ETH_HASH.fullmatch(outbound) if side=="SELL" and state=="DESTINATION_FINALIZED"
                    else (_ETH_HASH.fullmatch(outbound) if side=="BUY" and state=="REFUNDED_VERIFIED"
                    else _NATIVE_HASH.fullmatch(outbound)))))
            or (finalized and row.get("destination_received_atomic") is None)):
        raise ValueError("Unverified original native order progress")
    raw=row.get("input_amount")
    try:
        amount=Decimal(str(raw))
        if not amount.is_finite() or amount<=0:
            raise ValueError("Unverified original native source amount")
    except (TypeError,ValueError,ArithmeticError):
        raise ValueError("Unverified original native source amount") from None
    received=row.get("destination_received_atomic")
    if finalized:
        try:
            decimal_amount=Decimal(str(received))
            if (not decimal_amount.is_finite() or decimal_amount<=0
                    or decimal_amount!=decimal_amount.to_integral_value()):
                raise ValueError("Invalid finalized original payout")
        except (TypeError,ValueError,ArithmeticError):
            raise ValueError("Invalid finalized original payout") from None
    target=(
        asset if side=="BUY" else stable
    ) if state=="DESTINATION_FINALIZED" else (
        stable if side=="BUY" else asset
    ) if state=="REFUNDED_VERIFIED" else None
    return {
        "asset":asset,"intent_key":row["intent_key"],"side":side,
        "stable":stable,"source_chain":chain,
        "status":state,"budget_revision":row["budget_revision"],
        "original_input_amount":format(amount,"f"),
        "original_input_asset":stable if side=="BUY" else asset,
        "source_tx_hash":known_hash,
        "signed_source_hash_known":signed_hash is not None,
        "destination_tx_hash":outbound,
        "destination_amount_atomic":str(received) if finalized else None,
        "destination_asset":target,
        "source_blockchain_finalized":state in (
            "SOURCE_FINALIZED","DESTINATION_FINALIZED","REFUNDED_VERIFIED"),
        "destination_wallet_credited":state=="DESTINATION_FINALIZED",
        "original_funds_refunded":state=="REFUNDED_VERIFIED",
        "pending_or_uncertain":state in (
            "SENDING","SUBMITTED","RECONCILIATION_REQUIRED",
            "SOURCE_FINALIZED"),
        "automatic_retry_allowed":False,
        "realized_net_pnl_usd":None,
        "performance_fee_assessed":False,
        "wallet_transaction_sent_by_this_api":False,
        "created_at":when.astimezone(timezone.utc).isoformat(),
        "updated_at":updated.astimezone(timezone.utc).isoformat(),
    }


def get_owner_native_live_order_status(
    did:str,*,asset:str,limit:int=20,
)->dict:
    did=_safe_did(did)
    if asset not in _ASSETS:
        raise HTTPException(404,"No native order history for this coin")
    if type(limit) is not int or not 1<=limit<=50:
        raise HTTPException(422,"Invalid original native history page")
    try:
        db=_db_url()
        _ensure_schema(db)
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT asset,side,stable,intent_key,status,chain,
                              budget_revision,input_amount,
                              source_tx_hash,source_signed_tx_hash,
                              destination_tx_hash,destination_proof_sha256,
                              destination_received_atomic,
                              created_at,updated_at
                       FROM mininex_live_native_order_intents
                       WHERE privy_did=%s AND asset=%s
                       ORDER BY created_at DESC,intent_key DESC LIMIT %s""",
                    (did,asset,limit+1))
                rows=list(cur.fetchall())
    except psycopg.Error:
        raise HTTPException(503,"Original native order journal unavailable") from None
    verified=[_original_native_order_row(row,asset) for row in rows[:limit]]
    return {
        "mode":"OWNER_ORIGINAL_NATIVE_ORDER_HISTORY_ONLY",
        "asset":asset,"orders":verified,
        "history_truncated":len(rows)>limit,
        "includes_simulation":False,"wallet_signing_authorized_by_this_api":False,
        "automatic_execution_enabled_by_this_api":False,
    }
