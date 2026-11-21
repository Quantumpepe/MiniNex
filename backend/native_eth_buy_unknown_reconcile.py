"""Recover an uncertain original Ethereum THORChain BUY by canonical source nonce.

This is passive only: no Privy signing, no transaction replay, no approval,
no new order and no request to the private swap venue. The original ETH
wallet nonce, original Privy RPC request digest, pre-send observed block and
Privy wallet owner are immutable. Scan finalized Ethereum blocks a few at
a time and only promote a source if its exact original signed request is
cryptographically reconstructed from the on-chain calldata.

A mismatched same-nonce transaction or a reverted original source remains
unresolved for explicit incident review; it is NEVER treated as a new trade,
and the source nonce is NEVER reused by the recovery worker.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import re
from threading import Lock

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_eth_receipts import _num
from .live_eth_quotes import STABLES
from .manual_swap_live_quotes import THOR_ASSETS
from .native_live_eth_buy_source import _read_deposit_call
from .native_live_source_finality import _native_source_receipt_work_page
from .native_thor_swap_quote import _exact_owner_wallets
from .privy_live_signer import _owned_eth_wallet
from .privy_wallet_verification import fetch_privy_user
from .shared_evm_execution import bound_evm_wallet_rpc
from .native_eth_nonce_binding import pin_original_privy_eth_nonce
from .storage import _db_url, _ensure_schema, _safe_did
from .wallet_balances import ETH_RPC

_ADDR=re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_SHA=re.compile(r"^[0-9a-f]{64}$")
_INTENT=re.compile(r"^native_[0-9a-f]{64}$")
_ASSETS=frozenset(("BTC","SOL","XRP","TRX","DOGE"))
_PAGE_LOCK=Lock()
_SCAN_BLOCKS_PER_TICK=3


def _whole(value:object)->int:
    """PostgreSQL NUMERIC / strict int; never float-round a source nonce."""
    if type(value) is int:
        result=value
    elif type(value) is Decimal and value.is_finite() and value==int(value):
        result=int(value)
    else:
        raise ValueError("Original ETH on-chain numeric reference not exact")
    if not 0<=result<2**256:
        raise ValueError("Original ETH on-chain numeric reference out of range")
    return result


def _original_unknown_eth_buy_row(row:object)->dict:
    if (type(row) is not dict or row.get("asset") not in _ASSETS
            or row.get("side")!="BUY"
            or row.get("status") not in ("SENDING","RECONCILIATION_REQUIRED")
            or row.get("chain")!="ethereum-mainnet"
            or row.get("stable") not in STABLES
            or (row["asset"]=="XRP" and row["stable"]!="USDC")
            or type(row.get("privy_did")) is not str
            or type(row.get("intent_key")) is not str
            or not _INTENT.fullmatch(row["intent_key"])
            or type(row.get("payload_sha256")) is not str
            or not _SHA.fullmatch(row["payload_sha256"])
            or type(row.get("source_rpc_request_sha256")) is not str
            or not _SHA.fullmatch(row["source_rpc_request_sha256"])
            or type(row.get("source_wallet")) is not str
            or not _ADDR.fullmatch(row["source_wallet"])
            or row.get("source_tx_hash") is not None
            or type(row.get("destination_wallet")) is not str):
        raise ValueError("Original uncertain ETH BUY journal invalid")
    nonce=_whole(row.get("source_eth_pending_nonce"))
    observed=_whole(row.get("source_eth_observed_block_number"))
    scanned=row.get("source_eth_scan_next_block")
    next_block=(_whole(scanned) if scanned is not None else observed)
    if not 0<observed<=next_block:
        raise ValueError("Original ETH BUY scan block is not forward-only")
    try:
        amount=Decimal(str(row.get("input_amount")))*Decimal(10**6)
        if (not amount.is_finite() or amount<=0
                or amount!=amount.to_integral_value()):
            raise ValueError("Original ETH BUY input not integral")
    except (ValueError,ArithmeticError,TypeError):
        raise ValueError("Original ETH BUY input unavailable") from None
    return {**row,"nonce_exact":nonce,"observed":observed,
            "next_block":next_block,"amount_atomic":int(amount)}


def _original_eth_buy_candidate(
    row:dict,tx:object,*,wallet_id:str,block_number:int,block_hash:str,
)->str|None:
    """Verify exact original Privy RPC digest against a real finalized block.

    A different owner nonce is unrelated and ignored. A SAME nonce with
    different calldata/router is a conflict; never advance the scan cursor.
    """
    original=_original_unknown_eth_buy_row(row)
    if type(tx) is not dict:
        raise ValueError("Ethereum transaction entry malformed")
    sender=tx.get("from")
    if type(sender) is not str or not _ADDR.fullmatch(sender):
        raise ValueError("Finalized Ethereum transaction sender absent")
    if sender.lower()!=original["source_wallet"].lower():
        return None
    nonce=_num(tx.get("nonce"))
    if nonce!=original["nonce_exact"]:
        return None
    if (type(tx.get("to")) is not str or not _ADDR.fullmatch(tx["to"])
            or _num(tx.get("chainId"))!=1 or _num(tx.get("value"))!=0
            or _num(tx.get("blockNumber"))!=block_number
            or str(tx.get("blockHash","")).lower()!=block_hash.lower()
            or type(tx.get("hash")) is not str
            or not _HASH.fullmatch(tx["hash"])
            or type(tx.get("input")) is not str):
        raise ValueError("Same-nonce Ethereum transaction not verifiable")
    deposit=_read_deposit_call(tx["input"])
    fragments=deposit["memo"].split(":")
    destination=fragments[2].split("/") if len(fragments)>=3 else []
    if (deposit["token"].lower()!=STABLES[original["stable"]].lower()
            or deposit["atomic"]!=original["amount_atomic"]
            or len(fragments)<3 or fragments[0].upper() not in ("=","S","SWAP")
            or fragments[1].upper()!=THOR_ASSETS[original["asset"]][0].upper()
            or len(destination)!=2
            or destination[0]!=original["destination_wallet"]
            or destination[1].lower()!=original["source_wallet"].lower()):
        raise ValueError("Source Ethereum nonce used by another deposit")
    # Use the same lowercase canonical router spelling that MiniNex
    # used to hash the originally claimed Privy RPC request.
    url,body=bound_evm_wallet_rpc(
        asset="ETH",wallet_id=wallet_id,wallet=original["source_wallet"],
        tx={"from":original["source_wallet"],"chainId":1,
            "to":tx["to"].lower(),"data":tx["input"].lower(),"value":"0"})
    body=pin_original_privy_eth_nonce(url,body,original["nonce_exact"])
    digest=hashlib.sha256(json.dumps(
        [url,body,original["payload_sha256"],original["nonce_exact"]],
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    if digest!=original["source_rpc_request_sha256"]:
        raise ValueError("Unknown Ethereum nonce differs from original Privy request")
    return tx["hash"].lower()


async def _read_only_original_eth_rpc(
    client:httpx.AsyncClient,method:str,params:list,req_id:int,
)->object:
    """Strict bounded original Ethereum mainnet RPC; never accepts write calls."""
    if (type(client) is not httpx.AsyncClient or
            method not in ("eth_chainId","eth_getBlockByNumber",
                           "eth_getTransactionReceipt")
            or type(params) is not list or type(req_id) is not int
            or not 0<req_id<10000):
        raise ValueError("Unexpected Ethereum source recovery RPC method")
    response=await client.post(ETH_RPC,json={
        "jsonrpc":"2.0","id":req_id,"method":method,"params":params,
    },headers={"Content-Type":"application/json","Accept":"application/json"})
    response.raise_for_status()
    # Full blocks include transaction calldata; bound memory and refuse
    # silently truncated blocks instead of skipping missed user funds.
    if len(response.content)>8_000_000:
        raise ValueError("Original Ethereum recovery block exceeds RPC bound")
    data=response.json()
    if (type(data) is not dict or data.get("jsonrpc")!="2.0"
            or data.get("id")!=req_id or "error" in data
            or "result" not in data):
        raise ValueError("Original Ethereum recovery RPC response invalid")
    return data["result"]


def _pending_original_unknown_eth_buy_sources(limit:int=8)->list[dict]:
    query="""SELECT privy_did,asset,side,stable,chain,source_wallet,
                    destination_wallet,intent_key,payload_sha256,input_amount,
                    status,source_tx_hash,source_eth_pending_nonce,
                    source_rpc_request_sha256,source_eth_observed_block_number,
                    source_eth_scan_next_block,created_at
             FROM mininex_live_native_order_intents
             WHERE (status='RECONCILIATION_REQUIRED'
                    OR (status='SENDING' AND
                        updated_at<now()-interval '60 seconds'))
               AND side='BUY' AND chain='ethereum-mainnet'
               AND source_tx_hash IS NULL
               AND source_eth_pending_nonce IS NOT NULL
               AND source_rpc_request_sha256 IS NOT NULL
               AND source_eth_observed_block_number IS NOT NULL"""
    return _native_source_receipt_work_page(
        where_sql=query,kind="unknown_buy",limit=limit,lock=_PAGE_LOCK)


def _persist_eth_recovery_cursor(row:dict,after:int)->bool:
    original=_original_unknown_eth_buy_row(row)
    if type(after) is not int or after<=original["next_block"]:
        raise ValueError("ETH recovery cursor must only move forward")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """UPDATE mininex_live_native_order_intents
                   SET source_eth_scan_next_block=%s,updated_at=now()
                   WHERE privy_did=%s AND asset=%s AND intent_key=%s
                     AND side='BUY' AND chain='ethereum-mainnet'
                     AND status=%s AND source_tx_hash IS NULL
                     AND payload_sha256=%s
                     AND source_eth_pending_nonce=%s
                     AND source_rpc_request_sha256=%s
                     AND source_eth_observed_block_number=%s
                     AND source_eth_scan_next_block IS NOT DISTINCT FROM %s
                   RETURNING source_eth_scan_next_block""",
                (after,original["privy_did"],original["asset"],
                 original["intent_key"],original["status"],
                 original["payload_sha256"],original["nonce_exact"],
                 original["source_rpc_request_sha256"],
                 original["observed"],row["source_eth_scan_next_block"]))
            return cur.fetchone() is not None


def _persist_original_recovered_eth_buy(
    row:dict,*,source_tx_hash:str,block_number:int,
)->bool:
    """Atomically recover only the original ETH BUY; no synthetic settlement."""
    original=_original_unknown_eth_buy_row(row)
    if (type(source_tx_hash) is not str
            or not re.fullmatch(r"0x[0-9a-f]{64}",source_tx_hash)
            or type(block_number) is not int
            or block_number<original["next_block"]):
        raise ValueError("Unknown original ETH source cannot be promoted")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """UPDATE mininex_live_native_order_intents
                   SET status='SUBMITTED',source_tx_hash=%s,
                       source_eth_scan_next_block=%s,updated_at=now()
                   WHERE privy_did=%s AND asset=%s AND intent_key=%s
                     AND side='BUY' AND chain='ethereum-mainnet'
                     AND status=%s AND source_tx_hash IS NULL
                     AND payload_sha256=%s
                     AND source_eth_pending_nonce=%s
                     AND source_rpc_request_sha256=%s
                     AND source_eth_observed_block_number=%s
                     AND source_eth_scan_next_block IS NOT DISTINCT FROM %s
                   RETURNING status""",
                (source_tx_hash,block_number+1,
                 original["privy_did"],original["asset"],
                 original["intent_key"],original["status"],
                 original["payload_sha256"],original["nonce_exact"],
                 original["source_rpc_request_sha256"],
                 original["observed"],row["source_eth_scan_next_block"]))
            updated=cur.fetchone()
            return type(updated) is dict and updated.get("status")=="SUBMITTED"


async def _recover_original_eth_buy_blocks(
    row:dict,*,wallet_id:str,client:httpx.AsyncClient,
)->dict:
    original=_original_unknown_eth_buy_row(row)
    chain,checkpoint=await asyncio.gather(
        _read_only_original_eth_rpc(client,"eth_chainId",[],11),
        _read_only_original_eth_rpc(client,"eth_getBlockByNumber",
                                   ["finalized",False],12))
    if _num(chain)!=1 or type(checkpoint) is not dict:
        raise ValueError("Original ETH BUY recovery not on finalized Ethereum mainnet")
    final_height=_num(checkpoint.get("number"))
    start=original["next_block"]
    if start>final_height:
        return {"state":"WAITING_FINALITY","scanned_blocks":0}
    stop=min(final_height+1,start+_SCAN_BLOCKS_PER_TICK)
    seen=None
    for block_number in range(start,stop):
        block=await _read_only_original_eth_rpc(
            client,"eth_getBlockByNumber",[hex(block_number),True],
            100+block_number%1000)
        if (type(block) is not dict
                or _num(block.get("number"))!=block_number
                or type(block.get("hash")) is not str
                or not _HASH.fullmatch(block["hash"])
                or type(block.get("transactions")) is not list
                or len(block["transactions"])>5000):
            raise ValueError("Original Ethereum block cannot be fully scanned")
        match=[]
        for tx in block["transactions"]:
            tx_hash=_original_eth_buy_candidate(
                original,tx,wallet_id=wallet_id,
                block_number=block_number,block_hash=block["hash"])
            if tx_hash is not None:
                match.append(tx_hash)
        if len(match)>1:
            raise ValueError("Ambiguous original ETH source nonce in finalized block")
        if match:
            tx_hash=match[0]
            receipt,canonical=await asyncio.gather(
                _read_only_original_eth_rpc(
                    client,"eth_getTransactionReceipt",[tx_hash],201),
                _read_only_original_eth_rpc(
                    client,"eth_getBlockByNumber",[hex(block_number),False],202))
            if (type(receipt) is not dict or type(canonical) is not dict
                    or str(canonical.get("hash","")).lower()!=block["hash"].lower()
                    or _num(canonical.get("number"))!=block_number
                    or str(receipt.get("transactionHash","")).lower()!=tx_hash
                    or str(receipt.get("blockHash","")).lower()!=block["hash"].lower()
                    or _num(receipt.get("blockNumber"))!=block_number):
                raise ValueError("Unverified original ETH canonical receipt")
            status=_num(receipt.get("status"))
            if status!=1:
                # A real reverted transaction consumes the source nonce, but
                # it is NOT a settled native asset. Do not report success.
                raise ValueError("Original Ethereum source finalized but reverted")
            promoted=await asyncio.to_thread(
                _persist_original_recovered_eth_buy,
                original,source_tx_hash=tx_hash,block_number=block_number)
            return {"state":"RECOVERED_ORIGINAL_ETH_SOURCE" if promoted
                    else "STALE_ORIGINAL_ETH_SOURCE",
                    "scanned_blocks":block_number-start+1}
        seen=block_number
    if seen is not None:
        advanced=await asyncio.to_thread(
            _persist_eth_recovery_cursor,original,after=seen+1)
        return {"state":"SCANNED_NO_MATCH" if advanced else "STALE_SCAN",
                "scanned_blocks":stop-start}
    return {"state":"WAITING_FINALITY","scanned_blocks":0}


async def reconcile_unknown_original_eth_buy_sources_once(limit:int=8)->dict:
    """Passive original-source recovery. Unresolved never triggers another send."""
    originals=await asyncio.to_thread(
        _pending_original_unknown_eth_buy_sources,limit)
    report={
        "mode":"ORIGINAL_NATIVE_ETH_BUY_UNKNOWN_SOURCE_READ_ONLY_RECOVERY",
        "scanned":len(originals),"recovered_buy_sources":0,
        "unresolved_without_resend":0,"new_transactions_sent":0,
        "new_signatures_created":0,"fees_paid":False,
        "source_resubmission_permitted":False,
    }
    for raw in originals:
        try:
            original=_original_unknown_eth_buy_row(raw)
            did=_safe_did(original["privy_did"])
            user=await fetch_privy_user(did)
            native,eth=_exact_owner_wallets(user,did,original["asset"])
            owned=_owned_eth_wallet(user,did)
            if (eth.lower()!=original["source_wallet"].lower()
                    or owned["address"]!=eth.lower()
                    or native!=original["destination_wallet"]):
                raise ValueError("Original owner wallet changed since ETH BUY")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10),follow_redirects=False,
            ) as client:
                checked=await _recover_original_eth_buy_blocks(
                    original,wallet_id=owned["wallet_id"],client=client)
            if checked["state"]=="RECOVERED_ORIGINAL_ETH_SOURCE":
                report["recovered_buy_sources"]+=1
            elif checked["state"]=="STALE_ORIGINAL_ETH_SOURCE":
                report["unresolved_without_resend"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            report["unresolved_without_resend"]+=1
    return report
