"""Durable, one-shot automated Router02 ERC20 allowance for owner-granted EVM trades.

Only the user's previously accepted combined Privy policy can authorize this
specific finite approval. Claim SQL commits before signing/network POST; an
ambiguous result is terminal pending reconciliation, never retried.
"""
from __future__ import annotations

from datetime import datetime,timezone
import hashlib
import json
import logging
import os
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_allowance import (
    bound_owner_router_approval_rpc,inspect_combined_grant_router_approval,
    check_evm_source_allowance,
)
from .shared_evm_swap_receipts import _pair
from .shared_evm_remote_grant_audit import (
    get_owner_evm_remote_grant_audit,_read_grant_record,_grant_scope_digest,
)
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .eth_privy_p256_signer import sign_privy_rpc_payload
from .storage import _db_url,_ensure_schema,_safe_did
from .live_orders import valid_order_key

LOG=logging.getLogger("mininex.evm_owner_router_approval")


def _original_approval_id(*,did:str,asset:str,side:str,stable:str,
                          wallet:str,amount:int,revision:int)->str:
    raw=json.dumps([did,asset,side,stable,wallet,amount,revision],
                   separators=(",",":"),ensure_ascii=True).encode("ascii")
    return "appr_"+hashlib.sha256(raw).hexdigest()[:58]


