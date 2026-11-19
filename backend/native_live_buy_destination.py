"""Original BTC/SOL/XRP BUY delivery, independently checked on native chain.

Never equate a THORChain outbound status with an owner payout: the actual
destination transaction must be verified by the native blockchain. This module
never signs, broadcasts, retries, calculates profit, or charges an owner fee.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
import re

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .blockchain_preview import BTC_RE
from .live_btc_receipts import check_native_btc_transaction
from .native_coin_balances import _rpc_url,_valid_address
from .native_thor_swap_quote import _exact_owner_wallets
from .native_thor_transaction_status import (
    _VALID_INTENT,get_private_original_protocol_status,
)
from .native_trade_receipts import _valid_native_hash
from .privy_wallet_verification import fetch_privy_user
from .storage import _db_url,_ensure_schema,_safe_did

_ASSETS=frozenset(("BTC","SOL","XRP"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_DECIMAL=re.compile(r"^[1-9][0-9]{0,34}$")
_BTC_HASH=re.compile(r"^[a-f0-9]{64}$")


def _verified_native_credit(
    asset:str,wallet:str,hash_:str,expected_atomic:int,receipt:object,
)->dict:
    """Parse a finalized native transfer to exactly the owner's own account."""
    if (asset not in _ASSETS or type(wallet) is not str
            or type(expected_atomic) is not int or expected_atomic<=0):
        raise ValueError("Unsupported original native destination")
    if asset=="BTC":
        if (not BTC_RE.fullmatch(wallet)
                or not _BTC_HASH.fullmatch(hash_)):
            raise ValueError("Wrong original Bitcoin wallet or hash")
        r=receipt
        if (type(r) is not dict or r.get("mode")!="BTC_MAINNET_WALLET_TX_OBSERVATION"
                or r.get("asset")!="BTC" or r.get("network")!="bitcoin-mainnet"
                or r.get("txid")!=hash_
                or r.get("owned_address_proved_by_privy") is not True
                or r.get("owner_input_observed") is not False
                or r.get("owner_output_observed") is not True
                or r.get("explorer_reports_confirmed") is not True
                or r.get("recommended_confirmation_threshold_met") is not True
                or type(r.get("confirmations_observed")) is not int
                or r["confirmations_observed"]<6
                or r.get("wallet_input_sats")!="0"
                or r.get("wallet_output_sats")!=str(expected_atomic)
                or r.get("crosschain_swap_fill_verified") is not False
                or r.get("ethereum_stablecoin_settlement_verified") is not False
                or r.get("execution_enabled") is not False
                or r.get("can_submit") is not False):
            raise ValueError("Bitcoin original inbound transfer not proven")
        return {"chain_height":None,"chain_finality":"SIX_CONFIRMED",
                "credited_atomic":str(expected_atomic)}
    if asset=="SOL":
        if (not _valid_address("SOL",wallet)
                or not _valid_native_hash("SOL",hash_)
                or type(receipt) is not dict
                or receipt.get("jsonrpc")!="2.0" or receipt.get("id")!=1
                or "error" in receipt):
            raise ValueError("Wrong original Solana owner or response")
        r=receipt.get("result")
        if (type(r) is not dict or type(r.get("slot")) is not int
                or r["slot"]<=0):
            raise ValueError("Solana finalized slot unavailable")
        tx=r.get("transaction")
        meta=r.get("meta")
        if (type(tx) is not dict or type(meta) is not dict
                or "err" not in meta or meta["err"] is not None
                or type(tx.get("signatures")) is not list
                or hash_ not in tx["signatures"]):
            raise ValueError("Solana original destination transaction unverified")
        message=tx.get("message")
        if type(message) is not dict:
            raise ValueError("Solana decoded message unavailable")
        keys=message.get("accountKeys")
        pre=meta.get("preBalances")
        post=meta.get("postBalances")
        instructions=message.get("instructions")
        if (type(keys) is not list or type(pre) is not list
                or type(post) is not list or len(keys)!=len(pre)
                or len(keys)!=len(post) or type(instructions) is not list
                or len(instructions)!=1
                or type(meta.get("fee")) is not int or meta["fee"]<0):
            raise ValueError("Unverified Solana destination account delta")
        positions=[
            index for index,key in enumerate(keys)
            if type(key) is dict and key.get("pubkey")==wallet
            and key.get("signer") is False and key.get("writable") is True
        ]
        if len(positions)!=1 or positions[0]==0:
            raise ValueError("Solana owner destination was not independent payee")
        i=positions[0]
        if (type(pre[i]) is not int or type(post[i]) is not int
                or pre[i]<0 or post[i]<pre[i]
                or post[i]-pre[i]!=expected_atomic):
            raise ValueError("Exact Solana owner credit not established")
        ins=instructions[0]
        parsed=ins.get("parsed") if type(ins) is dict else None
        info=parsed.get("info") if type(parsed) is dict else None
        if (type(ins) is not dict or ins.get("program")!="system"
                or type(parsed) is not dict or parsed.get("type")!="transfer"
                or type(info) is not dict
                or info.get("destination")!=wallet
                or type(info.get("source")) is not str
                or info["source"]==wallet
                or type(info.get("lamports")) is not int
                or info["lamports"]!=expected_atomic):
            raise ValueError("Native Solana system transfer to owner unavailable")
        return {"chain_height":r["slot"],"chain_finality":"FINALIZED",
                "credited_atomic":str(expected_atomic)}
    if (not _valid_address("XRP",wallet)
            or not _valid_native_hash("XRP",hash_)
            or type(receipt) is not dict):
        raise ValueError("Wrong original XRP owner or transaction hash")
    r=receipt.get("result")
    if (type(r) is not dict or r.get("validated") is not True
            or type(r.get("ledger_index")) is not int
            or r["ledger_index"]<=0):
        raise ValueError("XRPL original destination not validated")
    tx=r.get("tx_json",r)
    meta=r.get("meta")
    if (type(tx) is not dict or type(meta) is not dict
            or str(tx.get("hash",r.get("hash",""))).upper()!=hash_.upper()
            or tx.get("TransactionType")!="Payment"
            or tx.get("Destination")!=wallet
            or type(tx.get("Account")) is not str
            or tx["Account"]==wallet
            or meta.get("TransactionResult")!="tesSUCCESS"
            or type(tx.get("Fee")) is not str
            or not re.fullmatch(r"[0-9]{1,20}",tx["Fee"])
            or type(tx.get("Flags",0)) is not int
            or tx.get("Flags",0)!=0):
        raise ValueError("Unverified original XRP payment to owner")
    delivered=meta.get("delivered_amount")
    if (type(delivered) is not str
            or not _DECIMAL.fullmatch(delivered)
            or delivered!=str(expected_atomic)
            or tx.get("Amount")!=delivered):
        raise ValueError("Exact validated XRP delivered drops unavailable")
    return {"chain_height":r["ledger_index"],"chain_finality":"VALIDATED",
            "credited_atomic":delivered}


