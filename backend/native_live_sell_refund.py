"""Original BTC/SOL/XRP SELL refund proof, not a swap fill.

Only an independently finalized source-chain transfer back to the ORIGINAL
Privy-linked source wallet can change a failed cross-chain SELL to REFUNDED.
A THORNode refund claim is not a blockchain transfer. No sign, send or retry.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import re

from fastapi import HTTPException
import httpx
import psycopg
from psycopg.rows import dict_row

from .blockchain_preview import BTC_RE
from .live_btc_receipts import check_native_btc_transaction
from .native_coin_balances import _valid_address, _rpc_url
from .native_live_buy_destination import _verified_native_credit
from .native_thor_swap_quote import _exact_owner_wallets
from .native_thor_transaction_status import (
    _VALID_INTENT, get_private_original_protocol_status,
)
from .native_trade_receipts import _valid_native_hash
from .native_trx_doge_buy_receipts import get_original_trx_doge_receipt
from .privy_wallet_verification import fetch_privy_user
from .storage import _db_url, _ensure_schema, _safe_did

_ASSETS=frozenset(("BTC","SOL","XRP","TRX"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_BTC_HASH=re.compile(r"^[0-9a-f]{64}$")
_AMOUNT=re.compile(r"^[1-9][0-9]{0,35}$")


def inspect_original_native_sell_refund(
    original:object, protocol:object, receipt:object,
)->dict:
    """Prove one native refund, distinct from a successful stablecoin SELL."""
    if (type(original) is not dict or type(protocol) is not dict
            or original.get("asset") not in _ASSETS
            or original.get("side")!="SELL"
            or original.get("status")!="SOURCE_FINALIZED"
            or type(original.get("privy_did")) is not str
            or type(original.get("intent_key")) is not str
            or not _VALID_INTENT.fullmatch(original["intent_key"])
            or type(original.get("payload_sha256")) is not str
            or not _SHA.fullmatch(original["payload_sha256"])
            or type(original.get("source_proof_sha256")) is not str
            or not _SHA.fullmatch(original["source_proof_sha256"])
            or type(original.get("source_wallet")) is not str):
        raise ValueError("Original native SELL source not independently finalized")
    asset=original["asset"]
    wallet=original["source_wallet"]
    if (not BTC_RE.fullmatch(wallet) if asset=="BTC"
            else not _valid_address(asset,wallet)):
        raise ValueError("Original refund wallet invalid")
    if (protocol.get("mode")!="THORCHAIN_ORIGINAL_TRANSACTION_READ_ONLY"
            or protocol.get("asset")!=asset
            or protocol.get("side")!="SELL"
            or protocol.get("source_tx_hash")!=original.get("source_tx_hash")
            or protocol.get("source_wallet_original") is not True
            or protocol.get("destination_owner_bound") is not True
            or protocol.get("protocol_inbound_observed") is not True
            or protocol.get("protocol_inbound_finalized") is not True
            or protocol.get("refund_original_source_chain")!=("TRON" if asset=="TRX" else asset)
            or protocol.get("refund_independently_finalized") is not False
            or protocol.get("destination_chain_receipts_verified") is not False
            or protocol.get("realized_pnl_verified") is not False
            or protocol.get("fee_assessed") is not False
            or protocol.get("retry_permitted") is not False
            or protocol.get("new_transactions_sent")!=0):
        raise ValueError("Protocol refund does not belong to original sender")
    payouts=protocol.get("protocol_outbound_hashes")
    hashes=protocol.get("protocol_refund_hashes")
    amounts=protocol.get("protocol_refund_units_1e8")
    if (type(payouts) is not list or payouts
            or type(hashes) is not list or len(hashes)!=1
            or type(amounts) is not list or len(amounts)!=1
            or type(amounts[0]) is not str or not _AMOUNT.fullmatch(amounts[0])):
        # Mixed partial payouts/refunds require a separate multi-leg accounting.
        raise ValueError("Cannot infer complete refund from partial protocol outbounds")
    hash_=hashes[0]
    if not (_BTC_HASH.fullmatch(hash_) if asset in ("BTC","TRX")
            else _valid_native_hash(asset,hash_)):
        raise ValueError("Invalid native original refund hash")
    units=int(amounts[0])
    if asset=="BTC":
        atomic=units
    elif asset=="SOL":
        atomic=units*10
    else:
        if units%100:
            raise ValueError("THORChain XRP/TRX refund has fractional native units")
        atomic=units//100
    if not 0<atomic<10**30:
        raise ValueError("Impossible native refund amount")
    checked=_verified_native_credit(asset,wallet,hash_,atomic,receipt)
    fingerprint=[
        original["privy_did"],asset,original["intent_key"],
        original["payload_sha256"],original["source_proof_sha256"],
        original["source_tx_hash"],wallet,hash_,str(atomic),
        checked["chain_height"],checked["chain_finality"],
    ]
    digest=hashlib.sha256(json.dumps(
        fingerprint,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "mode":"ORIGINAL_NATIVE_SELL_REFUND_CHAIN_FINALITY",
        "asset":asset,"side":"SELL",
        "intent_key":original["intent_key"],
        "original_order_sha256":original["payload_sha256"],
        "original_source_proof_sha256":original["source_proof_sha256"],
        "source_tx_hash":original["source_tx_hash"],
        "refund_tx_hash":hash_,
        "refund_wallet_original":True,
        "refund_amount_atomic":str(atomic),
        "refund_amount_native":format(
            Decimal(atomic)/Decimal(10**(8 if asset=="BTC" else 9 if asset=="SOL" else 6)),"f"),
        "refund_chain_checkpoint":checked["chain_height"],
        "refund_chain_finality":checked["chain_finality"],
        "refund_proof_sha256":digest,
        "refund_independently_finalized":True,
        "native_sell_settlement_complete":False,
        "realized_profit_verified":False,
        "fee_assessed":False,
        "new_transactions_sent":0,
        "retry_permitted":False,"can_submit":False,
    }


async def get_private_original_native_sell_refund(
    did:str,*,asset:str,intent_key:str,
)->dict:
    """Owner-authenticated chain read; no web-based write or transaction."""
    did=_safe_did(did)
    if (asset not in _ASSETS or type(intent_key) is not str
            or not _VALID_INTENT.fullmatch(intent_key)):
        raise HTTPException(422,"Invalid original native refund reference")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT privy_did,asset,intent_key,payload_sha256,
                          source_proof_sha256,source_tx_hash,
                          source_wallet,destination_wallet,side,status
                   FROM mininex_live_native_order_intents
                   WHERE privy_did=%s AND asset=%s AND intent_key=%s""",
                (did,asset,intent_key))
            row=cur.fetchone()
    if (type(row) is not dict or row.get("privy_did")!=did
            or row.get("asset")!=asset or row.get("intent_key")!=intent_key
            or row.get("side")!="SELL" or row.get("status")!="SOURCE_FINALIZED"):
        raise HTTPException(409,"Original native refund owner record unavailable")
    user=await fetch_privy_user(did)
    try:
        native_owner,eth_owner=_exact_owner_wallets(user,did,asset)
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"Original Privy refund ownership unavailable") from None
    if (row["source_wallet"]!=native_owner
            or row["destination_wallet"].lower()!=eth_owner.lower()):
        raise HTTPException(409,"Original native refund wallet has changed")
    protocol=await get_private_original_protocol_status(
        did,asset=asset,intent_key=intent_key)
    hashes=protocol.get("protocol_refund_hashes")
    if (type(hashes) is not list or len(hashes)!=1
            or protocol.get("protocol_outbound_hashes")!=[]):
        raise HTTPException(409,"No single original protocol refund found")
    hash_=hashes[0]
    if asset=="BTC":
        receipt=await check_native_btc_transaction(did,hash_)
    elif asset=="TRX":
        try:
            receipt=await get_original_trx_doge_receipt(asset,hash_)
        except (ValueError,TypeError,KeyError,httpx.HTTPError):
            raise HTTPException(503,"TRON refund chain proof unavailable") from None
    else:
        if asset=="SOL":
            payload={
                "jsonrpc":"2.0","id":1,"method":"getTransaction",
                "params":[hash_,{"encoding":"jsonParsed","commitment":"finalized",
                                  "maxSupportedTransactionVersion":0}],
            }
        else:
            payload={"method":"tx","params":[
                {"transaction":hash_,"binary":False}]}
        try:
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12),follow_redirects=False) as client:
                response=await client.post(
                    _rpc_url(asset),json=payload,
                    headers={"Content-Type":"application/json",
                             "Accept":"application/json"})
                response.raise_for_status()
                if len(response.content)>250_000:
                    raise ValueError("Oversized native refund transaction")
                receipt=response.json()
        except (ValueError,TypeError,httpx.HTTPError):
            raise HTTPException(503,"Refund chain proof unavailable") from None
    try:
        return inspect_original_native_sell_refund(row,protocol,receipt)
    except (ValueError,TypeError,KeyError,OverflowError):
        raise HTTPException(
            503,"Original refund not independently finalized") from None


