"""Reconcile one exact previously submitted MiniNex automated fee on-chain.

A Privy transaction hash is NOT payment; verify the owner's canonical,
finalized ERC20 Transfer receipt to the exact Treasury and exact amount,
then atomically update the original HWM obligation, payment receipt and
one-shot auto-fee journal. This worker ONLY reads on-chain state; it never
signs or retries the original transaction after a timeout.
"""
from __future__ import annotations

import asyncio
import logging
import re
from threading import Lock

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_fee_payment import _paid_locked,_saved_to_fee_draft
from .shared_evm_fee_transfer_proof import fetch_owner_fee_transfer_proof
from .shared_evm_swap_receipts import _pair
from .storage import _db_url,_ensure_schema

LOG=logging.getLogger("mininex.evm_auto_fee_finality")
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","QNT","UNI","ARB"))
_AUTO_FEE_FINALITY_LOCK=Lock()
_AUTO_FEE_FINALITY_OFFSET=0


def _submitted_automatic_owner_fee_records(limit:int=30)->list[dict]:
    if type(limit) is not int or not 1<=limit<=500:
        raise ValueError("Invalid original fee settlement scan size")
    global _AUTO_FEE_FINALITY_OFFSET
    db=_db_url();_ensure_schema(db)
    sql="""SELECT a.privy_did,a.asset,a.execution_id,a.chain_id,
                          a.stable,a.wallet_address,a.treasury_wallet,
                          a.token_address,a.amount_atomic,a.tx_hash,
                          p.asset AS payment_asset,p.stable AS payment_stable,
                          p.chain_id AS payment_chain_id,
                          p.stable_token_address,p.owner_wallet,
                          p.treasury_wallet AS payment_treasury_wallet,
                          p.fee_due_usd,p.stable_usd,
                          p.token_amount_atomic,p.state AS payment_state
                   FROM mininex_evm_auto_fee_sends a
                   JOIN mininex_evm_verified_fee_payments p
                     ON p.privy_did=a.privy_did AND p.asset=a.asset
                    AND p.execution_id=a.execution_id
                   WHERE a.status='SUBMITTED' AND a.tx_hash IS NOT NULL
                   ORDER BY a.created_at ASC,a.privy_did,a.asset,a.execution_id
                   LIMIT %s OFFSET %s"""
    # Old unresolved receipts never prevent other owner's finalized fees
    # from being verified. This is a read-only scan, not a payment retry.
    with _AUTO_FEE_FINALITY_LOCK:
        offset=_AUTO_FEE_FINALITY_OFFSET
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,offset))
                rows=list(cur.fetchall())
                if not rows and offset:
                    offset=0
                    cur.execute(sql,(limit,0))
                    rows=list(cur.fetchall())
        _AUTO_FEE_FINALITY_OFFSET=offset+len(rows)
    return rows


def original_auto_fee_draft(row:object)->dict:
    if (type(row) is not dict or row.get("asset") not in _ASSETS
            or row.get("asset")!=row.get("payment_asset")
            or row.get("stable")!=row.get("payment_stable")
            or row.get("chain_id")!=row.get("payment_chain_id")
            or row.get("wallet_address")!=row.get("owner_wallet")
            or row.get("treasury_wallet")!=row.get("payment_treasury_wallet")
            or row.get("token_address")!=row.get("stable_token_address")
            or row.get("amount_atomic")!=row.get("token_amount_atomic")
            or row.get("payment_state")!="PREPARED"
            or type(row.get("tx_hash")) is not str
            or not _HASH.fullmatch(row["tx_hash"])):
        raise ValueError("Original one-shot fee and HWM payment disagree")
    source={
        "asset":row["asset"],"stable":row["stable"],
        "chain_id":row["chain_id"],
        "stable_token_address":row["stable_token_address"],
        "owner_wallet":row["wallet_address"],
        "treasury_wallet":row["treasury_wallet"],
        "fee_due_usd":row["fee_due_usd"],
        "stable_usd":row["stable_usd"],
        "token_amount_atomic":row["token_amount_atomic"],
    }
    return _saved_to_fee_draft(source)


def _complete_auto_fee_locked(
    cur,*,row:dict,proof:dict,
)->dict:
    cur.execute(
        """SELECT status,tx_hash,chain_id,stable,wallet_address,
                  treasury_wallet,token_address,amount_atomic
           FROM mininex_evm_auto_fee_sends
           WHERE privy_did=%s AND asset=%s AND execution_id=%s
           FOR UPDATE""",
        (row["privy_did"],row["asset"],row["execution_id"]))
    original=cur.fetchone()
    if (type(original) is not dict
            or original.get("status")!="SUBMITTED"
            or original.get("tx_hash")!=row["tx_hash"]
            or any(original.get(key)!=row.get(key) for key in (
                "chain_id","stable","wallet_address",
                "treasury_wallet","token_address","amount_atomic"))
            or proof.get("onchain_finalized") is not True):
        raise HTTPException(409,"Only original finalized owner fee may settle")
    # Existing HWM and original payment ledgers settle in the SAME SQL txn.
    paid=_paid_locked(
        cur,did=row["privy_did"],asset=row["asset"],
        key=row["execution_id"],wallet=row["wallet_address"],
        tx_hash=row["tx_hash"],proof=proof)
    if paid.get("fee_paid_onchain") is not True:
        raise HTTPException(409,"Original assessed HWM fee not paid")
    cur.execute(
        """UPDATE mininex_evm_auto_fee_sends
           SET status='PAID_VERIFIED',updated_at=now()
           WHERE privy_did=%s AND asset=%s AND execution_id=%s
             AND status='SUBMITTED' AND tx_hash=%s RETURNING status""",
        (row["privy_did"],row["asset"],row["execution_id"],row["tx_hash"]))
    changed=cur.fetchone()
    if type(changed) is not dict or changed.get("status")!="PAID_VERIFIED":
        raise HTTPException(409,"Fee payment and SEND journal not atomic")
    return {
        "state":"PAID_VERIFIED","asset":row["asset"],
        "execution_id":row["execution_id"],
        "tx_hash":row["tx_hash"],
        "five_percent_fee_paid_onchain":True,
        "send_retried":False,
    }


async def reconcile_auto_fee_receipts_once()->dict:
    result={"original_fee_receipts_checked":0,"fees_paid_verified":0,
            "pending_or_unresolved":0,"wallet_transactions_sent":0}
    try:
        rows=await asyncio.to_thread(_submitted_automatic_owner_fee_records)
    except psycopg.Error:
        LOG.warning("Original EVM auto fee receipts unavailable")
        return result
    for row in rows:
        try:
            draft=original_auto_fee_draft(row)
            proof=await fetch_owner_fee_transfer_proof(
                asset=row["asset"],stable=row["stable"],
                draft=draft,tx_hash=row["tx_hash"])
            result["original_fee_receipts_checked"]+=1
            if proof.get("onchain_finalized") is not True:
                result["pending_or_unresolved"]+=1
                continue
            def settle():
                db=_db_url();_ensure_schema(db)
                with psycopg.connect(db,connect_timeout=6) as conn:
                    with conn.cursor(row_factory=dict_row) as cur:
                        return _complete_auto_fee_locked(
                            cur,row=row,proof=proof)
            settled=await asyncio.to_thread(settle)
            if settled.get("state")=="PAID_VERIFIED":
                result["fees_paid_verified"]+=1
        except asyncio.CancelledError:
            raise
        except (HTTPException,ValueError,KeyError,TypeError,psycopg.Error):
            result["pending_or_unresolved"]+=1
    return result
