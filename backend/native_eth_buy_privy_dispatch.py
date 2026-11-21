"""One-shot Ethereum source for original BTC/SOL/XRP THORChain BUY orders.

Internal only: no public route, scheduler, autonomous grant, or live activation.
The owner policy audit must be independently constructed from server state by
a future native strategy worker, never accepted from UI JSON. The original
nonce and Privy request digest commit with SENDING before the first POST.
Uncertain outcomes remain unresolved without any retry.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import time

from eth_abi import decode
import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_p256_signer import sign_privy_rpc_payload
from .live_eth_quotes import STABLES
from .native_eth_native_buy_preflight import _int_quantity
from .native_eth_thor_deposit_tx import _SELECTOR, encode_original_eth_native_buy_deposit
from .native_live_order_journal import _claim_once_locked, _record_send_result_locked
from .native_live_signed_dispatch import _get_original_prepared_source, _original_quote_digest
from .native_privy_owner_signer import get_original_native_privy_signer_status
from .privy_live_signer import _owned_eth_wallet, _scoped_signer, fetch_privy_wallet_metadata
from .privy_wallet_verification import fetch_privy_user
from .shared_evm_execution import bound_evm_wallet_rpc
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .storage import _db_url, _ensure_schema, _safe_did

_ASSETS=frozenset(("BTC","SOL","XRP"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_KEY=re.compile(r"^native_[0-9a-f]{64}$")
_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")


def bound_original_native_buy_rpc(
    order:dict,quote:dict,preflight:dict,*,wallet_id:str,now:int,
)->tuple[str,dict,int,str]:
    """Only construct a Privy RPC body from an exact current original BUY."""
    if (type(order) is not dict or type(quote) is not dict
            or type(preflight) is not dict or type(now) is not int
            or now<=0 or type(wallet_id) is not str or not _ID.fullmatch(wallet_id)
            or order.get("asset") not in _ASSETS or order.get("side")!="BUY"
            or order.get("chain")!="ethereum-mainnet"
            or order.get("status")!="PREPARED"
            or order.get("stable") not in STABLES
            or (order["asset"]=="XRP" and order["stable"]!="USDC")
            or quote.get("mode")!="THORCHAIN_DESTINATION_BOUND_READ_ONLY"
            or order.get("immutable_quote_sha256")!=_original_quote_digest(quote)
            or order.get("source_wallet")!=quote.get("source_wallet")
            or order.get("destination_wallet")!=quote.get("destination_wallet")
            or quote.get("source_asset")!=order["stable"]
            or quote.get("destination_asset")!=order["asset"]
            or quote.get("from_network")!="ethereum-mainnet"
            or quote.get("to_network")!={
                "BTC":"bitcoin-mainnet","SOL":"solana-mainnet",
                "XRP":"xrpl-mainnet",
            }[order["asset"]]
            or quote.get("quote_expires_at")!=order.get("quote_expires_at")
            or not isinstance(quote.get("quote_expires_at"),datetime)
            or quote["quote_expires_at"].tzinfo is None
            or not now<quote["quote_expires_at"].timestamp()):
        raise ValueError("Original native BUY identity or quote expired")
    try:
        if Decimal(str(order.get("input_amount")))!=Decimal(quote.get("amount")):
            raise ValueError("Original native BUY source amount changed")
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError("Original native BUY source amount invalid") from None
    if (preflight.get("mode")!="THORCHAIN_ETH_ORIGINAL_NATIVE_BUY_NETWORK_PREFLIGHT"
            or preflight.get("asset")!=order["asset"]
            or preflight.get("stable")!=order["stable"]
            or preflight.get("source_wallet")!=order["source_wallet"]
            or preflight.get("destination_wallet")!=order["destination_wallet"]
            or preflight.get("router")!=quote.get("inbound_router")
            or preflight.get("vault")!=quote.get("inbound_vault")
            or any(preflight.get(k) is not True for k in (
                "chain_id_verified","original_router_allowance_verified",
                "source_funds_and_gas_verified",
                "pending_nonce_and_gas_estimate_verified"))
            or any(preflight.get(k) is not False for k in (
                "independently_verified_privy_owner_grant","durable_native_order_claim",
                "ready_to_sign","wallet_signed","source_transaction_sent",
                "execution_enabled","can_submit"))):
        raise ValueError("Original native BUY network evidence unverified")
    checked=preflight.get("network_checked_at_unix")
    if type(checked) is not int or checked>now or checked<now-30:
        raise ValueError("Original native BUY nonce/gas observation stale")
    tx=preflight.get("unsigned_transaction")
    if (type(tx) is not dict or set(tx)!={
            "from","to","chainId","value","data","nonce","gas","gasPrice"}
            or type(tx.get("data")) is not str
            or not tx["data"].startswith("0x"+_SELECTOR.hex())
            or len(tx["data"])>2000 or len(tx["data"])%2):
        raise ValueError("Original native BUY calldata unavailable")
    try:
        vault,token,amount,memo,router_expiry=decode(
            ["address","address","uint256","string","uint256"],
            bytes.fromhex(tx["data"][10:]),
        )
        issued=router_expiry-90*60
        if (issued<=0 or issued>checked or router_expiry<=now
                or vault.lower()!=quote["inbound_vault"].lower()
                or token.lower()!=STABLES[order["stable"]].lower()
                or memo!=quote["memo"]):
            raise ValueError("Original native BUY source fields changed")
        unsigned=encode_original_eth_native_buy_deposit(
            quote,asset=order["asset"],stable=order["stable"],
            amount=quote["amount"],now=issued)
        if any(tx.get(k)!=v for k,v in unsigned["unsigned_transaction"].items()):
            raise ValueError("Original native BUY calldata differs from private quote")
        if (str(amount)!=unsigned["source_amount_atomic"]
                or str(amount)!=preflight.get("source_amount_atomic")):
            raise ValueError("Original native BUY source amount changed")
        nonce=_int_quantity(tx["nonce"])
        gas=_int_quantity(tx["gas"])
        price=_int_quantity(tx["gasPrice"])
        allowance=int(preflight["source_allowance_atomic"])
        owned_eth=int(preflight["verified_eth_balance_wei"])
        owned_token=int(preflight["verified_stable_balance_atomic"])
        if (gas<21_000 or gas>30_000_000 or price<=0 or
                owned_eth<gas*price or owned_token<amount
                or allowance<amount or amount<=0):
            raise ValueError("Original native BUY insufficient source funds")
        expected=hashlib.sha256(json.dumps(
            [unsigned["unsigned_transaction_sha256"],tx,allowance,
             owned_eth,owned_token,checked],sort_keys=True,
            separators=(",",":"),ensure_ascii=True,
        ).encode("ascii")).hexdigest()
        if preflight.get("unsigned_transaction_sha256")!=expected:
            raise ValueError("Original native BUY evidence digest changed")
    except (ValueError,KeyError,TypeError,OverflowError,IndexError):
        raise ValueError("Original native BUY cannot bind verified Ethereum source") from None
    url,body=bound_evm_wallet_rpc(
        asset="ETH",wallet_id=wallet_id,wallet=order["source_wallet"],
        # Normalized for identical original Privy request digest during
        # passive canonical Ethereum transaction reconstruction.
        tx={"from":tx["from"],"to":tx["to"].lower(),"chainId":1,
            "data":tx["data"],"value":"0"})
    digest=hashlib.sha256(json.dumps(
        [url,body,order["payload_sha256"],nonce],
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return url,body,nonce,digest


def _claim_eth_buy_once(
    *,did:str,asset:str,key:str,digest:str,
    permission_audit:dict,nonce:int,request_digest:str,
    observed_block_number:int,
)->None:
    """Owner lock and source ETH nonce are committed before any Privy POST."""
    if (type(nonce) is not int or nonce<0 or nonce>=2**256
            or type(observed_block_number) is not int
            or observed_block_number<=0 or observed_block_number>=2**256
            or not _SHA.fullmatch(request_digest)):
        raise ValueError("Original ETH source nonce or observed block invalid")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _claim_once_locked(
                cur,did=did,asset=asset,key=key,digest=digest,
                permission_audit=permission_audit)
            cur.execute(
                """UPDATE mininex_live_native_order_intents
                   SET source_eth_pending_nonce=%s,
                       source_rpc_request_sha256=%s,
                       source_eth_observed_block_number=%s,updated_at=now()
                   WHERE privy_did=%s AND asset=%s AND intent_key=%s
                     AND chain='ethereum-mainnet' AND side='BUY'
                     AND status='SENDING' AND payload_sha256=%s
                     AND source_eth_pending_nonce IS NULL
                     AND source_rpc_request_sha256 IS NULL
                     AND source_eth_observed_block_number IS NULL
                   RETURNING source_rpc_request_sha256""",
                (nonce,request_digest,observed_block_number,
                 did,asset,key,digest))
            row=cur.fetchone()
            if (type(row) is not dict or
                    row.get("source_rpc_request_sha256")!=request_digest):
                raise HTTPException(409,"Original ETH BUY source already claimed")


def _finish_eth_buy_once(
    *,did:str,asset:str,key:str,digest:str,
    request_digest:str,tx_hash:str|None,
)->str:
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT source_rpc_request_sha256,status
                   FROM mininex_live_native_order_intents
                   WHERE privy_did=%s AND asset=%s AND intent_key=%s
                   FOR UPDATE""",(did,asset,key))
            row=cur.fetchone()
            if (type(row) is not dict or row.get("status")!="SENDING"
                    or row.get("source_rpc_request_sha256")!=request_digest):
                raise HTTPException(409,"Original ETH BUY journal conflict")
            return _record_send_result_locked(
                cur,did=did,asset=asset,key=key,digest=digest,
                tx_hash=tx_hash)


