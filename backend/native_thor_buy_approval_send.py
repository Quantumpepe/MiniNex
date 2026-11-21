"""Original BTC/SOL/XRP first-BUY THORChain ERC20 approval sender.

Internal-only owner-granted Ethereum wallet operation. No API, scheduler,
policy creation or automatic run. An already sufficient allowance sends
nothing. A partially used USDT allowance requires its own finalized approve(0)
before a separate finite approve; never batch unconfirmed steps. The original
owner account lock and shared approval journal serialize the Ethereum nonce
against ETH, LINK, UNI, fees and the native BUY source.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import os
import re
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_p256_signer import sign_privy_rpc_payload
from .eth_router02_live_quote import _rpc
from .live_eth_quotes import STABLES
from .native_eth_native_buy_preflight import _int_quantity
from .native_thor_buy_approval_plan import read_original_thor_buy_allowance
from .privy_live_signer import (
    _owned_eth_wallet, _scoped_signer, fetch_privy_wallet_metadata,
)
from .privy_wallet_verification import fetch_privy_user
from .shared_evm_allowance import owner_approval_draft
from .shared_evm_execution import bound_evm_wallet_rpc
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .shared_evm_router_approval_send import _finish as _finish_shared_approval
from .storage import _db_url, _ensure_schema, _safe_did

_ASSETS=frozenset(("BTC","SOL","XRP"))
_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_SHA=re.compile(r"^[a-f0-9]{64}$")
_EVENT=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_ADDR=re.compile(r"^0x[0-9a-fA-F]{40}$")
_NEEDED=("owner_wallet_current","exact_remote_policy_verified",
         "backend_quorum_current","owner_signed_budget_current")


def bound_original_thor_approval(
    plan:dict,*,asset:str,stable:str,wallet_id:str,now:int,
)->dict:
    """Pure independently reconstructible finite USDC/USDT approval call."""
    if (type(plan) is not dict or asset not in _ASSETS
            or stable not in STABLES or (asset=="XRP" and stable!="USDC")
            or type(now) is not int or now<=0
            or type(wallet_id) is not str or not _ID.fullmatch(wallet_id)
            or plan.get("mode")!="THORCHAIN_ORIGINAL_ERC20_APPROVAL_PREVIEW_ONLY"
            or plan.get("asset")!=asset or plan.get("side")!="BUY"
            or plan.get("stable")!=stable or plan.get("chain_id")!=1
            or plan.get("network")!="ethereum-mainnet"
            or plan.get("owner_wallet_needs_erc20_approval") is not True
            or any(plan.get(k) is not False for k in (
                "exact_approval_gas_verified","current_remote_privy_trade_policy_verified",
                "durable_approval_send_claimed","real_token_approval_sent",
                "actual_buy_source_sent","execution_enabled","can_submit"))):
        raise ValueError("Untrusted original native BUY approval route")
    try:
        quote_end=datetime.fromisoformat(plan["original_quote_expires_at"])
        required=int(plan["approval_amount_atomic"])
        token_balance=int(plan["source_balance_atomic"])
        allowance=int(plan["existing_router_allowance_atomic"])
        eth=int(plan["source_eth_gas_balance_wei"])
        wallet=plan["wallet_address"]
        router=plan["original_router"]
        if (quote_end.tzinfo is None or quote_end.timestamp()<=now
                or not 0<required<10**45 or not 0<=allowance<required
                or token_balance<required or eth<=0
                or type(wallet) is not str or not _ADDR.fullmatch(wallet)
                or type(router) is not str or not _ADDR.fullmatch(router)
                or plan["official_stable_token_address"].lower()!=STABLES[stable].lower()
                or type(plan.get("original_buy_unsigned_sha256")) is not str
                or not _SHA.fullmatch(plan["original_buy_unsigned_sha256"])):
            raise ValueError("Native BUY approval source or allowance invalid")
        expected=owner_approval_draft(
            token=STABLES[stable],router=router,wallet=wallet,
            chain_id=1,amount_atomic=required)
        reset=stable=="USDT" and 0<allowance<required
        zero={**expected,"data":expected["data"][:-64]+"0"*64} if reset else None
        if (plan.get("existing_usdt_allowance_requires_zero_reset") is not reset
                or plan.get("required_sequential_approval_receipts")!=(2 if reset else 1)
                or plan.get("exact_owner_approval_transaction")!=expected
                or plan.get("exact_owner_approval_reset_transaction")!=zero):
            raise ValueError("Unverified USDT reset or exact finite approval")
        mode="THOR_USDT_ZERO" if reset else "THOR_FINITE"
        tx=zero if reset else expected
        url,body=bound_evm_wallet_rpc(
            asset="ETH",wallet_id=wallet_id,wallet=wallet,
            tx={"from":wallet,"to":tx["to"],"chainId":1,
                "data":tx["data"],"value":"0"})
        return {
            "mode":mode,"asset":asset,"side":"BUY","stable":stable,
            "wallet":wallet.lower(),"wallet_id":wallet_id,
            "token":STABLES[stable].lower(),"router":router.lower(),
            "amount_atomic":required,"quote_expires_at":quote_end,
            "original_buy_unsigned_sha256":plan["original_buy_unsigned_sha256"],
            "eth_balance_wei":eth,
            "privy_url":url,"privy_body":body,
            "source_unsigned_tx":{"from":wallet,"to":tx["to"],
                                  "data":tx["data"],"value":"0x0"},
            "approval_is_signed":False,"approval_sent":False,
        }
    except (KeyError,TypeError,ValueError,OverflowError,AttributeError):
        raise ValueError("Unverified original native BUY approval context") from None


def _approval_key(
    *,did:str,asset:str,stable:str,signal:str,revision:int,prepared:dict,
)->str:
    raw=json.dumps(
        [did,asset,stable,signal,revision,prepared["mode"],
         prepared["wallet"],prepared["token"],prepared["router"],
         str(prepared["amount_atomic"])],
        separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")
    return "appr_"+hashlib.sha256(raw).hexdigest()[:58]


def _claim_original_thor_approval(
    *,did:str,asset:str,stable:str,prepared:dict,
    audit:dict,nonce:int,request_digest:str,key:str,
    observed_block:int,wire_sha256:str,
)->None:
    """Durable shared-wallet single-send claim; never acquires spending power."""
    if (asset not in _ASSETS or stable not in STABLES
            or type(nonce) is not int or not 0<=nonce<2**256
            or type(key) is not str or not re.fullmatch(r"appr_[0-9a-f]{58}",key)
            or type(request_digest) is not str or not _SHA.fullmatch(request_digest)
            or type(wire_sha256) is not str or not _SHA.fullmatch(wire_sha256)
            or type(observed_block) is not int
            or observed_block<=0 or observed_block>=2**256):
        raise HTTPException(409,"Native approval journal identity invalid")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",
                (did,))
            row=cur.fetchone()
            if type(row) is not dict or row.get("privy_did")!=did:
                raise HTTPException(409,"Original Privy owner account unavailable")
            table=("mininex_budget_preferences" if asset=="BTC"
                   else "mininex_extra_coin_preferences")
            cur.execute(
                f"SELECT revision FROM {table} WHERE privy_did=%s AND asset=%s FOR UPDATE",
                (did,asset))
            prefs=cur.fetchone()
            if (type(prefs) is not dict
                    or prefs.get("revision")!=audit["budget_revision"]):
                raise HTTPException(409,"Native coin budget changed before approval")
            # The exact original wallet's pending Ethereum nonce is shared
            # across ALL engines. These are transaction races, not frequency
            # or per-coin capital limits.
            queries=(
                ("""SELECT COUNT(*) AS pending FROM mininex_live_eth_auto_broadcasts
                    WHERE privy_did=%s AND status IN
                    ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",(did,)),
                ("""SELECT COUNT(*) AS pending FROM mininex_live_eth_auto_native_steps
                    WHERE privy_did=%s AND status IN
                    ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",(did,)),
                ("""SELECT COUNT(*) AS pending FROM mininex_live_evm_auto_broadcasts
                    WHERE privy_did=%s AND chain_id=1 AND status IN
                    ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",(did,)),
                ("""SELECT COUNT(*) AS pending FROM mininex_evm_auto_fee_sends
                    WHERE privy_did=%s AND chain_id=1 AND status IN
                    ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",(did,)),
                ("""SELECT COUNT(*) AS pending FROM mininex_live_evm_router_approval_sends
                    WHERE privy_did=%s AND chain_id=1 AND status IN
                    ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",(did,)),
                ("""SELECT COUNT(*) AS pending FROM mininex_live_native_order_intents
                    WHERE privy_did=%s AND chain='ethereum-mainnet'
                    AND side='BUY' AND status IN
                    ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",(did,)),
                ("""SELECT COUNT(*) AS pending FROM mininex_live_order_intents
                    WHERE privy_did=%s AND asset='ETH' AND state IN
                    ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED','REORG_PENDING')""",
                    (did,)),
            )
            for query,params in queries:
                cur.execute(query,params)
                pending=cur.fetchone()
                if (type(pending) is not dict
                        or type(pending.get("pending")) is not int
                        or pending["pending"]!=0):
                    raise HTTPException(409,"Original Ethereum wallet has unresolved send")
            # No second USDT approve while the reset is unfinalized.
            # The read-only allowance source already selected RESET when
            # current nonzero allowance was insufficient.
            cur.execute(
                """INSERT INTO mininex_live_evm_router_approval_sends
                   (privy_did,asset,side,stable,chain_id,approval_mode,
                    wallet_id,wallet_address,token_address,router_address,
                    amount_atomic,policy_sha256,budget_revision,idempotency_key,
                    request_digest,status,owner_eth_pending_nonce,
                    owner_eth_observed_block_number,
                    owner_eth_request_wire_sha256)
                   VALUES (%s,%s,'BUY',%s,1,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s,'SENDING',%s,%s,%s)""",
                (did,asset,stable,prepared["mode"],prepared["wallet_id"],
                 prepared["wallet"],prepared["token"],prepared["router"],
                 prepared["amount_atomic"],audit["policy_sha256"],
                 audit["budget_revision"],key,request_digest,nonce,
                 observed_block,wire_sha256))


async def submit_original_thor_approval_once(
    did:str,*,asset:str,stable:str,amount:str,
    signal_event_id:str,permission_audit:dict,client:httpx.AsyncClient,
)->dict:
    """One owner-granted original THOR buy allowance send, never called by API.

    The future internal worker must derive audit from a signed budget record
    and an independently reviewed *actual remote Privy policy*. No owner
    consent, no exact policy, or no complete quorum => no signing or RPC.
    """
    did=_safe_did(did)
    if (asset not in _ASSETS or stable not in STABLES
            or (asset=="XRP" and stable!="USDC")
            or type(signal_event_id) is not str
            or not _EVENT.fullmatch(signal_event_id)
            or type(client) is not httpx.AsyncClient
            or type(permission_audit) is not dict
            or permission_audit.get("did")!=did
            or permission_audit.get("asset")!=asset
            or permission_audit.get("stable")!=stable
            or permission_audit.get("source_amount")!=amount
            or type(permission_audit.get("budget_revision")) is not int
            or permission_audit["budget_revision"]<=0
            or type(permission_audit.get("policy_sha256")) is not str
            or not _SHA.fullmatch(permission_audit["policy_sha256"])
            or any(permission_audit.get(k) is not True for k in _NEEDED)):
        raise HTTPException(409,"Original native owner budget and Privy grant required")
    plan=await read_original_thor_buy_allowance(
        did,asset=asset,stable=stable,amount=amount)
    if plan.get("owner_wallet_needs_erc20_approval") is False:
        return {"state":"ALREADY_APPROVED","sent":False,
                "owner_grant_modified":False,"may_retry":False}
    owner=_owned_eth_wallet(await fetch_privy_user(did),did)
    if (owner["delegated"] is not True
            or owner["address"]!=plan.get("wallet_address","").lower()):
        raise HTTPException(409,"Original Ethereum funding wallet unverified")
    remote=await fetch_privy_wallet_metadata(owner["wallet_id"])
    signer=permission_audit.get("signer_id")
    policy=permission_audit.get("policy_id")
    if (type(signer) is not str or not _ID.fullmatch(signer)
            or type(policy) is not str or not _ID.fullmatch(policy)
            or signer==policy
            or not _scoped_signer(
                remote,did=did,address=owner["address"],
                config={"signer_id":signer,"policy_id":policy})):
        raise HTTPException(409,"Original ETH owner Privy signer not attached")
    now=int(time.time())
    try:
        prepared=bound_original_thor_approval(
            plan,asset=asset,stable=stable,wallet_id=owner["wallet_id"],now=now)
        if (str(permission_audit.get("source_wallet","")).lower()
                !=prepared["wallet"]
                or permission_audit.get("source_amount_atomic")
                !=str(prepared["amount_atomic"])):
            raise ValueError("Original owner grant differs from source wallet/amount")
        tx=prepared["source_unsigned_tx"]
        cid,nonce,gas,price,balance,height=await asyncio.gather(
            _rpc(client,method="eth_chainId",params=[],req_id=1),
            _rpc(client,method="eth_getTransactionCount",
                 params=[prepared["wallet"],"pending"],req_id=2),
            _rpc(client,method="eth_estimateGas",params=[tx],req_id=3),
            _rpc(client,method="eth_gasPrice",params=[],req_id=4),
            _rpc(client,method="eth_getBalance",
                 params=[prepared["wallet"],"latest"],req_id=5),
            _rpc(client,method="eth_blockNumber",params=[],req_id=6),
        )
        if (_int_quantity(cid)!=1
                or _int_quantity(height)<=0
                or not 21000<=_int_quantity(gas)<=30_000_000
                or _int_quantity(price)<=0
                or _int_quantity(balance)<_int_quantity(gas)*_int_quantity(price)
                or prepared["quote_expires_at"].timestamp()<=time.time()):
            raise ValueError("Native exact approval gas or source quote expired")
        pending_nonce=_int_quantity(nonce)
        observed_block=_int_quantity(height)
    except (ValueError,KeyError,TypeError,OverflowError,httpx.HTTPError):
        raise HTTPException(409,"Original first-BUY approval network state changed") from None
    key=_approval_key(
        did=did,asset=asset,stable=stable,signal=signal_event_id,
        revision=permission_audit["budget_revision"],prepared=prepared)
    url,body=prepared["privy_url"],prepared["privy_body"]
    digest=hashlib.sha256(json.dumps(
        [url,body,key,pending_nonce,
         prepared["original_buy_unsigned_sha256"]],
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    # Immutable ON-WIRE digest can be reconstructed from a finalized Ethereum
    # transaction even after the short THORChain quote has expired.
    wire_sha256=hashlib.sha256(json.dumps(
        [url,body,key,pending_nonce],
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    secret=os.environ.get("MININEX_PRIVY_APP_SECRET","").strip()
    if len(secret)<12:
        raise HTTPException(503,"Original Privy owner execution secret unavailable")
    await asyncio.to_thread(
        _claim_original_thor_approval,did=did,asset=asset,stable=stable,
        prepared=prepared,audit=permission_audit,nonce=pending_nonce,
        request_digest=digest,key=key,observed_block=observed_block,
        wire_sha256=wire_sha256)
    tx_hash=None
    try:
        signature=sign_privy_rpc_payload(
            url=url,body=body,idempotency_key=key)
        tx_hash=await post_claimed_privy_evm_rpc(
            url=url,body=body,app_secret=secret,
            authorization_signature=signature,
            idempotency_key=key,client=client)
    except BaseException:
        await asyncio.to_thread(
            _finish_shared_approval,did=did,asset=asset,key=key,
            digest=digest,tx_hash=None)
        raise
    state=await asyncio.to_thread(
        _finish_shared_approval,did=did,asset=asset,key=key,
        digest=digest,tx_hash=tx_hash)
    return {
        "mode":prepared["mode"],"asset":asset,"stable":stable,
        "state":state,"tx_hash":tx_hash,"sent":True,
        "source_trade_sent":False,"finalized":False,"may_retry":False,
        "owner_grant_modified":False,
    }