def _claim_once_locked(
    cur,*,did:str,asset:str,side:str,stable:str,
    record:dict,reviewed:dict,revision:int,
    amount_atomic:int,key:str,request_digest:str,now:int,
)->None:
    pair=_pair(asset,stable,side)
    cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",
                (did,))
    owner=cur.fetchone()
    if type(owner) is not dict or owner.get("privy_did")!=did:
        raise HTTPException(409,"Cannot lock owner wallet for approval")
    cur.execute(
        """SELECT asset,stable,chain_id,wallet_id,wallet_address,owner_id,
                  signer_id,policy_id,policy_sha256,remote_policy_sha256,
                  authorization_method,status,grant_verified_at,
                  consent_sha256,budget_revision,expires_at,revoked_at,
                  candidate_json
           FROM mininex_live_evm_owner_permissions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    permission=cur.fetchone()
    if (type(permission) is not dict
            or permission.get("policy_sha256")!=record.get("policy_sha256")
            or permission.get("wallet_address")!=record.get("wallet_address")
            or permission.get("wallet_id")!=record.get("wallet_id")
            or permission.get("policy_id")!=record.get("policy_id")
            or permission.get("budget_revision")!=revision
            or permission.get("status")!="GRANT_VERIFIED"
            or permission.get("grant_verified_at") is None
            or permission.get("revoked_at") is not None
            or permission.get("remote_policy_sha256")!=permission.get("policy_sha256")
            or permission.get("authorization_method")!="PRIVY_SCOPED_GRANT"
            or permission.get("chain_id")!=pair["chain_id"]
            or permission.get("stable")!=stable
            or not isinstance(permission.get("expires_at"),datetime)
            or permission["expires_at"]<=datetime.fromtimestamp(now,timezone.utc)):
        raise HTTPException(409,"Original owner Privy grant changed")
    cur.execute(
        """SELECT stable,revision,budget_usd
           FROM mininex_extra_coin_preferences
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    settings=cur.fetchone()
    if (type(settings) is not dict or settings.get("stable")!=stable
            or settings.get("revision")!=revision):
        raise HTTPException(409,"Owner coin budget changed before approval")
    if permission.get("consent_sha256")!=_grant_scope_digest(
            did=did,record=permission,budget_usd=settings["budget_usd"]):
        raise HTTPException(409,"Original wallet owner grant does not cover this budget")
    current=inspect_combined_grant_router_approval(
        record=permission,asset=asset,side=side,stable=stable,
        amount_atomic=amount_atomic,now=now)
    if current!=reviewed:
        raise HTTPException(409,"Approved router amount or policy changed")
    cur.execute(
        """SELECT status,chain_id,stable,wallet_address,budget_revision,
                  started_by_owner_at
           FROM mininex_live_evm_auto_sessions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    session=cur.fetchone()
    if (type(session) is not dict or session.get("status")!="ACTIVE"
            or session.get("chain_id")!=pair["chain_id"]
            or session.get("stable")!=stable
            or session.get("wallet_address")!=record["wallet_address"]
            or session.get("budget_revision")!=revision
            or session.get("started_by_owner_at") is None):
        raise HTTPException(409,"Coin trading is not owner-started and active")
    # Same-wallet, same-chain nonce conflicts. Every participating sender
    # must also check this approval journal before claiming a new nonce.
    for table in (
        "mininex_live_evm_router_approval_sends",
        "mininex_live_evm_auto_broadcasts",
        "mininex_evm_auto_fee_sends",
    ):
        cur.execute(
            f"""SELECT COUNT(*) AS pending FROM {table}
                WHERE privy_did=%s AND chain_id=%s AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
            (did,pair["chain_id"]))
        pending=cur.fetchone()
        if type(pending) is not dict or pending.get("pending")!=0:
            raise HTTPException(409,"Earlier same-chain wallet send unresolved")
    if pair["chain_id"]==1:
        tables=("mininex_live_eth_auto_broadcasts",
                "mininex_live_eth_auto_native_steps")
    else:
        tables=("mininex_live_evm_native_auto_steps",
                "mininex_live_evm_native_sell_intents")
    for table in tables:
        where=("WHERE privy_did=%s" if pair["chain_id"]==1
               else "WHERE privy_did=%s AND chain_id=%s")
        cur.execute(
            f"""SELECT COUNT(*) AS pending FROM {table}
                {where} AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
            (did,) if pair["chain_id"]==1 else (did,pair["chain_id"]))
        pending=cur.fetchone()
        if type(pending) is not dict or pending.get("pending")!=0:
            raise HTTPException(409,"Earlier native/Ethereum send unresolved")
    cur.execute(
        """INSERT INTO mininex_live_evm_router_approval_sends
           (privy_did,asset,side,stable,chain_id,wallet_id,wallet_address,
            token_address,router_address,amount_atomic,policy_sha256,
            budget_revision,idempotency_key,request_digest,status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SENDING')""",
        (did,asset,side,stable,pair["chain_id"],record["wallet_id"],
         record["wallet_address"],pair["source_token"].lower(),
         pair["router"].lower(),amount_atomic,record["policy_sha256"],
         revision,key,request_digest))


def _claim(*,did:str,asset:str,side:str,stable:str,record:dict,
           reviewed:dict,revision:int,amount_atomic:int,key:str,
           digest:str,now:int)->None:
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _claim_once_locked(
                cur,did=did,asset=asset,side=side,stable=stable,
                record=record,reviewed=reviewed,revision=revision,
                amount_atomic=amount_atomic,key=key,
                request_digest=digest,now=now)


def _finish(*,did:str,asset:str,key:str,digest:str,tx_hash:str|None)->str:
    db=_db_url();_ensure_schema(db)
    state="SUBMITTED" if tx_hash else "RECONCILIATION_REQUIRED"
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """UPDATE mininex_live_evm_router_approval_sends
                   SET status=%s,tx_hash=%s,updated_at=now()
                   WHERE privy_did=%s AND asset=%s AND idempotency_key=%s
                     AND request_digest=%s AND status='SENDING'
                   RETURNING status""",
                (state,tx_hash,did,asset,key,digest))
            row=cur.fetchone()
            if type(row) is not dict or row.get("status")!=state:
                raise HTTPException(409,"Original approval send needs reconciliation")
    return state


async def submit_scoped_router_approval_once(
    *,did:str,asset:str,side:str,stable:str,amount_atomic:int,
    budget_revision:int,client:httpx.AsyncClient,
)->dict:
    """One approved owner-wallet ERC20 approve, not an arbitrary spender API."""
    did=_safe_did(did)
    if (type(client) is not httpx.AsyncClient
            or type(amount_atomic) is not int or amount_atomic<=0
            or type(budget_revision) is not int or budget_revision<1):
        raise ValueError("Invalid internal Router02 approval request")
    proof=await get_owner_evm_remote_grant_audit(did,asset)
    if any(proof.get(name) is not True for name in (
        "owner_wallet_current","exact_remote_policy_rules_verified",
        "privy_additional_signer_scoped","backend_quorum_current",
        "signed_coin_budget_intent_in_ledger",
    )):
        raise HTTPException(409,"No current original Privy owner trading grant")
    record=await __import__("asyncio").to_thread(_read_grant_record,did,asset)
    now=int(time.time())
    reviewed=inspect_combined_grant_router_approval(
        record=record,asset=asset,side=side,stable=stable,
        amount_atomic=amount_atomic,now=now)
    # This fresh onchain check is required even when the strategist's earlier
    # small probe showed zero allowance. Never send unnecessary approvals.
    pair=_pair(asset,stable,side)
    exact=format(__import__("decimal").Decimal(amount_atomic)
                 /__import__("decimal").Decimal(10**pair["source_decimals"]),"f")
    balance=await check_evm_source_allowance(
        did,asset=asset,side=side,stable=stable,amount=exact)
    if not balance.get("source_funds_sufficient"):
        raise HTTPException(409,"Actual source tokens insufficient for approval")
    if balance.get("router_allowance_sufficient"):
        return {"asset":asset,"state":"ALREADY_APPROVED","sent":False}
    wallet=record["wallet_address"]
    url,body=bound_owner_router_approval_rpc(
        asset=asset,side=side,stable=stable,
        wallet_id=record["wallet_id"],wallet=wallet,
        amount_atomic=amount_atomic)
    if (reviewed["transaction"]["to"].lower()
            !=body["params"]["transaction"]["to"].lower()
            or reviewed["transaction"]["data"]
            !=body["params"]["transaction"]["data"]):
        raise HTTPException(409,"Onchain approval differs from original grant")
    key=_original_approval_id(
        did=did,asset=asset,side=side,stable=stable,wallet=wallet,
        amount=amount_atomic,revision=budget_revision)
    key=valid_order_key(key)
    digest=hashlib.sha256(json.dumps(
        [url,body,record["policy_sha256"],budget_revision],
        separators=(",",":"),sort_keys=True,
    ).encode("ascii")).hexdigest()
    await __import__("asyncio").to_thread(
        _claim,did=did,asset=asset,side=side,stable=stable,
        record=record,reviewed=reviewed,revision=budget_revision,
        amount_atomic=amount_atomic,key=key,digest=digest,now=now)
    tx_hash=None
    try:
        signature=sign_privy_rpc_payload(
            url=url,body=body,idempotency_key=key)
        tx_hash=await post_claimed_privy_evm_rpc(
            url=url,body=body,
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signature,idempotency_key=key,
            client=client)
    except BaseException:
        await __import__("asyncio").to_thread(
            _finish,did=did,asset=asset,key=key,digest=digest,tx_hash=None)
        raise
    state=await __import__("asyncio").to_thread(
        _finish,did=did,asset=asset,key=key,digest=digest,tx_hash=tx_hash)
    return {"asset":asset,"state":state,"tx_hash":tx_hash,
            "sent":True,"finalized":False,"may_retry":False}
