"""Durable one-shot Privy fee transfer claim for a real 5%-HWM debt.

An actual independent owner-signed REMOTE fee grant is freshly checked
BEFORE a permanent send claim. The original fee amount comes exclusively
from the immutable HWM obligation and current real stablecoin price. A
committed SENDING journal is never reverted or retried even if Privy
returns an unknown status. A claim cannot overtake another same-wallet
EVM swap/native conversion or fee send on that network.

No client endpoint accepts a transaction, amount, wallet or treasury here.
"""
from __future__ import annotations

from datetime import datetime,timezone
import hashlib
import json
import logging
import re

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_fee_payment import _prepare_locked
from .shared_evm_fee_transfer_proof import owner_fee_transfer_draft
from .shared_evm_swap_receipts import _pair
from .shared_evm_execution import bound_evm_wallet_rpc
from .storage import _db_url,_ensure_schema,_safe_did
from .live_orders import valid_order_key

LOG=logging.getLogger("mininex.evm_fee_auto_send_claim")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI","ARB"))
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_SHA=re.compile(r"^[0-9a-f]{64}$")


def immutable_fee_rpc_identity(
    *,asset:str,key:str,wallet_id:str,draft:dict,
    policy_sha256:str,
)->dict:
    """Bind a single exact ERC20 transfer to the granted policy and HWM id."""
    if (asset not in _ASSETS or type(draft) is not dict
            or draft.get("mode")!="OWNER_MANUALLY_SIGNED_MININEX_FEE_ONLY"
            or draft.get("asset")!=asset
            or type(policy_sha256) is not str or not _SHA.fullmatch(policy_sha256)
            or type(wallet_id) is not str):
        raise ValueError("No exact verified fee payment instruction")
    key=valid_order_key(key)
    stable=draft.get("token_symbol")
    pair=_pair(asset,stable,"BUY")
    tx=draft.get("unsigned_transaction")
    if (type(tx) is not dict or tx.get("from")!=draft.get("owner_wallet")
            or tx.get("chainId")!=pair["chain_id"]
            or tx.get("to")!=pair["source_token"].lower()
            or tx.get("value")!="0x0"
            or tx.get("data")!=(
                "0xa9059cbb"+
                f"{int(draft['treasury_wallet'],16):064x}"+
                f"{int(draft['amount_atomic']):064x}")
            or draft.get("token_address")!=pair["source_token"].lower()):
        raise ValueError("Unsigned auto fee differs from approved ERC20 transfer")
    rpc_tx=dict(tx,value="0")
    url,body=bound_evm_wallet_rpc(
        asset=asset,wallet_id=wallet_id,
        wallet=draft["owner_wallet"],tx=rpc_tx)
    digest=hashlib.sha256(json.dumps([
        asset,key,policy_sha256,url,body
    ],sort_keys=True,separators=(",",":")).encode("ascii")).hexdigest()
    idem="fee_"+digest[:60]
    return {
        "asset":asset,"execution_id":key,"chain_id":pair["chain_id"],
        "stable":stable,"owner_wallet":draft["owner_wallet"],
        "wallet_id":wallet_id,"treasury_wallet":draft["treasury_wallet"],
        "token_address":draft["token_address"],
        "amount_atomic":draft["amount_atomic"],
        "request_digest":digest,"idempotency_key":idem,
        "url":url,"body":body,
    }