def inspect_original_native_buy_destination(
    original:object,protocol:object,receipt:object,
)->dict:
    if (type(original) is not dict or type(protocol) is not dict
            or original.get("asset") not in _ASSETS
            or original.get("side")!="BUY"
            or original.get("status")!="SOURCE_FINALIZED"
            or type(original.get("intent_key")) is not str
            or not _VALID_INTENT.fullmatch(original["intent_key"])
            or type(original.get("payload_sha256")) is not str
            or not _SHA.fullmatch(original["payload_sha256"])
            or type(original.get("source_proof_sha256")) is not str
            or not _SHA.fullmatch(original["source_proof_sha256"])
            or type(original.get("privy_did")) is not str
            or (not BTC_RE.fullmatch(original.get("destination_wallet",""))
                if original["asset"]=="BTC" else
                not _valid_address(original["asset"],original.get("destination_wallet")))):
        raise ValueError("Original BUY source or owner wallet not proven")
    asset=original["asset"]
    if (protocol.get("mode")!="THORCHAIN_ORIGINAL_TRANSACTION_READ_ONLY"
            or protocol.get("asset")!=asset or protocol.get("side")!="BUY"
            or protocol.get("source_tx_hash")!=original.get("source_tx_hash")
            or protocol.get("source_wallet_original") is not True
            or protocol.get("destination_owner_bound") is not True
            or protocol.get("protocol_inbound_observed") is not True
            or protocol.get("protocol_inbound_finalized") is not True
            or protocol.get("protocol_reports_swap_finalized") is not True
            or protocol.get("protocol_refund_hashes",[])!=[]
            or protocol.get("destination_chain_receipts_verified") is not False
            or protocol.get("realized_pnl_verified") is not False
            or protocol.get("fee_assessed") is not False
            or protocol.get("retry_permitted") is not False
            or protocol.get("new_transactions_sent")!=0):
        raise ValueError("Original THORChain BUY not finalized")
    hashes=protocol.get("protocol_outbound_hashes")
    amounts=protocol.get("protocol_outbound_units_1e8")
    if (type(hashes) is not list or len(hashes)!=1
            or type(amounts) is not list or len(amounts)!=1
            or type(amounts[0]) is not str
            or not _DECIMAL.fullmatch(amounts[0])):
        raise ValueError("Single original native outbound unavailable")
    quantity=int(amounts[0])
    if asset=="BTC":
        expected=quantity
    elif asset=="SOL":
        expected=quantity*10
    else:
        if quantity%100:
            raise ValueError("THORChain XRP output has sub-drop precision")
        expected=quantity//100
    receipt_proof=_verified_native_credit(
        asset,original["destination_wallet"],hashes[0],expected,receipt)
    fields=[
        original["privy_did"],asset,original["intent_key"],
        original["payload_sha256"],original["source_proof_sha256"],
        original["source_tx_hash"],hashes[0],
        original["destination_wallet"],receipt_proof["chain_height"],
        str(expected),
    ]
    digest=hashlib.sha256(json.dumps(
        fields,sort_keys=False,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "mode":"OWNER_NATIVE_BUY_DESTINATION_FINALITY",
        "asset":asset,"side":"BUY",
        "intent_key":original["intent_key"],
        "original_order_sha256":original["payload_sha256"],
        "original_source_proof_sha256":original["source_proof_sha256"],
        "source_tx_hash":original["source_tx_hash"],
        "destination_tx_hash":hashes[0],
        "destination_wallet_original":True,
        "destination_received_atomic":str(expected),
        "destination_received_amount":format(
            Decimal(expected)/Decimal(
                10**(8 if asset=="BTC" else 9 if asset=="SOL" else 6)),"f",
        ),
        "destination_chain_checkpoint":receipt_proof["chain_height"],
        "destination_chain_finality":receipt_proof["chain_finality"],
        "destination_proof_sha256":digest,
        "native_buy_settlement_complete":True,
        "native_sell_settlement_complete":False,
        "trading_profit_verified":False,"fee_assessed":False,
        "retry_permitted":False,"new_transactions_sent":0,
        "can_submit":False,
    }


def _read_original_buy_locked(cur,*,did:str,asset:str,key:str)->dict:
    cur.execute(
        """SELECT privy_did,asset,intent_key,payload_sha256,
                  source_proof_sha256,source_tx_hash,
                  source_wallet,destination_wallet,stable,side,status
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s""",
        (did,asset,key),
    )
    row=cur.fetchone()
    if (type(row) is not dict or row.get("privy_did")!=did
            or row.get("asset")!=asset or row.get("intent_key")!=key
            or row.get("side")!="BUY"
            or row.get("status")!="SOURCE_FINALIZED"):
        raise HTTPException(409,"Original native BUY source is not finalized")
    return row


async def get_private_original_native_buy_destination(
    did:str,*,asset:str,intent_key:str,
)->dict:
    """Read-only, owner-authenticated finalized destination inspection."""
    did=_safe_did(did)
    if (asset not in _ASSETS or type(intent_key) is not str
            or not _VALID_INTENT.fullmatch(intent_key)):
        raise HTTPException(422,"Invalid original native BUY reference")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            original=_read_original_buy_locked(
                cur,did=did,asset=asset,key=intent_key)
    user=await fetch_privy_user(did)
    try:
        native_owner,eth_owner=_exact_owner_wallets(user,did,asset)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Original Privy native BUY wallet unavailable") from None
    if (original["destination_wallet"]!=native_owner
            or original["source_wallet"].lower()!=eth_owner.lower()):
        raise HTTPException(409,"Original BUY source/destination wallet changed")
    protocol=await get_private_original_protocol_status(
        did,asset=asset,intent_key=intent_key)
    hashes=protocol.get("protocol_outbound_hashes")
    if type(hashes) is not list or len(hashes)!=1:
        raise HTTPException(409,"Original native outbound unavailable")
    hash_=hashes[0]
    if asset=="BTC":
        receipt=await check_native_btc_transaction(did,hash_)
    else:
        if asset=="SOL":
            payload={
                "jsonrpc":"2.0","id":1,"method":"getTransaction",
                "params":[hash_,{"encoding":"jsonParsed",
                                  "commitment":"finalized",
                                  "maxSupportedTransactionVersion":0}],
            }
        else:
            payload={"method":"tx","params":[
                {"transaction":hash_,"binary":False}]}
        try:
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12),follow_redirects=False) as client:
                resp=await client.post(
                    _rpc_url(asset),json=payload,
                    headers={"Accept":"application/json",
                             "Content-Type":"application/json"})
                resp.raise_for_status()
                if len(resp.content)>250_000:
                    raise ValueError("Oversized native chain outbound transaction")
                receipt=resp.json()
        except (ValueError,TypeError,KeyError,httpx.HTTPError):
            raise HTTPException(
                503,"Original native BUY receiving transaction unavailable") from None
    try:
        return inspect_original_native_buy_destination(original,protocol,receipt)
    except (ValueError,TypeError,KeyError,OverflowError):
        raise HTTPException(
            503,"Original native BUY payout not independently finalized") from None


