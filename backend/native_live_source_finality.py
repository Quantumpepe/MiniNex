"""Passive owner-bound BTC/SOL/XRP original-source finality.

Reads original Privy wallet identity and confirmed source-chain transactions,
then records the exact immutable source proof alongside a SUBMITTED journal
row. This is NOT a swap output, destination receipt, position, or 5% fee.
Never sends, retries, approves, signs or creates a user order.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import logging
from threading import Lock

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row

from .storage import _db_url,_ensure_schema,_safe_did
from .native_live_order_journal import _valid_source_hash
from .native_thor_swap_quote import _exact_owner_wallets
from .native_live_eth_buy_source import get_original_eth_buy_source
from .privy_wallet_verification import fetch_privy_user
from .native_trade_receipts import get_owner_native_sender_receipt
from .live_btc_receipts import check_native_btc_transaction

LOG=logging.getLogger("mininex.native_source_finality")
_FINALITY_PAGE_LOCK=Lock()
# Separate source/unknown/ETH BUY keyset cursors. Finished rows disappear
# from the SQL predicate, so OFFSET pagination would skip later users.
_RECEIPT_PAGE_CURSORS={"sell":None,"unknown":None,"buy":None}


def _native_source_receipt_work_page(
    *,where_sql:str,kind:str,limit:int,lock:Lock,
)->list[dict]:
    """Keyset scan against mutable receipt statuses, never a trade cap."""
    if kind not in _RECEIPT_PAGE_CURSORS or type(limit) is not int or not 0<limit<=256:
        raise ValueError("Invalid original native receipt page")
    db=_db_url()
    _ensure_schema(db)
    suffix=""" ORDER BY created_at ASC,privy_did,asset,intent_key LIMIT %s"""
    after=""" AND (created_at,privy_did,asset,intent_key)>(%s,%s,%s,%s)"""
    with lock:
        previous=_RECEIPT_PAGE_CURSORS[kind]
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(where_sql+(after if previous else "")+suffix,
                            (*previous,limit) if previous else (limit,))
                rows=list(cur.fetchall())
                if not rows and previous:
                    cur.execute(where_sql+suffix,(limit,))
                    rows=list(cur.fetchall())
        if rows:
            last=rows[-1]
            stamp=last.get("created_at")
            did=last.get("privy_did")
            asset=last.get("asset")
            key=last.get("intent_key")
            if (not isinstance(stamp,datetime) or stamp.tzinfo is None
                    or type(did) is not str or type(asset) is not str
                    or type(key) is not str):
                raise ValueError("Original native receipt key not verifiable")
            _RECEIPT_PAGE_CURSORS[kind]=(stamp,did,asset,key)
        else:
            _RECEIPT_PAGE_CURSORS[kind]=None
    return rows
_CHAIN={"BTC":"bitcoin-mainnet","SOL":"solana-mainnet","XRP":"xrpl-mainnet"}


def _confirmed_original_source(record:dict,proof:dict,owner_wallet:str)->dict:
    """Exact source-chain finality; never infer swap output or profit."""
    if (type(record) is not dict or type(proof) is not dict
            or record.get("status")!="SUBMITTED"
            or record.get("side")!="SELL"
            or record.get("asset") not in _CHAIN
            or record.get("chain")!=_CHAIN[record["asset"]]
            or record.get("source_wallet")!=owner_wallet
            or type(record.get("source_tx_hash")) is not str
            or not _valid_source_hash(record["asset"],"SELL",
                                      record["source_tx_hash"])):
        raise ValueError("Original source chain identity not verified")
    asset=record["asset"]
    tx=record["source_tx_hash"]
    if asset=="BTC":
        if (proof.get("mode")!="BTC_MAINNET_WALLET_TX_OBSERVATION"
                or proof.get("asset")!="BTC"
                or proof.get("network")!="bitcoin-mainnet"
                or proof.get("txid")!=tx.lower()
                or proof.get("owned_address_proved_by_privy") is not True
                or proof.get("owner_input_observed") is not True
                or proof.get("explorer_reports_confirmed") is not True
                or proof.get("recommended_confirmation_threshold_met") is not True
                or type(proof.get("confirmations_observed")) is not int
                or proof["confirmations_observed"]<6
                or proof.get("crosschain_swap_fill_verified") is not False
                or proof.get("ethereum_stablecoin_settlement_verified") is not False
                or proof.get("wallet_signed") is not False
                or proof.get("can_submit") is not False):
            raise ValueError("Bitcoin source transaction is not finalized")
        checkpoint={"confirmations":proof["confirmations_observed"],
                    "wallet_input_sats":proof.get("wallet_input_sats"),
                    "wallet_output_sats":proof.get("wallet_output_sats"),
                    "network_fee_btc":proof.get("transaction_fee_btc")}
    else:
        correct=(proof.get("source_wallet_signature_verified") is True if asset=="SOL"
                 else proof.get("source_wallet_transaction_verified") is True)
        checkpoint_value=(
            proof.get("source_chain_slot") if asset=="SOL" else
            proof.get("source_chain_ledger"))
        if (not correct or proof.get("mode")!="OWNER_NATIVE_SOURCE_RECEIPT_READ_ONLY"
                or proof.get("asset")!=asset
                or proof.get("network")!=_CHAIN[asset]
                or proof.get("source_tx_hash")!=tx
                or proof.get("source_chain_finalized") is not True
                or type(checkpoint_value) is not int or checkpoint_value<=0
                or proof.get("destination_finality_still_required") is not True
                or proof.get("cross_chain_swap_completed") is not False
                or proof.get("destination_stablecoin_received") is not False
                or proof.get("trading_profit_recorded") is not False
                or proof.get("fee_assessed") is not False
                or proof.get("can_submit") is not False):
            raise ValueError("Native source receipt does not verify original payment")
        fee=("network_fee_lamports" if asset=="SOL" else "network_fee_drops")
        network_fee=proof.get(fee)
        if (type(network_fee) is not str or
                not network_fee.isdigit() or len(network_fee)>30):
            raise ValueError("Source-chain fee cannot be reconstructed")
        checkpoint={
            "chain_height":checkpoint_value,
            "network_fee_atomic":network_fee,
        }
    record_fields={
        "mode":"EXACT_OWNER_NATIVE_SOURCE_FINALITY_ONLY",
        "asset":asset,"network":_CHAIN[asset],"side":"SELL",
        "intent_key":record.get("intent_key"),
        "source_tx_hash":tx,"source_wallet":owner_wallet,
        "owner_privy_did":record.get("privy_did"),
        "original_order_sha256":record.get("payload_sha256"),
        "checkpoint":checkpoint,
        "source_chain_finalized":True,
        "destination_settlement_finalized":False,
        "net_profit_verified":False,
        "fee_assessed":False,
        "broadcast_or_retry_performed":False,
    }
    commitment=hashlib.sha256(json.dumps(
        record_fields,sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    return {"source_proof_sha256":commitment,**record_fields}


def _pending_original_source_submissions(limit:int=32)->list[dict]:
    """Original submitted native SELL source receipts, fair under mutations."""
    query="""SELECT privy_did,asset,intent_key,payload_sha256,side,
                    chain,source_wallet,source_tx_hash,status,created_at
             FROM mininex_live_native_order_intents
             WHERE status='SUBMITTED' AND side='SELL'
               AND asset IN ('BTC','SOL','XRP')
               AND source_tx_hash IS NOT NULL"""
    return _native_source_receipt_work_page(
        where_sql=query,kind="sell",limit=limit,lock=_FINALITY_PAGE_LOCK)


def _persist_source_finality_locked(
    cur,*,original:dict,verified:dict,
)->str:
    """One linearized transition; original chain hash never changes."""
    side=original.get("side") if type(original) is dict else None
    asset=original.get("asset") if type(original) is dict else None
    if side not in ("SELL","BUY") or asset not in _CHAIN:
        raise HTTPException(409,"Wrong original native source side")
    if side=="BUY" and (
            type(verified) is not dict
            or verified.get("mode")!="OWNER_ORIGINAL_ETH_BUY_SOURCE_FINALITY"
            or verified.get("side")!="BUY"
            or verified.get("trusted_thorchain_deposit_verified") is not True
            or verified.get("destination_wallet")!=original.get("destination_wallet")
            or verified.get("source_stable")!=original.get("stable")
            or verified.get("source_stable_atomic") is None):
        raise HTTPException(409,"Original ETH BUY source not verified")
    if (type(original) is not dict or type(verified) is not dict
            or type(verified) is not dict
            or verified.get("source_chain_finalized") is not True
            or verified.get("destination_settlement_finalized") is not False
            or verified.get("net_profit_verified") is not False
            or verified.get("fee_assessed") is not False
            or verified.get("broadcast_or_retry_performed") is not False
            or verified.get("asset")!=original.get("asset")
            or verified.get("source_tx_hash")!=original.get("source_tx_hash")
            or verified.get("original_order_sha256")!=original.get("payload_sha256")
            or verified.get("source_wallet")!=original.get("source_wallet")
            or verified.get("owner_privy_did")!=original.get("privy_did")):
        raise HTTPException(409,"Original source finality not bound to journal")
    did=_safe_did(original["privy_did"])
    asset=original["asset"]
    key=original["intent_key"]
    digest=verified.get("source_proof_sha256")
    if (type(digest) is not str or len(digest)!=64 or
            any(c not in "0123456789abcdef" for c in digest)):
        raise ValueError("Invalid original chain proof digest")
    cur.execute(
        """SELECT status,side,chain,source_wallet,source_tx_hash,
                  payload_sha256,source_proof_sha256
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
           FOR UPDATE""",(did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict or row.get("status")!="SUBMITTED"
            or row.get("side")!=side
            or row.get("chain")!=("ethereum-mainnet" if side=="BUY" else _CHAIN.get(asset))
            or row.get("source_wallet")!=original.get("source_wallet")
            or row.get("source_tx_hash")!=original.get("source_tx_hash")
            or row.get("payload_sha256")!=original.get("payload_sha256")
            or row.get("source_proof_sha256") is not None):
        raise HTTPException(409,"Native source already claimed or changed")
    cur.execute(
        """UPDATE mininex_live_native_order_intents
           SET status='SOURCE_FINALIZED',
               source_proof_sha256=%s,source_finalized_at=now(),
               updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status='SUBMITTED' AND source_tx_hash=%s
             AND source_proof_sha256 IS NULL
           RETURNING status""",
        (digest,did,asset,key,original["source_tx_hash"]))
    result=cur.fetchone()
    if type(result) is not dict or result.get("status")!="SOURCE_FINALIZED":
        raise HTTPException(409,"Native original receipt persistence conflict")
    return "SOURCE_FINALIZED"



def _persist_independent_source_evidence(
    original:dict,evidence:dict,*,unknown:bool=False,
)->str:
    """Write actual owner source evidence on a worker thread, never broadcast."""
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            callback=(_persist_unknown_native_finality_locked if unknown else
                      _persist_source_finality_locked)
            return callback(cur,original=original,verified=evidence)


async def _reconcile_owned_source_rows(rows:list[dict],inspect)->tuple[int,int]:
    """Different wallet owners progress together; original owner stays serial.

    Prevent a delayed native explorer response from blocking all other users.
    Work-page size is only a temporary receipt scan, not a trading limit.
    """
    owners:dict[str,list[dict]]={}
    unavailable=0
    for original in rows:
        try:
            if type(original) is not dict:
                raise ValueError("Malformed original native receipt job")
            did=_safe_did(original["privy_did"])
            owners.setdefault(did,[]).append(original)
        except (KeyError,TypeError,ValueError):
            unavailable+=1

    async def one_owner(items:list[dict])->tuple[int,int]:
        good=bad=0
        for original in items:
            try:
                await inspect(original)
                good+=1
            except asyncio.CancelledError:
                raise
            except (HTTPException,ValueError,TypeError,KeyError,psycopg.Error,
                    OverflowError):
                bad+=1
        return good,bad
    completed=await asyncio.gather(*(one_owner(items) for items in owners.values()))
    return sum(count[0] for count in completed), (
        unavailable+sum(count[1] for count in completed))


async def reconcile_original_native_source_once(limit:int=32)->dict:
    """Passive source receipts concurrently by owner, no signature or retry."""
    rows=await asyncio.to_thread(_pending_original_source_submissions,limit)
    async def inspect(original:dict):
        did=_safe_did(original["privy_did"])
        asset=original["asset"]
        user=await fetch_privy_user(did)
        owned,_eth=_exact_owner_wallets(user,did,asset)
        if owned!=original["source_wallet"]:
            raise ValueError("Original Privy wallet changed")
        hash_=original["source_tx_hash"]
        proof=(await check_native_btc_transaction(did,hash_) if asset=="BTC"
               else await get_owner_native_sender_receipt(
                   did,asset=asset,transaction_hash=hash_))
        evidence=_confirmed_original_source(original,proof,owned)
        await asyncio.to_thread(_persist_independent_source_evidence,
                                original,evidence)
    done,errors=await _reconcile_owned_source_rows(rows,inspect)
    return {
        "mode":"NATIVE_ORIGINAL_SOURCE_READ_ONLY_RECONCILIATION",
        "scanned":len(rows),"finalized_sources":done,
        "unverified_or_unavailable":errors,
        "destination_settlements_confirmed":0,
        "realized_profit_assessed":False,
        "fees_paid":False,"new_transactions_sent":0,
    }

# A timed-out network submission OR a server crash between the durable
# SENDING commit and recording the network answer may already be confirmed.
# The immutable signed tx ID is committed before any network POST. Recover
# either state by READ-ONLY source-chain finality; never sign or rebroadcast.
_UNKNOWN_SIGNED_PAGE_LOCK=Lock()


def _pending_unknown_signed_sources(limit:int=32)->list[dict]:
    # Avoid racing an active source broadcaster (HTTP timeout 12 seconds):
    # only SENDING rows stranded for 60 seconds may enter passive recovery.
    query="""SELECT privy_did,asset,intent_key,payload_sha256,side,
                    chain,source_wallet,source_tx_hash,
                    source_signed_tx_hash,status,created_at
             FROM mininex_live_native_order_intents
             WHERE (status='RECONCILIATION_REQUIRED'
                    OR (status='SENDING'
                        AND updated_at < now() - interval '60 seconds'))
               AND side='SELL' AND asset IN ('BTC','SOL','XRP')
               AND source_tx_hash IS NULL
               AND source_signed_tx_hash IS NOT NULL"""
    return _native_source_receipt_work_page(
        where_sql=query,kind="unknown",limit=limit,
        lock=_UNKNOWN_SIGNED_PAGE_LOCK)


def _persist_unknown_native_finality_locked(
    cur,*,original:dict,verified:dict,
)->str:
    """Atomic SENDING/UNKNOWN -> FINALIZED only on independent chain proof."""
    asset=original.get("asset") if type(original) is dict else None
    hash_=original.get("source_signed_tx_hash") if type(original) is dict else None
    if (asset not in _CHAIN
            or original.get("side")!="SELL"
            or original.get("status") not in (
                "SENDING","RECONCILIATION_REQUIRED")
            or original.get("source_tx_hash") is not None
            or type(hash_) is not str
            or not _valid_source_hash(asset,"SELL",hash_)
            or verified.get("source_chain_finalized") is not True
            or verified.get("source_tx_hash")!=hash_):
        raise HTTPException(409,"Unknown original source not chain-confirmed")
    did=_safe_did(original["privy_did"])
    key=original["intent_key"]
    # The expected state is bound, never interpolated into SQL. If the
    # original sender concurrently saved SUBMITTED, let its normal finality
    # worker finish instead of changing a now-stale row.
    expected_state=original["status"]
    cur.execute(
        """UPDATE mininex_live_native_order_intents
           SET status='SUBMITTED',source_tx_hash=source_signed_tx_hash,
               updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status=%s
             AND side='SELL' AND source_tx_hash IS NULL
             AND source_signed_tx_hash=%s
             AND payload_sha256=%s RETURNING status""",
        (did,asset,key,expected_state,hash_,original["payload_sha256"]),
    )
    row=cur.fetchone()
    if type(row) is not dict or row.get("status")!="SUBMITTED":
        raise HTTPException(409,"Unknown source chain identity changed")
    promoted={**original,"status":"SUBMITTED","source_tx_hash":hash_}
    return _persist_source_finality_locked(
        cur,original=promoted,verified=verified)


async def reconcile_unknown_original_native_signed_sources_once(limit:int=32)->dict:
    """Original signed source receipt lookup only; never retry the network."""
    originals=await asyncio.to_thread(_pending_unknown_signed_sources,limit)
    async def inspect(original:dict):
        did=_safe_did(original["privy_did"])
        asset=original["asset"]
        signed_hash=original["source_signed_tx_hash"]
        if not _valid_source_hash(asset,"SELL",signed_hash):
            raise ValueError("Original signed hash invalid")
        user=await fetch_privy_user(did)
        owned,_eth=_exact_owner_wallets(user,did,asset)
        if owned!=original["source_wallet"]:
            raise ValueError("Original native owner was changed")
        proof=(await check_native_btc_transaction(did,signed_hash)
               if asset=="BTC" else
               await get_owner_native_sender_receipt(
                   did,asset=asset,transaction_hash=signed_hash))
        provisional={**original,"status":"SUBMITTED",
                     "source_tx_hash":signed_hash}
        verified=_confirmed_original_source(provisional,proof,owned)
        await asyncio.to_thread(_persist_independent_source_evidence,
                                original,verified,unknown=True)
    finalized,unresolved=await _reconcile_owned_source_rows(originals,inspect)
    return {
        "mode":"UNKNOWN_ORIGINAL_NATIVE_SIGNED_SOURCE_READ_ONLY_RECOVERY",
        "scanned":len(originals),
        "finalized_without_resend":finalized,
        "unresolved_without_resend":unresolved,
        "new_signatures_created":0,
        "new_transactions_sent":0,
        "source_resubmission_permitted":False,
        "destination_settlement_finalized":False,
        "fees_collected":False,
    }

# Separate Ethereum-stablecoin BUY scan shares only the query helper.


def _pending_original_eth_buy_submissions(limit:int=32)->list[dict]:
    query="""SELECT privy_did,asset,intent_key,payload_sha256,
                    side,stable,chain,input_amount,source_wallet,
                    destination_wallet,source_tx_hash,status,created_at
             FROM mininex_live_native_order_intents
             WHERE status='SUBMITTED' AND side='BUY'
               AND asset IN ('BTC','SOL','XRP')
               AND source_tx_hash IS NOT NULL"""
    return _native_source_receipt_work_page(
        where_sql=query,kind="buy",limit=limit,lock=_FINALITY_PAGE_LOCK)


async def reconcile_original_eth_buy_sources_once(limit:int=32)->dict:
    """Owner-independent Ethereum BUY receipt reads, never new sends."""
    rows=await asyncio.to_thread(_pending_original_eth_buy_submissions,limit)
    async def inspect(original:dict):
        did=_safe_did(original["privy_did"])
        proof=await get_original_eth_buy_source(
            did,asset=original["asset"],intent_key=original["intent_key"])
        await asyncio.to_thread(_persist_independent_source_evidence,
                                original,proof)
    verified_count,unverified=await _reconcile_owned_source_rows(rows,inspect)
    return {
        "mode":"NATIVE_ETH_BUY_SOURCE_READ_ONLY_RECONCILIATION",
        "scanned":len(rows),"verified_eth_source":verified_count,
        "unverified_or_unavailable":unverified,
        "destination_settlements_confirmed":0,
        "realized_profit_assessed":False,"fees_paid":False,
        "new_transactions_sent":0,
    }