def _claim_single_auto_fee_locked(
    cur,*,did:str,asset:str,key:str,remote:dict,
    stable_usd,treasury:str,now:int,
)->dict:
    """All source evidence and same-account nonce claims are SQL-atomic."""
    if (asset not in _ASSETS or type(remote) is not dict
            or remote.get("owner_fee_grant_remotely_verified") is not True
            or remote.get("signer_quorum_remotely_verified") is not True
            or remote.get("asset")!=asset
            or type(now) is not int or now<=0):
        raise ValueError("Recent owner-scoped fee permission required")
    key=valid_order_key(key)
    cur.execute(
        """SELECT privy_did FROM mininex_accounts
           WHERE privy_did=%s FOR UPDATE""",(did,))
    owner=cur.fetchone()
    if type(owner) is not dict or owner.get("privy_did")!=did:
        raise HTTPException(409,"Cannot lock original owner fee wallet")
    cur.execute(
        """SELECT stable,chain_id,wallet_id,wallet_address,treasury_wallet,
                  owner_max_fee_atomic,expires_at,status,policy_id,
                  policy_sha256,remote_policy_sha256,owner_signed_at,
                  grant_verified_at,revoked_at
           FROM mininex_evm_fee_auto_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    grant=cur.fetchone()
    if (type(grant) is not dict
            or grant.get("status")!="GRANT_VERIFIED"
            or grant.get("owner_signed_at") is None
            or grant.get("grant_verified_at") is None
            or grant.get("revoked_at") is not None
            or not isinstance(grant.get("expires_at"),datetime)
            or grant["expires_at"].tzinfo is None
            or grant["expires_at"]<=datetime.fromtimestamp(now,timezone.utc)
            or grant.get("remote_policy_sha256")!=grant.get("policy_sha256")
            or grant.get("chain_id")!=remote.get("chain_id")
            or grant.get("wallet_id")!=remote.get("wallet_id")
            or grant.get("wallet_address")!=remote.get("owner_wallet")
            or grant.get("stable")!=remote.get("stable")
            or grant.get("policy_id")!=remote.get("policy_id")
            or grant.get("policy_sha256")!=remote.get("policy_sha256")
            or grant.get("treasury_wallet")!=remote.get("treasury_wallet")
            or grant.get("treasury_wallet")!=treasury):
        raise HTTPException(409,"Original approved fee grant changed before SEND")
    pair=_pair(asset,grant["stable"],"BUY")
    if pair["chain_id"]!=grant["chain_id"]:
        raise HTTPException(409,"Wrong network in owner's fee permission")
    # Any same-owner same-chain broadcast or native conversion uses the
    # same Privy account nonce; no concurrent fee send can race it.
    cur.execute(
        """SELECT COUNT(*) AS pending
           FROM mininex_live_evm_auto_broadcasts
           WHERE privy_did=%s AND chain_id=%s AND status IN
             ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
        (did,pair["chain_id"]))
    active=cur.fetchone()
    if type(active) is not dict or active.get("pending")!=0:
        raise HTTPException(409,"Earlier EVM wallet trade send is unresolved")
    if pair["chain_id"]==1:
        for table in (
            "mininex_live_eth_auto_broadcasts",
            "mininex_live_eth_auto_native_steps",
        ):
            cur.execute(
                f"""SELECT COUNT(*) AS pending FROM {table}
                    WHERE privy_did=%s AND status IN
                    ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",(did,))
            row=cur.fetchone()
            if type(row) is not dict or row.get("pending")!=0:
                raise HTTPException(409,"Earlier ETH wallet send is unresolved")
    else:
        for table in (
            "mininex_live_evm_native_auto_steps",
            "mininex_live_evm_native_sell_intents",
        ):
            cur.execute(
                f"""SELECT COUNT(*) AS pending FROM {table}
                    WHERE privy_did=%s AND chain_id=%s
                      AND status IN
                    ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
                (did,pair["chain_id"]))
            row=cur.fetchone()
            if type(row) is not dict or row.get("pending")!=0:
                raise HTTPException(409,"Earlier native wallet send is unresolved")
    cur.execute(
        """SELECT COUNT(*) AS pending FROM mininex_evm_auto_fee_sends
           WHERE privy_did=%s AND chain_id=%s
             AND status IN
               ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
        (did,pair["chain_id"]))
    pending=cur.fetchone()
    if type(pending) is not dict or pending.get("pending")!=0:
        raise HTTPException(409,"Original fee wallet network send unresolved")
    cur.execute(
        """SELECT COUNT(*) AS pending
           FROM mininex_live_evm_router_approval_sends
           WHERE privy_did=%s AND chain_id=%s
             AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
        (did,pair["chain_id"]))
    approval=cur.fetchone()
    if type(approval) is not dict or approval.get("pending")!=0:
        raise HTTPException(409,"Original token router approval is still onchain")

    # Do not autosend if an existing manual-payment draft might have
    # already been signed outside MiniNex; require a fresh unique debt.
    cur.execute(
        """SELECT state FROM mininex_evm_verified_fee_payments
           WHERE privy_did=%s AND asset=%s AND execution_id=%s FOR UPDATE""",
        (did,asset,key))
    if cur.fetchone() is not None:
        raise HTTPException(409,"Fee payment already prepared/sent; never auto-retry")
    draft=_prepare_locked(
        cur,did=did,asset=asset,key=key,
        wallet=grant["wallet_address"],treasury=treasury,
        rate=stable_usd)
    identity=immutable_fee_rpc_identity(
        asset=asset,key=key,wallet_id=grant["wallet_id"],
        draft=draft,policy_sha256=grant["policy_sha256"])
    if (identity["chain_id"]!=grant["chain_id"]
            or identity["stable"]!=grant["stable"]
            or identity["owner_wallet"]!=grant["wallet_address"]
            or int(identity["amount_atomic"])>int(grant["owner_max_fee_atomic"])):
        raise HTTPException(409,"Original fee exceeds owner's explicit Privy amount")
    cur.execute(
        """INSERT INTO mininex_evm_auto_fee_sends
           (privy_did,asset,execution_id,chain_id,stable,wallet_id,
            wallet_address,treasury_wallet,policy_id,policy_sha256,
            token_address,amount_atomic,request_digest,idempotency_key,
            status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SENDING')""",
        (did,asset,key,identity["chain_id"],identity["stable"],
         identity["wallet_id"],identity["owner_wallet"],
         identity["treasury_wallet"],grant["policy_id"],
         grant["policy_sha256"],identity["token_address"],
         int(identity["amount_atomic"]),identity["request_digest"],
         identity["idempotency_key"]))
    return identity