async def submit_original_eth_native_buy_once(
    did:str,*,asset:str,intent_key:str,quote:dict,preflight:dict,
    permission_audit:dict,client:httpx.AsyncClient,
)->dict:
    """Internal real source sender; no public API or automatic worker calls it."""
    did=_safe_did(did)
    if (asset not in _ASSETS or type(intent_key) is not str
            or not _KEY.fullmatch(intent_key)
            or type(client) is not httpx.AsyncClient
            or type(permission_audit) is not dict
            or permission_audit.get("did")!=did
            or permission_audit.get("asset")!=asset
            or any(permission_audit.get(k) is not True for k in (
                "owner_wallet_current","exact_remote_policy_verified",
                "backend_quorum_current","owner_signed_budget_current"))):
        raise HTTPException(409,"Native BUY requires original signed owner policy")
    order=_get_original_prepared_source(did,asset,intent_key)
    if any(order.get(k)!=permission_audit.get(k) for k in (
            "payload_sha256","owner_consent_sha256","privy_grant_sha256",
            "budget_revision","source_wallet")):
        raise HTTPException(409,"Native BUY grant does not match original order")
    destination=await get_original_native_privy_signer_status(did,asset=asset)
    if any(destination.get(k) is not True for k in (
            "original_wallet_remote_owner_verified",
            "original_public_key_matches_wallet",
            "privy_native_wallet_api_supported")):
        raise HTTPException(409,"Original native receiving wallet unverified")
    owner=_owned_eth_wallet(await fetch_privy_user(did),did)
    if (owner["delegated"] is not True
            or owner["address"]!=order["source_wallet"].lower()):
        raise HTTPException(409,"Original ETH funding wallet not delegated")
    remote=await fetch_privy_wallet_metadata(owner["wallet_id"])
    signer=permission_audit.get("signer_id")
    policy=permission_audit.get("policy_id")
    if (type(signer) is not str or not _ID.fullmatch(signer)
            or type(policy) is not str or not _ID.fullmatch(policy)
            or signer==policy
            or not _scoped_signer(
                remote,did=did,address=owner["address"],
                config={"signer_id":signer,"policy_id":policy})):
        raise HTTPException(409,"Original native BUY Privy signer not attached")
    try:
        url,body,nonce,request_digest=bound_original_native_buy_rpc(
            order,quote,preflight,wallet_id=owner["wallet_id"],now=int(time.time()))
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"Original ETH BUY source no longer current") from None
    observed_block_number=preflight.get(
        "source_ethereum_observed_block_number")
    if (type(observed_block_number) is not int
            or observed_block_number<=0):
        raise HTTPException(409,"Original ETH source observation block unavailable")
    secret=os.environ.get("MININEX_PRIVY_APP_SECRET","").strip()
    if len(secret)<12:
        raise HTTPException(503,"Privy ETH funding secret unavailable")
    await asyncio.to_thread(
        _claim_eth_buy_once,did=did,asset=asset,key=intent_key,
        digest=order["payload_sha256"],permission_audit=permission_audit,
        nonce=nonce,request_digest=request_digest,
        observed_block_number=observed_block_number)
    tx_hash=None
    try:
        signature=sign_privy_rpc_payload(
            url=url,body=body,idempotency_key=intent_key)
        tx_hash=await post_claimed_privy_evm_rpc(
            url=url,body=body,app_secret=secret,
            authorization_signature=signature,
            idempotency_key=intent_key,client=client)
    except BaseException:
        await asyncio.to_thread(
            _finish_eth_buy_once,did=did,asset=asset,key=intent_key,
            digest=order["payload_sha256"],request_digest=request_digest,
            tx_hash=None)
        raise
    state=await asyncio.to_thread(
        _finish_eth_buy_once,did=did,asset=asset,key=intent_key,
        digest=order["payload_sha256"],request_digest=request_digest,
        tx_hash=tx_hash)
    return {
        "mode":"ORIGINAL_NATIVE_ETH_SOURCE_SUBMITTED_PENDING_FINALITY",
        "asset":asset,"side":"BUY","intent_key":intent_key,
        "source_tx_hash":tx_hash,"journal_state":state,
        "source_chain_finalized":False,"native_destination_settled":False,
        "fee_assessed":False,"retry_allowed":False,
    }
