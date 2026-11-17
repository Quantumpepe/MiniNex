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
        """SELECT (SELECT COUNT(*) FROM mininex_live_evm_auto_broadcasts
              WHERE privy_did=%s AND chain_id=%s
                AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
             + (SELECT COUNT(*) FROM mininex_evm_auto_fee_sends
              WHERE privy_did=%s AND chain_id=%s
                AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
             AS pending""",
        (did,pair["chain_id"],did,pair["chain_id"]))
    row=cur.fetchone()
    if type(row) is not dict or type(row.get("pending")) is not int:
        raise HTTPException(503,"Cannot verify pending EVM submissions")
    if row["pending"]!=0:
        raise HTTPException(409,"Existing wallet network send unresolved")
    # Native WETH9-style conversions and Router02 swaps use the same
    # wallet nonce. Prevent a simultaneous native WITHDRAW (including an
    # ambiguous submission) from overlapping a new network swap.
    if asset in ("BNB","POL","AVAX"):
        cur.execute(
            """SELECT COUNT(*) AS pending
               FROM mininex_live_evm_native_auto_steps
               WHERE privy_did=%s AND chain_id=%s AND status IN
                 ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
            (did,pair["chain_id"]))
        native=cur.fetchone()
        if (type(native) is not dict or type(native.get("pending")) is not int
                or native["pending"]!=0):
            raise HTTPException(409,"Native EVM network send unresolved")
    if asset in ("BNB","POL","AVAX"):
        cur.execute(
            """SELECT COUNT(*) AS pending
               FROM mininex_live_evm_native_sell_intents
               WHERE privy_did=%s AND chain_id=%s AND status IN
                 ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
            (did,pair["chain_id"]))
        native_sell=cur.fetchone()
        if (type(native_sell) is not dict
                or type(native_sell.get("pending")) is not int
                or native_sell["pending"]!=0):
            raise HTTPException(409,"Native EVM SELL deposit unresolved")
        # Once a native SELL deposit is mined, the exact original event
        # has priority to swap those tokens. An unrelated BUY/SELL must
        # not consume the shared wallet nonce before that event settles.
        cur.execute(
            """SELECT COUNT(*) AS pending
               FROM mininex_live_evm_native_sell_intents n
               LEFT JOIN mininex_live_evm_auto_broadcasts b
                 ON b.privy_did=n.privy_did AND b.asset=n.asset
                AND b.intent_key=n.intent_key
               WHERE n.privy_did=%s AND n.chain_id=%s
                 AND n.status='FINALIZED_SUCCESS'
                 AND n.intent_key<>%s AND b.intent_key IS NULL""",
            (did,pair["chain_id"],key))
        unfinished=cur.fetchone()
        if (type(unfinished) is not dict
                or type(unfinished.get("pending")) is not int
                or unfinished["pending"]!=0):
            raise HTTPException(409,"Original native SELL must complete first")
        # A native BUY is not settled by its finalized Router02 leg alone.
        # Do not allow a fresh swap to consume the wrapped source between
        # the confirmed BUY and its original native WITHDRAW.
        cur.execute(
            """SELECT COUNT(*) AS pending
               FROM mininex_live_evm_auto_broadcasts b
               LEFT JOIN mininex_live_evm_completed_native_cycles c
                 ON c.privy_did=b.privy_did AND c.asset=b.asset
                AND c.intent_key=b.intent_key
               WHERE b.privy_did=%s AND b.chain_id=%s
                 AND b.side='BUY' AND b.status='FINALIZED_SUCCESS'
                 AND c.intent_key IS NULL AND b.intent_key<>%s""",
            (did,pair["chain_id"],key))
        unsettled_buy=cur.fetchone()
        if (type(unsettled_buy) is not dict
                or type(unsettled_buy.get("pending")) is not int
                or unsettled_buy["pending"]!=0):
            raise HTTPException(409,"Original native BUY conversion unsettled")
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