def claim_one_owner_authorized_fee(
    *,did:str,asset:str,execution_id:str,remote:dict,
    stable_usd,treasury:str,now:int,
)->dict:
    did=_safe_did(did);key=valid_order_key(execution_id)
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _claim_single_auto_fee_locked(
                cur,did=did,asset=asset,key=key,remote=remote,
                stable_usd=stable_usd,treasury=treasury,now=now)


def _record_auto_fee_send_locked(
    cur,*,did:str,asset:str,key:str,digest:str,tx_hash:str|None,
)->str:
    if type(digest) is not str or not _SHA.fullmatch(digest):
        raise ValueError("Invalid original fee transaction digest")
    if tx_hash is not None and (
        type(tx_hash) is not str or not _HASH.fullmatch(tx_hash)):
        raise ValueError("Invalid original fee submission hash")
    cur.execute(
        """SELECT status,request_digest FROM mininex_evm_auto_fee_sends
           WHERE privy_did=%s AND asset=%s AND execution_id=%s FOR UPDATE""",
        (did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict or row.get("status")!="SENDING"
            or row.get("request_digest")!=digest):
        raise HTTPException(409,"Original fee send has already been claimed")
    new="SUBMITTED" if tx_hash is not None else "RECONCILIATION_REQUIRED"
    cur.execute(
        """UPDATE mininex_evm_auto_fee_sends
           SET status=%s,tx_hash=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND execution_id=%s
             AND status='SENDING' RETURNING status""",
        (new,tx_hash,did,asset,key))
    updated=cur.fetchone()
    if type(updated) is not dict or updated.get("status")!=new:
        raise HTTPException(409,"Concurrent fee send finality state conflict")
    return new


def record_original_auto_fee_send(
    *,did:str,asset:str,execution_id:str,digest:str,tx_hash:str|None,
)->dict:
    did=_safe_did(did);key=valid_order_key(execution_id)
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            state=_record_auto_fee_send_locked(
                cur,did=did,asset=asset,key=key,
                digest=digest,tx_hash=tx_hash)
    return {
        "asset":asset,"execution_id":key,"state":state,
        "tx_hash":tx_hash,"retry_allowed":False,
    }