def _persist_original_native_sell_refund_locked(
    cur,*,original:dict,verified:dict,
)->str:
    """Internal one-time REFUNDED_VERIFIED; never turns refund into gain."""
    if (type(original) is not dict or type(verified) is not dict
            or original.get("asset") not in _ASSETS
            or original.get("side")!="SELL"
            or original.get("status")!="SOURCE_FINALIZED"
            or verified.get("mode")!="ORIGINAL_NATIVE_SELL_REFUND_CHAIN_FINALITY"
            or verified.get("asset")!=original.get("asset")
            or verified.get("side")!="SELL"
            or verified.get("intent_key")!=original.get("intent_key")
            or verified.get("original_order_sha256")!=original.get("payload_sha256")
            or verified.get("original_source_proof_sha256")!=original.get("source_proof_sha256")
            or verified.get("source_tx_hash")!=original.get("source_tx_hash")
            or verified.get("refund_wallet_original") is not True
            or verified.get("refund_independently_finalized") is not True
            or verified.get("native_sell_settlement_complete") is not False
            or verified.get("realized_profit_verified") is not False
            or verified.get("fee_assessed") is not False
            or verified.get("new_transactions_sent")!=0
            or verified.get("retry_permitted") is not False
            or verified.get("can_submit") is not False):
        raise HTTPException(409,"Refund proof not tied to original native source")
    did=_safe_did(original["privy_did"])
    asset=original["asset"]
    key=original["intent_key"]
    digest=verified.get("refund_proof_sha256")
    tx=verified.get("refund_tx_hash")
    amount=verified.get("refund_amount_atomic")
    if (type(digest) is not str or not _SHA.fullmatch(digest)
            or type(tx) is not str
            or not (_BTC_HASH.fullmatch(tx) if asset in ("BTC","TRX")
                    else _valid_native_hash(asset,tx))
            or type(amount) is not str or not _AMOUNT.fullmatch(amount)):
        raise ValueError("Invalid immutable original refund proof")
    cur.execute(
        """SELECT status,side,source_wallet,source_tx_hash,
                  payload_sha256,source_proof_sha256,
                  destination_tx_hash,destination_proof_sha256
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
           FOR UPDATE""",(did,asset,key))
    state=cur.fetchone()
    if (type(state) is not dict or state.get("status")!="SOURCE_FINALIZED"
            or state.get("side")!="SELL"
            or state.get("source_wallet")!=original.get("source_wallet")
            or state.get("source_tx_hash")!=original.get("source_tx_hash")
            or state.get("payload_sha256")!=original.get("payload_sha256")
            or state.get("source_proof_sha256")!=original.get("source_proof_sha256")
            or state.get("destination_tx_hash") is not None
            or state.get("destination_proof_sha256") is not None):
        raise HTTPException(409,"Native refund is already settled or changed")
    cur.execute(
        """UPDATE mininex_live_native_order_intents
           SET status='REFUNDED_VERIFIED',destination_tx_hash=%s,
               destination_proof_sha256=%s,destination_received_atomic=%s,
               destination_finalized_at=now(),updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status='SOURCE_FINALIZED' AND side='SELL'
             AND source_tx_hash=%s AND source_proof_sha256=%s
             AND destination_proof_sha256 IS NULL RETURNING status""",
        (tx,digest,amount,did,asset,key,
         original["source_tx_hash"],original["source_proof_sha256"]))
    result=cur.fetchone()
    if type(result) is not dict or result.get("status")!="REFUNDED_VERIFIED":
        raise HTTPException(409,"Native refund persistence conflict")
    return "REFUNDED_VERIFIED"
