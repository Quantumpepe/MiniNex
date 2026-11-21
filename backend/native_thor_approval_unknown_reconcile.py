"""Passive recovery of an uncertain original THORChain ERC20 allowance send.

A Privy approval HTTP timeout can still mean an Ethereum transaction was
broadcast. The original owner nonce, pre-send Ethereum block, immutable Privy
RPC wire digest and exact finite token/router are stored before the first
request. Only a canonical finalized same-nonce matching approval is recovered.
No signing, rebroadcast, policy mutation, funds move, or fake swap fill.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
import re

import httpx
import psycopg
from psycopg.rows import dict_row

from .live_eth_quotes import STABLES
from .native_eth_buy_unknown_reconcile import (
    _whole, _read_only_original_eth_rpc, _SCAN_BLOCKS_PER_TICK,
)
from .privy_live_signer import _owned_eth_wallet
from .privy_wallet_verification import fetch_privy_user
from .shared_evm_allowance import owner_approval_draft
from .shared_evm_execution import bound_evm_wallet_rpc
from .storage import _db_url, _ensure_schema, _safe_did
from .live_eth_receipts import _num

_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_SHA=re.compile(r"^[0-9a-f]{64}$")
_ID=re.compile(r"^appr_[0-9a-f]{58}$")
_ASSETS=frozenset(("BTC","SOL","XRP","TRX","DOGE"))
_PENDING_OFFSET=0


def _original_unknown_thor_approval_row(row:object)->dict:
    if (type(row) is not dict or row.get("asset") not in _ASSETS
            or row.get("side")!="BUY"
            or row.get("stable") not in STABLES
            or (row["asset"]=="XRP" and row["stable"]!="USDC")
            or row.get("chain_id")!=1
            or row.get("approval_mode") not in ("THOR_FINITE","THOR_USDT_ZERO")
            or (row["approval_mode"]=="THOR_USDT_ZERO" and row["stable"]!="USDT")
            or row.get("status") not in ("SENDING","RECONCILIATION_REQUIRED")
            or row.get("tx_hash") is not None
            or type(row.get("wallet_address")) is not str
            or not re.fullmatch(r"0x[0-9a-f]{40}",row["wallet_address"])
            or type(row.get("token_address")) is not str
            or row["token_address"]!=STABLES[row["stable"]].lower()
            or type(row.get("router_address")) is not str
            or not re.fullmatch(r"0x[0-9a-f]{40}",row["router_address"])
            or type(row.get("wallet_id")) is not str
            or type(row.get("idempotency_key")) is not str
            or not _ID.fullmatch(row["idempotency_key"])
            or type(row.get("request_digest")) is not str
            or not _SHA.fullmatch(row["request_digest"])
            or type(row.get("owner_eth_request_wire_sha256")) is not str
            or not _SHA.fullmatch(row["owner_eth_request_wire_sha256"])):
        raise ValueError("Unknown original THORChain allowance journal invalid")
    did=_safe_did(row["privy_did"])
    nonce=_whole(row.get("owner_eth_pending_nonce"))
    observed=_whole(row.get("owner_eth_observed_block_number"))
    next_block=(_whole(row["owner_eth_scan_next_block"])
                if row.get("owner_eth_scan_next_block") is not None else observed)
    amount=_whole(row.get("amount_atomic"))
    if not 0<observed<=next_block or not 0<amount<10**45:
        raise ValueError("Original THOR approval scan height or amount invalid")
    return {**row,"privy_did":did,"nonce_exact":nonce,"observed":observed,
            "next_block":next_block,"exact_amount":amount}


def _original_thor_approval_candidate(
    row:dict,tx:object,*,block_number:int,block_hash:str,
)->str|None:
    original=_original_unknown_thor_approval_row(row)
    if type(tx) is not dict:
        raise ValueError("Malformed original THOR approval Ethereum transaction")
    owner=tx.get("from")
    if type(owner) is not str or not re.fullmatch(r"0x[0-9a-fA-F]{40}",owner):
        raise ValueError("Invalid original Ethereum approval transaction owner")
    if owner.lower()!=original["wallet_address"]:
        return None
    nonce=_num(tx.get("nonce"))
    if nonce!=original["nonce_exact"]:
        return None
    approved_value=(0 if original["approval_mode"]=="THOR_USDT_ZERO"
                    else original["exact_amount"])
    draft=owner_approval_draft(
        token=STABLES[original["stable"]],
        router=original["router_address"],
        wallet=original["wallet_address"],chain_id=1,
        amount_atomic=original["exact_amount"])
    expected_data=(draft["data"][:-64]+"0"*64 if approved_value==0
                   else draft["data"])
    if (str(tx.get("to","")).lower()!=original["token_address"]
            or str(tx.get("input","")).lower()!=expected_data.lower()
            or _num(tx.get("value"))!=0
            or _num(tx.get("chainId"))!=1
            or _num(tx.get("blockNumber"))!=block_number
            or str(tx.get("blockHash","")).lower()!=block_hash.lower()
            or type(tx.get("hash")) is not str
            or not _HASH.fullmatch(tx["hash"])):
        raise ValueError("Unknown owner nonce is not the claimed THOR ERC20 approval")
    url,body=bound_evm_wallet_rpc(
        asset="ETH",wallet_id=original["wallet_id"],
        wallet=original["wallet_address"],
        # Original signer emits the canonical official stablecoin spelling.
        tx={"from":original["wallet_address"],"chainId":1,
            "to":STABLES[original["stable"]],"data":expected_data,
            "value":"0"})
    digest=hashlib.sha256(json.dumps(
        [url,body,original["idempotency_key"],original["nonce_exact"]],
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    if digest!=original["owner_eth_request_wire_sha256"]:
        raise ValueError("Unknown approval nonce not bound to original Privy RPC")
    return tx["hash"].lower()


def _pending_unknown_thor_approvals(limit:int=8)->list[dict]:
    global _PENDING_OFFSET
    if type(limit) is not int or not 1<=limit<=50:
        raise ValueError("Invalid native approval recovery page")
    db=_db_url()
    _ensure_schema(db)
    sql="""SELECT privy_did,asset,side,stable,chain_id,approval_mode,
                  wallet_address,wallet_id,token_address,router_address,
                  amount_atomic,idempotency_key,request_digest,status,tx_hash,
                  owner_eth_pending_nonce,owner_eth_observed_block_number,
                  owner_eth_scan_next_block,owner_eth_request_wire_sha256
           FROM mininex_live_evm_router_approval_sends
           WHERE approval_mode IN ('THOR_FINITE','THOR_USDT_ZERO')
             AND (status='RECONCILIATION_REQUIRED'
                  OR (status='SENDING' AND
                      updated_at<now()-interval '60 seconds'))
             AND tx_hash IS NULL AND owner_eth_pending_nonce IS NOT NULL
             AND owner_eth_request_wire_sha256 IS NOT NULL
             AND owner_eth_observed_block_number IS NOT NULL
           ORDER BY created_at,privy_did,asset,idempotency_key
           LIMIT %s OFFSET %s"""
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            offset=_PENDING_OFFSET
            cur.execute(sql,(limit,offset))
            rows=list(cur.fetchall())
            if not rows and offset:
                offset=0
                cur.execute(sql,(limit,offset))
                rows=list(cur.fetchall())
    _PENDING_OFFSET=offset+len(rows)
    return rows


def _persist_unknown_thor_cursor(row:dict,after:int)->bool:
    original=_original_unknown_thor_approval_row(row)
    if type(after) is not int or after<=original["next_block"]:
        raise ValueError("Unknown approval scan cannot rewind")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """UPDATE mininex_live_evm_router_approval_sends
                   SET owner_eth_scan_next_block=%s,updated_at=now()
                   WHERE privy_did=%s AND asset=%s AND idempotency_key=%s
                     AND status=%s AND tx_hash IS NULL AND chain_id=1
                     AND owner_eth_pending_nonce=%s
                     AND owner_eth_observed_block_number=%s
                     AND owner_eth_request_wire_sha256=%s
                     AND owner_eth_scan_next_block IS NOT DISTINCT FROM %s
                   RETURNING owner_eth_scan_next_block""",
                (after,original["privy_did"],original["asset"],
                 original["idempotency_key"],original["status"],
                 original["nonce_exact"],original["observed"],
                 original["owner_eth_request_wire_sha256"],
                 original["owner_eth_scan_next_block"]))
            return cur.fetchone() is not None


def _persist_unknown_thor_finality(
    row:dict,*,tx_hash:str,block_number:int,reverted:bool,
)->bool:
    original=_original_unknown_thor_approval_row(row)
    if (type(tx_hash) is not str or not re.fullmatch(r"0x[0-9a-f]{64}",tx_hash)
            or type(block_number) is not int or block_number<original["next_block"]
            or type(reverted) is not bool):
        raise ValueError("Original unknown approval not final")
    state="FINALIZED_REVERTED" if reverted else "SUBMITTED"
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """UPDATE mininex_live_evm_router_approval_sends
                   SET status=%s,tx_hash=%s,owner_eth_scan_next_block=%s,
                       updated_at=now()
                   WHERE privy_did=%s AND asset=%s AND idempotency_key=%s
                     AND status=%s AND tx_hash IS NULL AND chain_id=1
                     AND owner_eth_pending_nonce=%s
                     AND owner_eth_observed_block_number=%s
                     AND owner_eth_request_wire_sha256=%s
                     AND owner_eth_scan_next_block IS NOT DISTINCT FROM %s
                   RETURNING status""",
                (state,tx_hash,block_number+1,original["privy_did"],
                 original["asset"],original["idempotency_key"],
                 original["status"],original["nonce_exact"],
                 original["observed"],original["owner_eth_request_wire_sha256"],
                 original["owner_eth_scan_next_block"]))
            result=cur.fetchone()
            return type(result) is dict and result.get("status")==state


async def _recover_unknown_thor_blocks(
    row:dict,*,client:httpx.AsyncClient,
)->dict:
    original=_original_unknown_thor_approval_row(row)
    chain,checkpoint=await asyncio.gather(
        _read_only_original_eth_rpc(client,"eth_chainId",[],11),
        _read_only_original_eth_rpc(client,"eth_getBlockByNumber",
                                   ["finalized",False],12))
    if _num(chain)!=1 or type(checkpoint) is not dict:
        raise ValueError("Original THOR approval source is not Ethereum mainnet")
    final_height=_num(checkpoint.get("number"))
    start=original["next_block"]
    if start>final_height:
        return {"state":"WAITING_FINALITY"}
    stop=min(final_height+1,start+_SCAN_BLOCKS_PER_TICK)
    for height in range(start,stop):
        block=await _read_only_original_eth_rpc(
            client,"eth_getBlockByNumber",[hex(height),True],100+height%1000)
        if (type(block) is not dict or _num(block.get("number"))!=height
                or type(block.get("hash")) is not str
                or not _HASH.fullmatch(block["hash"])
                or type(block.get("transactions")) is not list
                or len(block["transactions"])>5000):
            raise ValueError("Original THOR approval finalized block unavailable")
        matched=[]
        for tx in block["transactions"]:
            hit=_original_thor_approval_candidate(
                original,tx,block_number=height,block_hash=block["hash"])
            if hit:
                matched.append(hit)
        if len(matched)>1:
            raise ValueError("Original THOR allowance nonce is ambiguous")
        if matched:
            hash_=matched[0]
            receipt,canonical=await asyncio.gather(
                _read_only_original_eth_rpc(
                    client,"eth_getTransactionReceipt",[hash_],201),
                _read_only_original_eth_rpc(
                    client,"eth_getBlockByNumber",[hex(height),False],202))
            if (type(receipt) is not dict or type(canonical) is not dict
                    or str(canonical.get("hash","")).lower()!=block["hash"].lower()
                    or _num(canonical.get("number"))!=height
                    or str(receipt.get("transactionHash","")).lower()!=hash_
                    or str(receipt.get("blockHash","")).lower()!=block["hash"].lower()
                    or _num(receipt.get("blockNumber"))!=height):
                raise ValueError("Original THOR allowance canonical receipt invalid")
            status=_num(receipt.get("status"))
            if status not in (0,1):
                raise ValueError("Original ERC20 allowance execution unknown")
            found=await asyncio.to_thread(
                _persist_unknown_thor_finality,
                original,tx_hash=hash_,block_number=height,
                reverted=status==0)
            return {"state":("FINALIZED_REVERTED" if status==0 else
                             "RECOVERED_SUBMITTED") if found else "STALE_APPROVAL"}
    progressed=await asyncio.to_thread(
        _persist_unknown_thor_cursor,original,after=stop)
    return {"state":"SCANNED_NO_MATCH" if progressed else "STALE_APPROVAL"}


async def reconcile_unknown_thor_approvals_once(limit:int=8)->dict:
    """Native-only owner-scoped approval finality restoration, no resend."""
    rows=await asyncio.to_thread(_pending_unknown_thor_approvals,limit)
    report={
        "mode":"ORIGINAL_THOR_APPROVAL_UNKNOWN_SEND_READ_ONLY_RECOVERY",
        "scanned":len(rows),"recovered_approvals":0,
        "reverted_approvals":0,"unresolved_without_resend":0,
        "new_transactions_sent":0,"new_signatures_created":0,
        "fees_paid":False,"source_trades_sent":0,
    }
    for raw in rows:
        try:
            original=_original_unknown_thor_approval_row(raw)
            user=await fetch_privy_user(original["privy_did"])
            owner=_owned_eth_wallet(user,original["privy_did"])
            if (owner["wallet_id"]!=original["wallet_id"]
                    or owner["address"]!=original["wallet_address"]):
                raise ValueError("Owner changed since original Privy allowance")
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10),follow_redirects=False,
            ) as client:
                state=await _recover_unknown_thor_blocks(original,client=client)
            if state["state"]=="RECOVERED_SUBMITTED":
                report["recovered_approvals"]+=1
            elif state["state"]=="FINALIZED_REVERTED":
                report["reverted_approvals"]+=1
            elif state["state"]=="STALE_APPROVAL":
                report["unresolved_without_resend"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            report["unresolved_without_resend"]+=1
    return report