def _persist_original_native_buy_locked(
    cur,*,original:dict,verified:dict,
)->str:
    """Persist BUY only after exact source, original THOR and native proof.

    This is an internal-only compare-and-swap operation. A BUY creates a
    verified cost/quantity basis, never realized profit or a 5% fee.
    """
    if (type(original) is not dict or type(verified) is not dict
            or original.get("asset") not in _ASSETS
            or original.get("side")!="BUY"
            or original.get("status")!="SOURCE_FINALIZED"
            or verified.get("mode")!="OWNER_NATIVE_BUY_DESTINATION_FINALITY"
            or verified.get("asset")!=original["asset"]
            or verified.get("side")!="BUY"
            or verified.get("intent_key")!=original.get("intent_key")
            or verified.get("original_order_sha256")!=original.get("payload_sha256")
            or verified.get("original_source_proof_sha256")!=original.get("source_proof_sha256")
            or verified.get("source_tx_hash")!=original.get("source_tx_hash")
            or verified.get("destination_wallet_original") is not True
            or verified.get("native_buy_settlement_complete") is not True
            or verified.get("native_sell_settlement_complete") is not False
            or verified.get("trading_profit_verified") is not False
            or verified.get("fee_assessed") is not False
            or verified.get("retry_permitted") is not False
            or verified.get("new_transactions_sent")!=0
            or verified.get("can_submit") is not False):
        raise HTTPException(409,"Native BUY payout is not proven for original owner")
    did=_safe_did(original["privy_did"])
    asset=original["asset"]
    key=original["intent_key"]
    tx=verified.get("destination_tx_hash")
    digest=verified.get("destination_proof_sha256")
    atomic=verified.get("destination_received_atomic")
    amount=verified.get("destination_received_amount")
    tx_is_valid=(type(tx) is str and
                 (_BTC_HASH.fullmatch(tx) if asset=="BTC"
                  else _valid_native_hash(asset,tx)))
    if (not tx_is_valid
            or type(digest) is not str or not _SHA.fullmatch(digest)
            or type(atomic) is not str or not _DECIMAL.fullmatch(atomic)
            or type(amount) is not str):
        raise ValueError("Cannot durably persist unverified native BUY credit")
    divisor=Decimal(10**(8 if asset=="BTC" else 9 if asset=="SOL" else 6))
    if amount!=format(Decimal(atomic)/divisor,"f"):
        raise ValueError("Native BUY atomic credit does not equal reported quantity")
    cur.execute(
        """SELECT status,side,stable,source_tx_hash,payload_sha256,
                  source_proof_sha256,source_wallet,destination_wallet,
                  destination_tx_hash,destination_proof_sha256
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
           FOR UPDATE""",(did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict or row.get("status")!="SOURCE_FINALIZED"
            or row.get("side")!="BUY"
            or row.get("stable")!=original.get("stable")
            or row.get("source_tx_hash")!=original["source_tx_hash"]
            or row.get("payload_sha256")!=original["payload_sha256"]
            or row.get("source_proof_sha256")!=original["source_proof_sha256"]
            or row.get("source_wallet")!=original["source_wallet"]
            or row.get("destination_wallet")!=original["destination_wallet"]
            or row.get("destination_tx_hash") is not None
            or row.get("destination_proof_sha256") is not None):
        raise HTTPException(409,"Native BUY order changed or was already completed")
    cur.execute(
        """UPDATE mininex_live_native_order_intents
           SET status='DESTINATION_FINALIZED',destination_tx_hash=%s,
               destination_proof_sha256=%s,destination_received_atomic=%s,
               destination_finalized_at=now(),updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND side='BUY' AND status='SOURCE_FINALIZED'
             AND source_tx_hash=%s AND source_proof_sha256=%s
             AND destination_proof_sha256 IS NULL
           RETURNING status""",
        (tx,digest,atomic,did,asset,key,
         original["source_tx_hash"],original["source_proof_sha256"]))
    saved=cur.fetchone()
    if (type(saved) is not dict
            or saved.get("status")!="DESTINATION_FINALIZED"):
        raise HTTPException(409,"Native BUY original finalization lost concurrency race")
    return "DESTINATION_FINALIZED"
