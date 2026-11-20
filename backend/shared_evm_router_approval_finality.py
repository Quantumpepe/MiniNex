"""Passive finality of originally broadcast owner-granted ERC20 Router02 approvals.

Never broadcasts, retries, or turns a pending transaction into a successful
allowance based only on Privy's response. Once finalized, the next strategist
tick independently rechecks actual token allowance before its real swap.
"""
from __future__ import annotations

import asyncio
import os
import re

import httpx
import psycopg
from psycopg.rows import dict_row

from .shared_evm_swap_receipts import _pair,_receipt_rpc
from .multicoin_evm_balances import _RPC
from .live_eth_receipts import _num
from .storage import _db_url,_ensure_schema

_HASH=re.compile(r"^0x[0-9a-f]{64}$")


def _pending_approvals()->list[dict]:
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT privy_did,asset,side,stable,chain_id,wallet_address,
                          token_address,router_address,amount_atomic,
                          idempotency_key,tx_hash
                   FROM mininex_live_evm_router_approval_sends
                   WHERE status='SUBMITTED' AND tx_hash IS NOT NULL
                   ORDER BY created_at,privy_did,asset
                   LIMIT 100""")
            return list(cur.fetchall())


def _verify_approval_finality(row:dict,*,chain_id:object,
                              tx:object,receipt:object,
                              finalized:object)->str|None:
    if type(row) is not dict or row.get("asset") not in (
            "BNB","POL","AVAX","LINK","UNI","ARB"):
        raise ValueError("Unsupported originally submitted approval")
    route=_pair(row["asset"],row["stable"],row["side"])
    if route["chain_id"]!=row["chain_id"] or _num(chain_id)!=row["chain_id"]:
        raise ValueError("Wrong approval chain")
    hash_=row.get("tx_hash")
    if type(hash_) is not str or not _HASH.fullmatch(hash_):
        raise ValueError("No original approval transaction")
    if tx is None or receipt is None or finalized is None:
        return None
    if type(tx) is not dict or type(receipt) is not dict or type(finalized) is not dict:
        raise ValueError("Untrusted approval receipt")
    wallet=row["wallet_address"]
    token=row["token_address"]
    spender=row["router_address"]
    amount=int(row["amount_atomic"])
    expected="0x095ea7b3"+f"{int(spender,16):064x}"+f"{amount:064x}"
    if (str(tx.get("hash","")).lower()!=hash_
            or str(receipt.get("transactionHash","")).lower()!=hash_
            or str(tx.get("from","")).lower()!=wallet
            or str(receipt.get("from","")).lower()!=wallet
            or str(tx.get("to","")).lower()!=token
            or str(receipt.get("to","")).lower()!=token
            or tx.get("input","").lower()!=expected
            or _num(tx.get("chainId"))!=row["chain_id"]
            or _num(tx.get("value"))!=0):
        raise ValueError("Original router approval transaction was substituted")
    block=_num(receipt.get("blockNumber"))
    if (block<=0 or block!=_num(tx.get("blockNumber"))
            or str(receipt.get("blockHash","")).lower()
                !=str(tx.get("blockHash","")).lower()):
        raise ValueError("Approval transaction and receipt have different blocks")
    if _num(finalized.get("number"))<block:
        return None
    state=_num(receipt.get("status"))
    if state not in (0,1):
        raise ValueError("Invalid original approval execution result")
    return "FINALIZED_SUCCESS" if state else "FINALIZED_REVERTED"


def _persist_finality(row:dict,status:str)->bool:
    if status not in ("FINALIZED_SUCCESS","FINALIZED_REVERTED"):
        raise ValueError("Unsupported original approval settlement")
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """UPDATE mininex_live_evm_router_approval_sends
                   SET status=%s,updated_at=now()
                   WHERE privy_did=%s AND asset=%s
                     AND idempotency_key=%s AND tx_hash=%s
                     AND status='SUBMITTED' RETURNING status""",
                (status,row["privy_did"],row["asset"],
                 row["idempotency_key"],row["tx_hash"]))
            return cur.fetchone() is not None


async def reconcile_original_router_approvals_once(
    *,client:httpx.AsyncClient,
)->dict:
    result={"checked":0,"finalized":0,"pending":0,"unresolved":0,"resent":0}
    if type(client) is not httpx.AsyncClient:
        raise ValueError("Trusted RPC client required")
    rows=await asyncio.to_thread(_pending_approvals)
    for row in rows:
        try:
            asset=row["asset"]
            if asset in ("LINK","UNI"):
                chain,network,fallback,env=_RPC["ETH_TOKENS"]
            elif asset=="ARB":
                chain,network,fallback,env=_RPC["ARB_TOKENS"]
            else:
                chain,network,fallback,env=_RPC[asset]
            if chain!=row["chain_id"]:
                raise ValueError("Owner approval RPC chain changed")
            url=os.getenv(env,"").strip() or fallback
            if not url.startswith("https://") or any(c in url for c in " \t\r\n#@"):
                raise ValueError("Invalid chain RPC")
            h=row["tx_hash"]
            cid,tx,receipt,finalized=await asyncio.gather(
                _receipt_rpc(client,url,"eth_chainId",[],1),
                _receipt_rpc(client,url,"eth_getTransactionByHash",[h],2),
                _receipt_rpc(client,url,"eth_getTransactionReceipt",[h],3),
                _receipt_rpc(client,url,"eth_getBlockByNumber",["finalized",False],4),
            )
            state=_verify_approval_finality(
                row,chain_id=cid,tx=tx,receipt=receipt,finalized=finalized)
            result["checked"]+=1
            if state is None:
                result["pending"]+=1
            elif await asyncio.to_thread(_persist_finality,row,state):
                result["finalized"]+=1
        except asyncio.CancelledError:
            raise
        except Exception:
            result["unresolved"]+=1
    return result