def _claim_authorized_evm_order_locked(
    cur,*,did:str,key:str,identity:dict,
    quote_sha256:str,budget_revision:int,
)->None:
    """Atomic last-mile gate before any Privy RPC or signer use.

    Same transaction locks the owner asset's consent, policy and reserved
    budget; a revocation or settings change between GET/readback and SEND
    blocks the SENDING claim. Remote Privy policy and quorum must ALSO have
    been freshly verified by the caller before entering this transaction.
    """
    if (type(identity) is not dict
            or identity.get("asset") not in ("BNB","POL","AVAX","LINK","UNI")
            or type(quote_sha256) is not str
            or not _SHA.fullmatch(quote_sha256)
            or type(budget_revision) is not int or budget_revision<1):
        raise ValueError("Unverified executable coin order")
    asset=identity["asset"]
    pair=_pair(asset,identity.get("stable"),identity.get("side"))
    cur.execute(
        """SELECT status,stable,chain_id,wallet_id,wallet_address,
                  policy_id,remote_policy_sha256,policy_sha256,
                  consent_sha256,expires_at,revoked_at,budget_revision
           FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    policy=cur.fetchone()
    from datetime import datetime,timezone
    if (type(policy) is not dict
            or policy.get("status")!="GRANT_VERIFIED"
            or policy.get("revoked_at") is not None
            or policy.get("stable")!=identity["stable"]
            or policy.get("chain_id")!=pair["chain_id"]
            or policy.get("wallet_id")!=identity["wallet_id"]
            or policy.get("wallet_address")!=identity["wallet_address"]
            or policy.get("budget_revision")!=budget_revision
            or policy.get("remote_policy_sha256")!=policy.get("policy_sha256")
            or not _SHA.fullmatch(str(policy.get("policy_sha256","")))
            or type(policy.get("policy_id")) is not str
            or not isinstance(policy.get("expires_at"),datetime)
            or policy["expires_at"].tzinfo is None
            or policy["expires_at"]<=datetime.now(timezone.utc)):
        raise HTTPException(409,"Current owner-approved EVM signer grant required")
    cur.execute(
        """SELECT status,wallet_id,wallet_address,chain_id,stable,
                  budget_revision,message_sha256,confirmed_at
           FROM mininex_live_evm_consent_challenges
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    consent=cur.fetchone()
    if (type(consent) is not dict
            or consent.get("status")!="SIGNED_INTENT"
            or consent.get("confirmed_at") is None
            or consent.get("wallet_id")!=policy["wallet_id"]
            or consent.get("wallet_address")!=policy["wallet_address"]
            or consent.get("chain_id")!=pair["chain_id"]
            or consent.get("stable")!=identity["stable"]
            or consent.get("budget_revision")!=budget_revision
            or consent.get("message_sha256")!=policy["consent_sha256"]):
        raise HTTPException(409,"Matching signed coin consent required")
    cur.execute(
        """SELECT stable,revision FROM mininex_extra_coin_preferences
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    settings=cur.fetchone()
    if (type(settings) is not dict
            or settings.get("revision")!=budget_revision
            or settings.get("stable")!=identity["stable"]):
        raise HTTPException(409,"Coin settings changed since signed approval")
    cur.execute(
        """SELECT state,side,stable,budget_revision,quote_sha256
           FROM mininex_live_evm_budget_reservations
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,key))
    reserved=cur.fetchone()
    if (type(reserved) is not dict or reserved.get("state")!="RESERVED"
            or reserved.get("side")!=identity["side"]
            or reserved.get("stable")!=identity["stable"]
            or reserved.get("budget_revision")!=budget_revision
            or reserved.get("quote_sha256")!=quote_sha256):
        raise HTTPException(409,"No matching durable per-coin strategy reservation")
    # No actual send without an independently owner-initiated ACTIVE session.
    # A paused session can have pending settlements, but never a fresh send.
    cur.execute(
        """SELECT status,chain_id,stable,wallet_address,budget_revision,
                  started_by_owner_at
           FROM mininex_live_evm_auto_sessions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    session=cur.fetchone()
    if (type(session) is not dict
            or session.get("status")!="ACTIVE"
            or session.get("chain_id")!=pair["chain_id"]
            or session.get("stable")!=identity["stable"]
            or session.get("wallet_address")!=identity["wallet_address"]
            or session.get("budget_revision")!=budget_revision
            or session.get("started_by_owner_at") is None):
        raise HTTPException(409,"Owner has not started this coin's LIVE session")
    # This helper also atomically guards all unresolved same-wallet network
    # transactions, including ETH native conversions for LINK and UNI.
    _claim_locked(cur,did=did,key=key,identity=identity)
    cur.execute(
        """UPDATE mininex_live_evm_budget_reservations
           SET state='SENDING',updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND state='RESERVED' RETURNING state""",(did,asset,key))
    updated=cur.fetchone()
    if type(updated) is not dict or updated.get("state")!="SENDING":
        raise HTTPException(409,"Concurrent budget claim conflict")


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


def _record_authorized_result_locked(
    cur,*,did:str,asset:str,key:str,digest:str,tx_hash:str|None,
)->str:
    """Broadcast and budget reservation move together or both roll back.

    The actual RPC must have been attempted at most once after the committed
    authorized SENDING claim. Timeout/unknown result permanently retains
    RECONCILIATION_REQUIRED; funds remain reserved until reconciliation.
    """
    state=_record_result_locked(
        cur,did=did,asset=asset,key=key,digest=digest,tx_hash=tx_hash)
    cur.execute(
        """UPDATE mininex_live_evm_budget_reservations
           SET state=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND state='SENDING' RETURNING state""",
        (state,did,asset,key))
    updated=cur.fetchone()
    if type(updated) is not dict or updated.get("state")!=state:
        raise HTTPException(409,"EVM budget/broadcast persistence conflict")
    return state


def record_internal_authorized_evm_send_result(
    *,did:str,asset:str,intent_key:str,digest:str,tx_hash:str|None,
)->dict:
    """Internal approved executor only, never a public HTTP writer."""
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    if (asset not in ("BNB","POL","AVAX","LINK","UNI")
            or type(digest) is not str or not _SHA.fullmatch(digest)
            or tx_hash is not None and (
                type(tx_hash) is not str or not _HASH.fullmatch(tx_hash))):
        raise ValueError("Invalid privileged EVM send outcome")
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                state=_record_authorized_result_locked(
                    cur,did=did,asset=asset,key=key,
                    digest=digest,tx_hash=tx_hash)
    except psycopg.Error:
        raise HTTPException(
            503,"Cannot durably bind EVM broadcast to reserved budget") from None
    return {
        "asset":asset,"intent_key":key,"state":state,
        "tx_hash":tx_hash,"retry_allowed":False,
    }


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
