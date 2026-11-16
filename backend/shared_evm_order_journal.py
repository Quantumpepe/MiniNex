"""Durable internal EVM send journal shared by five additional trading coins.

The existing ETH privileged executor keeps its audited legacy journal.
This module ONLY claims/persists a server-approved send after caller's
independent user consent, per-asset Privy permission, and strategy reservation.
There is intentionally no FastAPI route, signer call, or scheduler here.

A committed SENDING entry is never retried, even after ambiguous network I/O.
"""
from __future__ import annotations

import hashlib
import json
import re

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_privy_transport import bound_privy_evm_swap_request
from .shared_evm_swap_receipts import _pair
from .storage import _db_url,_ensure_schema,_safe_did
from .live_orders import valid_order_key

_SHA=re.compile(r"^[0-9a-f]{64}$")
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ADDR=re.compile(r"^0x[0-9a-f]{40}$")
_STATES=frozenset(("SENDING","SUBMITTED","RECONCILIATION_REQUIRED",
                  "FINALIZED_SUCCESS","FINALIZED_REVERTED"))
_ACTIVE=frozenset(("SENDING","SUBMITTED","RECONCILIATION_REQUIRED"))


def immutable_evm_send_identity(
    *,asset:str,wallet_id:str,wallet:str,tx:dict,side:str,
    stable:str,input_atomic:str,minimum_output_atomic:str,
)->dict:
    """Canonical identity based on actual Privy CAIP2 RPC, not UI values."""
    if asset=="ETH":
        raise ValueError("ETH uses its existing durable send journal")
    pair=_pair(asset,stable,side)
    url,body=bound_privy_evm_swap_request(
        asset=asset,wallet_id=wallet_id,wallet=wallet,tx=tx,
        side=side,stable=stable,input_atomic=input_atomic,
        minimum_output_atomic=minimum_output_atomic,
    )
    request_bytes=json.dumps(
        [url,body],sort_keys=True,separators=(",",":")).encode("ascii")
    return {
        "asset":asset,"chain_id":pair["chain_id"],
        "network":pair["network"],"side":side,"stable":stable,
        "wallet_address":wallet.lower(),"wallet_id":wallet_id,
        "request_digest":hashlib.sha256(request_bytes).hexdigest(),
        "signed_or_sent":False,
    }


