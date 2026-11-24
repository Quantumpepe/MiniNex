"""Owner-signed manual cross-chain swap. Never signs or broadcasts server-side."""
from __future__ import annotations

import asyncio
from decimal import Decimal, localcontext
import re
import time
import uuid

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .ethereum_settlement_journal import load_quote
from .ethereum_settlement_preflight import prepare_bridge_privy_request
from .privy_live_signer import _owned_eth_wallet
from .privy_wallet_verification import fetch_privy_user
from .storage import _db_url, _ensure_schema, _safe_did
from .ethereum_settlement_funding import assert_ethereum_funds_locked
from .shared_evm_policy_reference_rates import _price, fetch_evm_policy_reference_rates

_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")


def _manual_row(did,quote_id):
    did=_safe_did(did)
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT state,source_tx_hash,source_chain_id
                FROM mininex_ethereum_settlement_manual_attempts
                WHERE privy_did=%s AND quote_id=%s""",(did,uuid.UUID(quote_id)))
            return cur.fetchone()


def claim_manual_attempt(did,quote_id,digest,*,balance_atomic=None,stable_usd=None):
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT privy_did FROM mininex_accounts
                WHERE privy_did=%s FOR UPDATE""",(did,))
            if cur.fetchone() is None:
                raise HTTPException(409,"Owner account unavailable")
            cur.execute("""SELECT source_chain_id,route_sha256,expires_at,asset,side,stable,input_atomic
                FROM mininex_ethereum_settlement_quotes
                WHERE privy_did=%s AND quote_id=%s FOR UPDATE""",(did,uuid.UUID(quote_id)))
            q=cur.fetchone()
            if not q or q["route_sha256"]!=digest or q["expires_at"]<=int(time.time()):
                raise HTTPException(409,"Original quote expired or changed")
            cur.execute("""SELECT a.quote_id FROM mininex_ethereum_settlement_manual_attempts a
                JOIN mininex_ethereum_settlement_quotes q ON q.quote_id=a.quote_id
                WHERE a.privy_did=%s AND q.asset=%s AND a.final_outcome IS NULL
                AND a.state IN ('AWAITING_SIGNATURE','SUBMITTED','UNCERTAIN') LIMIT 1""",
                (did,q["asset"]))
            if cur.fetchone():
                raise HTTPException(409,"Previous swap requires reconciliation")
            if q["side"]=="BUY":
                assert_ethereum_funds_locked(cur,did=did,asset=q["asset"],stable=q["stable"],
                    input_atomic=int(q["input_atomic"]),balance_atomic=balance_atomic,stable_usd=stable_usd)
            cur.execute("""INSERT INTO mininex_ethereum_settlement_manual_attempts
                (quote_id,privy_did,source_chain_id,state)
                VALUES (%s,%s,%s,'AWAITING_SIGNATURE')
                ON CONFLICT DO NOTHING RETURNING quote_id""",
                (uuid.UUID(quote_id),did,q["source_chain_id"]))
            if cur.fetchone() is None:
                raise HTTPException(409,"Original quote already used")


async def prepare_manual_bridge(did,quote_id):
    did=_safe_did(did)
    q=await asyncio.to_thread(load_quote,did,quote_id)
    if await asyncio.to_thread(pending_manual_attempt,did,q["asset"]):
        raise HTTPException(409,"Previous swap requires reconciliation")
    if not q.get("unsigned_transaction"):
        raise HTTPException(409,"No supported executable route for this quote")
    try:
        user=await fetch_privy_user(did)
        owner=_owned_eth_wallet(user,did)
        if owner["address"].lower()!=q["wallet_address"]:
            raise HTTPException(409,"Original wallet changed")
        async with httpx.AsyncClient(timeout=httpx.Timeout(9),follow_redirects=False) as client:
            plan=await prepare_bridge_privy_request(client,q,wallet_id=owner["wallet_id"],
                now=int(time.time()),manual=True)
        if q["expires_at"]<=int(time.time()):
            raise HTTPException(409,"Original quote expired")
        approval=plan.get("manual_approval")
        if approval:
            # Approval is not a swap and must not consume the quote's send claim.
            kind="APPROVAL"; tx=approval
        else:
            kind="SWAP"; tx=q["unsigned_transaction"]
            stable_usd=None
            if q["side"]=="BUY":
                rates=await fetch_evm_policy_reference_rates(q["asset"],q["stable"])
                stable_usd=_price(rates,q["stable"],int(time.time()))
            await asyncio.to_thread(claim_manual_attempt,did,quote_id,q["route_sha256"],
                balance_atomic=int(plan["verified_source_balance_atomic"]),stable_usd=stable_usd)
        return {"mode":"OWNER_MANUAL_ETHEREUM_SETTLEMENT",
            "quote_id":quote_id,"kind":kind,"asset":q["asset"],"side":q["side"],
            "stable":q["stable"],"source_chain_id":q["source_chain_id"],
            "wallet_address":q["wallet_address"],"input_atomic":q["input_atomic"],
            "minimum_output_atomic":q["minimum_output_atomic"],
            "expires_at":q["expires_at"],"transaction":tx,
            "estimated_gas_atomic":plan["estimated_gas_atomic"],
            "server_signing_authorized":False,"automatic_trading_authorized":False}
    except HTTPException:
        raise
    except (ValueError,TypeError,KeyError,httpx.HTTPError,psycopg.Error):
        raise HTTPException(503,"Manual bridge preparation unavailable") from None


