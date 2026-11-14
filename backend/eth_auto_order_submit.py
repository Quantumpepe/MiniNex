"""Durable privileged ETH AUTO order sender, using Privy additional signer.

This module actually calls the Privy ETH transaction broadcaster after an
already-reserved strategy order and exact verified user permission. Nothing
here is a public route or an automatic scheduled worker; the separate grant
activation and verified strategy scheduler must call it when approved.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_auto_transport import (
    bound_privy_rpc_request,broadcast_privy_eth_swap,
)
from .eth_privy_p256_signer import sign_privy_eth_request
from .live_orders import valid_order_key
from .storage import _db_url,_ensure_schema,_safe_did
from .privy_wallet_verification import APP_ID,fetch_privy_user
from .privy_live_signer import (
    _owned_eth_wallet,fetch_privy_wallet_metadata,_scoped_signer,
    signer_configuration,
)

LOG=logging.getLogger("mininex.eth_auto_executor")
_SHA=re.compile(r"^[0-9a-f]{64}$")


def _request_digest(wallet_id: str,tx: dict, *, wallet: str,
                    side: str,stable: str,input_atomic: str,
                    minimum_output_atomic: str) -> str:
    url,body=bound_privy_rpc_request(
        wallet_id=wallet_id,wallet=wallet,tx=tx,side=side,stable=stable,
        input_atomic=input_atomic,
        minimum_output_atomic=minimum_output_atomic)
    payload=json.dumps([url,body],sort_keys=True,separators=(",",":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _claim_locked(cur,*,did:str,key:str, wallet:str, wallet_id:str,
                  signer_id:str,policy_id:str,digest:str)->str:
    """Commit SENDING before attempting Privy HTTP, under the ETH limit lock."""
    cur.execute(
        """SELECT trading_paused,revision
           FROM mininex_live_eth_limits
           WHERE privy_did=%s FOR UPDATE""",(did,))
    limits=cur.fetchone()
    if type(limits) is not dict or limits.get("trading_paused") is not False:
        raise HTTPException(409,"ETH AUTO is stopped")
    cur.execute(
        """SELECT status,policy_id,wallet_address,limits_revision,
                  execution_signer_id,remote_policy_sha256,candidate_sha256,
                  extract(epoch FROM policy_expires_at)::bigint AS expires_at_unix
           FROM mininex_live_eth_policy_enrollments
           WHERE privy_did=%s FOR UPDATE""",(did,))
    grant=cur.fetchone()
    from time import time
    if (type(grant) is not dict or grant.get("status")!="GRANT_VERIFIED"
            or grant.get("policy_id")!=policy_id
            or grant.get("execution_signer_id")!=signer_id
            or str(grant.get("wallet_address","")).lower()!=wallet
            or grant.get("limits_revision")!=limits.get("revision")
            or not _SHA.fullmatch(str(grant.get("candidate_sha256","")))
            or grant.get("remote_policy_sha256")!=grant.get("candidate_sha256")
            or type(grant.get("expires_at_unix")) is not int
            or grant["expires_at_unix"]<=int(time())+30):
        raise HTTPException(409,"Active verified Privy signer grant required")
    cur.execute(
        """SELECT status,revoked_at,limits_revision
           FROM mininex_live_eth_consent_intents
           WHERE privy_did=%s FOR UPDATE""",(did,))
    consent=cur.fetchone()
    if (type(consent) is not dict
            or consent.get("status")!="SIGNED_INTENT"
            or consent.get("revoked_at") is not None
            or consent.get("limits_revision")!=limits.get("revision")):
        raise HTTPException(409,"LIVE signer consent revoked or outdated")
    # Avoid overlapping non-finalized transactions for this wallet.
    cur.execute(
        """SELECT COUNT(*) AS pending FROM mininex_live_order_intents
           WHERE privy_did=%s AND asset='ETH'
             AND intent_key<>%s AND state IN
             ('RESERVED','SENDING','RECONCILIATION_REQUIRED',
              'SUBMITTED','REORG_PENDING')""",(did,key))
    pending=cur.fetchone()
    if type(pending) is not dict or pending.get("pending")!=0:
        raise HTTPException(409,"Existing ETH order must settle first")
    cur.execute(
        """SELECT COUNT(*) AS pending FROM mininex_live_eth_auto_native_steps
           WHERE privy_did=%s AND status IN
             ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
        (did,))
    pending_native=cur.fetchone()
    if type(pending_native) is not dict or pending_native.get("pending")!=0:
        raise HTTPException(409,"Native ETH conversion still in progress")
    cur.execute(
        """SELECT state,side,stable,chain_network
           FROM mininex_live_order_intents
           WHERE privy_did=%s AND asset='ETH' AND intent_key=%s FOR UPDATE""",
        (did,key))
    order=cur.fetchone()
    if (type(order) is not dict or order.get("state")!="RESERVED"
            or order.get("chain_network")!="ethereum-mainnet"
            or order.get("side") not in ("BUY","SELL")
            or order.get("stable") not in ("USDC","USDT")):
        raise HTTPException(409,"Reserved ETH order unavailable or already sent")
    cur.execute(
        """INSERT INTO mininex_live_eth_auto_broadcasts
           (privy_did,intent_key,wallet_address,wallet_id,
            request_digest,side,stable,status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'SENDING')""",
        (did,key,wallet,wallet_id,digest,order["side"],order["stable"]))
    cur.execute(
        """UPDATE mininex_live_order_intents SET state='SENDING',
                    updated_at=now()
           WHERE privy_did=%s AND asset='ETH' AND intent_key=%s""",
        (did,key))
    return order["side"]+"|"+order["stable"]


