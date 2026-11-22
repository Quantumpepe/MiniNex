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
from .live_eth_quotes import STABLES
from .live_eth_receipts import _num
from .storage import _db_url,_ensure_schema

_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_APPROVAL_PAGE_OFFSET=0
_NATIVE_APPROVAL_PAGE_OFFSET=0


def _pending_approvals(limit:int=100,*,native_only:bool=False)->list[dict]:
    """Rotate across original chain submissions, not just the oldest page.

    A perpetually pending old transaction must not prevent other wallets
    from having their original allowances finalized.
    """
    global _APPROVAL_PAGE_OFFSET,_NATIVE_APPROVAL_PAGE_OFFSET
    if type(native_only) is not bool or type(limit) is not int or not 1<=limit<=500:
        raise ValueError("Invalid passive approval scan page size")
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            sql="""SELECT privy_did,asset,side,stable,chain_id,wallet_address,
                          token_address,router_address,amount_atomic,
                          idempotency_key,tx_hash,approval_mode
                   FROM mininex_live_evm_router_approval_sends
                   WHERE status='SUBMITTED' AND tx_hash IS NOT NULL""" + (
                   """ AND approval_mode IN ('THOR_FINITE','THOR_USDT_ZERO')"""
                   if native_only else "") + """
                   ORDER BY created_at,privy_did,asset,idempotency_key
                   LIMIT %s OFFSET %s"""
            offset=(_NATIVE_APPROVAL_PAGE_OFFSET if native_only
                    else _APPROVAL_PAGE_OFFSET)
            cur.execute(sql,(limit,offset))
            rows=list(cur.fetchall())
            if not rows and offset:
                offset=0
                cur.execute(sql,(limit,0))
                rows=list(cur.fetchall())
    if native_only:
        _NATIVE_APPROVAL_PAGE_OFFSET=offset+len(rows)
    else:
        _APPROVAL_PAGE_OFFSET=offset+len(rows)
    return rows


def _verify_approval_finality(row:dict,*,chain_id:object,
                              tx:object,receipt:object,
                              finalized:object,canonical:object=None)->str|None:
    if type(row) is not dict or row.get("asset") not in (
            "BNB","POL","AVAX","LINK","QNT","UNI","ARB","BTC","SOL","XRP",
            "TRX","DOGE"):
        raise ValueError("Unsupported originally submitted approval")
    mode=row.get("approval_mode","ROUTER02_FINITE")
    native=row["asset"] in ("BTC","SOL","XRP","TRX","DOGE")
    if native:
        if (mode not in ("THOR_FINITE","THOR_USDT_ZERO")
                or row.get("side")!="BUY" or row.get("stable") not in STABLES
                or (row["asset"]=="XRP" and row["stable"]!="USDC")
                or (mode=="THOR_USDT_ZERO" and row["stable"]!="USDT")
                or row.get("chain_id")!=1 or _num(chain_id)!=1):
            raise ValueError("Native THOR approval source chain invalid")
        # The THOR router was independently validated and pinned at first
        # send, not substituted with the unrelated Uniswap Router02.
        route={"chain_id":1,"source_token":STABLES[row["stable"]],
               "router":row.get("router_address")}
    else:
        if mode!="ROUTER02_FINITE":
            raise ValueError("Unexpected shared EVM approval mode")
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
    if (type(wallet) is not str or type(token) is not str
            or type(spender) is not str
            or token.lower()!=route["source_token"].lower()
            or spender.lower()!=route["router"].lower()):
        raise ValueError("Original approval token or spender differs from canonical route")
    amount=int(row["amount_atomic"])
    if not 0<amount<10**45:
        raise ValueError("Original approved amount not finite")
    if not re.fullmatch(r"0x[0-9a-f]{40}",spender):
        raise ValueError("Invalid original approved spender")
    approved_value=0 if mode=="THOR_USDT_ZERO" else amount
    expected="0x095ea7b3"+f"{int(spender,16):064x}"+f"{approved_value:064x}"
    if (str(tx.get("hash","")).lower()!=hash_
            or str(receipt.get("transactionHash","")).lower()!=hash_
            or str(tx.get("from","")).lower()!=wallet.lower()
            or str(receipt.get("from","")).lower()!=wallet.lower()
            or str(tx.get("to","")).lower()!=token
            or str(receipt.get("to","")).lower()!=token
            or str(tx.get("input","")).lower()!=expected
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
    if canonical is None:
        return None
    if (type(canonical) is not dict
            or _num(canonical.get("number"))!=block
            or type(canonical.get("hash")) is not str
            or canonical["hash"].lower()!=str(receipt.get("blockHash","")).lower()):
        raise ValueError("Router approval is not in the canonical mined block")
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
    *,client:httpx.AsyncClient,native_only:bool=False,limit:int=100,
)->dict:
    result={"checked":0,"finalized":0,"pending":0,"unresolved":0,"resent":0,"scanned":0}
    if type(client) is not httpx.AsyncClient:
        raise ValueError("Trusted RPC client required")
    rows=await asyncio.to_thread(_pending_approvals,limit,native_only=native_only)
    result["scanned"]=len(rows)
    for row in rows:
        try:
            asset=row["asset"]
            if asset in ("ETH","LINK","QNT","UNI","BTC","SOL","XRP","TRX","DOGE"):
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
            canonical=(await _receipt_rpc(
                client,url,"eth_getBlockByNumber",
                [hex(_num(receipt["blockNumber"])),False],5)
                if type(receipt) is dict and receipt.get("blockNumber") is not None
                and type(finalized) is dict
                and _num(finalized.get("number"))>=_num(receipt["blockNumber"])
                else None)
            state=_verify_approval_finality(
                row,chain_id=cid,tx=tx,receipt=receipt,
                finalized=finalized,canonical=canonical)
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


async def reconcile_original_native_thor_approvals_once(limit:int=24)->dict:
    """Native BUY-specific read-only allowance finality without another signer.

    The EVM worker may also reconcile these same submitted receipts; the
    journal status check ensures both observers cannot finalize twice.
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(9),follow_redirects=False,
    ) as client:
        report=await reconcile_original_router_approvals_once(
            client=client,native_only=True,limit=limit)
    return {
        "mode":"ORIGINAL_NATIVE_THOR_APPROVAL_PASSIVE_FINALITY",
        "scanned":report["scanned"],
        "finalized_approvals":report["finalized"],
        "pending_approvals":report["pending"],
        "unresolved_without_resend":report["unresolved"],
        "new_transactions_sent":0,"fees_paid":False,
        "source_trades_finalized":0,
    }