def record_manual_attempt(did,quote_id,state,tx_hash=None):
    did=_safe_did(did)
    load_quote(did,quote_id)
    if state not in ("SUBMITTED","CANCELLED","UNCERTAIN"):
        raise HTTPException(422,"Invalid manual submission result")
    if state=="SUBMITTED":
        if not isinstance(tx_hash,str) or not _HASH.fullmatch(tx_hash):
            raise HTTPException(422,"Invalid transaction hash")
        tx_hash=tx_hash.lower()
    elif tx_hash is not None:
        raise HTTPException(422,"Unexpected transaction hash")
    db=_db_url();_ensure_schema(db)
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""UPDATE mininex_ethereum_settlement_manual_attempts
                    SET state=%s,source_tx_hash=%s,updated_at=now()
                    WHERE quote_id=%s AND privy_did=%s
                    AND (state='AWAITING_SIGNATURE' OR
                         (state='UNCERTAIN' AND %s='SUBMITTED'))
                    RETURNING state""",
                    (state,tx_hash,uuid.UUID(quote_id),did,state))
                if cur.fetchone() is None:
                    cur.execute("""SELECT state,source_tx_hash
                        FROM mininex_ethereum_settlement_manual_attempts
                        WHERE quote_id=%s AND privy_did=%s""",(uuid.UUID(quote_id),did))
                    old=cur.fetchone()
                    if not old or old["state"]!=state or old["source_tx_hash"]!=tx_hash:
                        raise HTTPException(409,"Original manual attempt differs")
    except psycopg.Error:
        raise HTTPException(409,"Manual transaction cannot be recorded") from None
    return {"quote_id":quote_id,"state":state,"source_tx_hash":tx_hash,
            "settlement_verified":False}


def load_manual_attempt(did,quote_id):
    try:
        return _manual_row(did,quote_id)
    except psycopg.Error:
        raise HTTPException(503,"Manual submission unavailable") from None


def pending_manual_attempt(did,asset):
    """Recover the same owner-bound attempt after app restart or lost local state."""
    did=_safe_did(did)
    if asset not in ("BNB","POL","AVAX","ARB"):
        raise HTTPException(422,"Unsupported bridge asset")
    db=_db_url();_ensure_schema(db)
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""SELECT a.quote_id,a.state,a.source_tx_hash,q.asset,
                    q.side,q.stable,q.input_atomic,q.wallet_address
                    FROM mininex_ethereum_settlement_manual_attempts a
                    JOIN mininex_ethereum_settlement_quotes q ON q.quote_id=a.quote_id
                    WHERE a.privy_did=%s AND q.asset=%s AND a.final_outcome IS NULL
                    AND a.state IN ('AWAITING_SIGNATURE','SUBMITTED','UNCERTAIN')
                    ORDER BY a.created_at LIMIT 1""",(did,asset))
                row=cur.fetchone()
        if row is None:
            return None
        with localcontext() as context:
            context.prec=80
            amount=format(Decimal(row["input_atomic"]).scaleb(-6 if row["side"]=="BUY" else -18),"f")
        return {"quote_id":str(row["quote_id"]),"asset":row["asset"],
                "side":row["side"],"stable":row["stable"],"wallet":row["wallet_address"],
                "amount":amount,"hash":row["source_tx_hash"],"state":row["state"]}
    except psycopg.Error:
        raise HTTPException(503,"Manual swap recovery unavailable") from None


def mark_verified_manual_outcome(did,quote_id,tx_hash,outcome):
    """Only called from the independent chain verifier, never a client result."""
    did=_safe_did(did)
    if outcome not in ("FINALIZED","SOURCE_REVERTED") or not _HASH.fullmatch(tx_hash):
        raise ValueError("Invalid final chain outcome")
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE mininex_ethereum_settlement_manual_attempts
                SET final_outcome=%s,updated_at=now()
                WHERE quote_id=%s AND privy_did=%s AND state='SUBMITTED'
                AND source_tx_hash=%s AND (final_outcome IS NULL OR final_outcome=%s)""",
                (outcome,uuid.UUID(quote_id),did,tx_hash.lower(),outcome))