def _claim_locked(cur,*,did:str,key:str,identity:dict)->None:
    """Atomic per-wallet/per-chain claim, invoked only from an approved worker.

    The database lock ensures two engine slots on one network cannot race
    the same Privy account nonce. Different chains need not block each other.
    """
    if (type(identity) is not dict
            or identity.get("asset") not in ("BNB","POL","AVAX","LINK","UNI")
            or type(identity.get("wallet_address")) is not str
            or not _ADDR.fullmatch(identity["wallet_address"])
            or type(identity.get("request_digest")) is not str
            or not _SHA.fullmatch(identity["request_digest"])):
        raise ValueError("Invalid durable EVM order identity")
    asset=identity["asset"]
    pair=_pair(asset,identity.get("stable"),identity.get("side"))
    if (identity.get("chain_id")!=pair["chain_id"]
            or identity.get("network")!=pair["network"]
            or type(identity.get("wallet_id")) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{6,160}",identity["wallet_id"])):
        raise ValueError("Unverified EVM journal chain or wallet")
    # Lock owner's own account row to serialize all same-account claims.
    cur.execute(
        """SELECT privy_did FROM mininex_accounts
           WHERE privy_did=%s FOR UPDATE""",(did,))
    owner=cur.fetchone()
    if type(owner) is not dict or owner.get("privy_did")!=did:
        raise HTTPException(409,"EVM wallet owner has no durable account")
    cur.execute(
        """SELECT COUNT(*) AS pending FROM mininex_live_evm_auto_broadcasts
           WHERE privy_did=%s AND chain_id=%s
             AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
        (did,pair["chain_id"]))
    row=cur.fetchone()
    if type(row) is not dict or type(row.get("pending")) is not int:
        raise HTTPException(503,"Cannot verify pending EVM submissions")
    if row["pending"]!=0:
        raise HTTPException(409,"Existing wallet network send unresolved")
    # ETH, LINK and UNI use the same owner-wallet nonce on Ethereum.
    # Existing ETH AUTO may already have an unconfirmed submitted swap
    # or WETH9 conversion, even when the extra-asset journal is empty.
    if pair["chain_id"]==1:
        cur.execute(
            """SELECT COUNT(*) AS pending
               FROM mininex_live_eth_auto_broadcasts
               WHERE privy_did=%s
                 AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
            (did,))
        eth=cur.fetchone()
        if (type(eth) is not dict or type(eth.get("pending")) is not int
                or eth["pending"]<0):
            raise HTTPException(503,"Cannot verify existing ETH network sends")
        if eth["pending"]!=0:
            raise HTTPException(409,"An ETH swap on this wallet is unresolved")
        cur.execute(
            """SELECT COUNT(*) AS pending
               FROM mininex_live_eth_auto_native_steps
               WHERE privy_did=%s AND status IN
                   ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
            (did,))
        native=cur.fetchone()
        if (type(native) is not dict or type(native.get("pending")) is not int
                or native["pending"]<0):
            raise HTTPException(503,"Cannot verify ETH native conversion status")
        if native["pending"]!=0:
            raise HTTPException(409,"An ETH native conversion is unresolved")
    # No upsert: a repeated key conflicts even after a confirmed transaction.
    cur.execute(
        """INSERT INTO mininex_live_evm_auto_broadcasts
           (privy_did,asset,chain_id,intent_key,wallet_address,wallet_id,
            request_digest,side,stable,status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'SENDING')""",
        (did,asset,pair["chain_id"],key,identity["wallet_address"],
         identity["wallet_id"],identity["request_digest"],
         identity["side"],identity["stable"]))


def claim_internal_evm_send(*,did:str,intent_key:str,identity:dict)->dict:
    """Commit the SENDING claim before an independently authorized RPC send.

    NEVER call with a user-supplied identity. Unlike ETH's LIVE executor,
    this is intentionally not yet connected to a signing or sending worker.
    """
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _claim_locked(cur,did=did,key=key,identity=identity)
    except psycopg.Error:
        raise HTTPException(503,"Could not durably claim EVM order") from None
    return {"asset":identity["asset"],"intent_key":key,
            "state":"SENDING","ready_to_broadcast":False}


def _record_result_locked(cur,*,did:str,asset:str,key:str,
                          digest:str,tx_hash:str|None)->str:
    """Once-only transition; never turn an uncertain send back to RESERVED."""
    pair=_pair(asset,"USDT" if asset=="BNB" else "USDC","BUY")
    cur.execute(
        """SELECT request_digest,status FROM mininex_live_evm_auto_broadcasts
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND chain_id=%s FOR UPDATE""",
        (did,asset,key,pair["chain_id"]))
    row=cur.fetchone()
    if (type(row) is not dict or row.get("status")!="SENDING"
            or row.get("request_digest")!=digest):
        raise HTTPException(409,"Immutable EVM send journal conflict")
    state="SUBMITTED" if tx_hash is not None else "RECONCILIATION_REQUIRED"
    cur.execute(
        """UPDATE mininex_live_evm_auto_broadcasts
           SET status=%s,tx_hash=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s AND status='SENDING'""",
        (state,tx_hash,did,asset,key))
    return state


def record_internal_evm_send_result(
    *,did:str,asset:str,intent_key:str,digest:str,tx_hash:str|None,
)->dict:
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    if (type(digest) is not str or not _SHA.fullmatch(digest)
            or tx_hash is not None and
                (type(tx_hash) is not str or not _HASH.fullmatch(tx_hash))):
        raise ValueError("Invalid server-signed EVM result")
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                state=_record_result_locked(
                    cur,did=did,asset=asset,key=key,
                    digest=digest,tx_hash=tx_hash)
    except psycopg.Error:
        raise HTTPException(503,"Cannot persist immutable EVM send outcome") from None
    return {"asset":asset,"intent_key":key,"state":state,
            "tx_hash":tx_hash,"retry_allowed":False}