def _persist_submission(did:str,key:str,*,digest:str,tx_hash:str|None)->None:
    """Even uncertainty is durable and NEVER retried by this executor."""
    url=_db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT request_digest,status FROM mininex_live_eth_auto_broadcasts
                   WHERE privy_did=%s AND intent_key=%s FOR UPDATE""",
                (did,key))
            row=cur.fetchone()
            if (type(row) is not dict or row.get("request_digest")!=digest
                    or row.get("status")!="SENDING"):
                raise HTTPException(409,"Privy ETH broadcast journal conflict")
            state="SUBMITTED" if tx_hash else "RECONCILIATION_REQUIRED"
            cur.execute(
                """UPDATE mininex_live_eth_auto_broadcasts
                   SET status=%s,tx_hash=%s,updated_at=now()
                   WHERE privy_did=%s AND intent_key=%s""",
                (state,tx_hash,did,key))
            cur.execute(
                """UPDATE mininex_live_order_intents
                   SET state=%s,tx_hash=%s,updated_at=now(),
                       submitted_at=CASE WHEN %s='SUBMITTED'
                                          THEN now() ELSE submitted_at END
                   WHERE privy_did=%s AND asset='ETH'
                     AND intent_key=%s AND state='SENDING'""",
                (state,tx_hash,state,did,key))


async def submit_privy_eth_auto_order(
    *,did:str,intent_key:str,tx:dict,side:str,stable:str,
    input_atomic:str,minimum_output_atomic:str,
    client:httpx.AsyncClient,
)->dict:
    """A single non-retriable real Privy send for a previously reserved order.

    Called only from a trusted internal strategy executor, not FastAPI.
    The registered server P-256 key signs *this exact RPC body*. No user JWT,
    client-supplied wallet identity, approval or arbitrary recipient accepted.
    """
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    config=signer_configuration()
    if config is None:
        raise HTTPException(503,"Privy ETH additional signer not configured")
    user=await fetch_privy_user(did)
    owned=_owned_eth_wallet(user,did)
    if not owned["delegated"]:
        raise HTTPException(409,"Wallet owner has not delegated signer")
    metadata=await fetch_privy_wallet_metadata(owned["wallet_id"])
    if not _scoped_signer(metadata,did=did,address=owned["address"],config=config):
        raise HTTPException(409,"Restricted Privy wallet signer not verified")
    wallet=owned["address"]
    digest=_request_digest(
        owned["wallet_id"],tx,wallet=wallet,side=side,stable=stable,
        input_atomic=input_atomic,
        minimum_output_atomic=minimum_output_atomic)
    # Durable once-only SENDING claim MUST commit before any remote send.
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                terms=_claim_locked(cur,did=did,key=key,wallet=wallet,
                    wallet_id=owned["wallet_id"],
                    signer_id=config["signer_id"],policy_id=config["policy_id"],
                    digest=digest)
    except psycopg.Error:
        raise HTTPException(503,"Could not durably claim LIVE ETH order") from None
    if terms!=side+"|"+stable:
        # SENDING now exists, so never broadcast wrong trade or re-claim.
        _persist_submission(did,key,digest=digest,tx_hash=None)
        raise HTTPException(409,"ETH order terms mismatch")
    try:
        # Authorization key never crosses an HTTP request or UI boundary.
        # One signature over the exact canonical Privy RPC payload.
        signature=sign_privy_eth_request(
            wallet_id=owned["wallet_id"],wallet=wallet,tx=tx,
            side=side,stable=stable,
            input_atomic=input_atomic,
            minimum_output_atomic=minimum_output_atomic,
            idempotency_key=key)
        tx_hash=await broadcast_privy_eth_swap(
            wallet_id=owned["wallet_id"],wallet=wallet,
            tx=tx,side=side,stable=stable,
            input_atomic=input_atomic,
            minimum_output_atomic=minimum_output_atomic,
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signature,
            idempotency_key=key,client=client)
    except Exception:
        _persist_submission(did,key,digest=digest,tx_hash=None)
        raise
    _persist_submission(did,key,digest=digest,tx_hash=tx_hash)
    return {"asset":"ETH","intent_key":key,"state":"SUBMITTED",
            "tx_hash":tx_hash,"real_privy_broadcast":True,
            "onchain_finalized":False,"portfolio_credited":False}
