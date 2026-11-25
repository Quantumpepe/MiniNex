"""Internal one-shot direct Across sender using the original Privy EVM wallet.
No public send route. Only current user-started sessions, exact direct scope
and a durable original journal claim may reach the existing Privy transport.
Nested calls are not authorized. Finite bridge approvals use the existing
ERC20 approval journal and passive canonical receipt reconciliation.
"""
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException

from .ethereum_settlement_plan import check_evm_quote_route,CROSS_COIN_CONTRACTS
from .ethereum_settlement_calldata import inspect_settlement_route,DIAMOND
from .ethereum_settlement_journal import claim_authorized_submission_locked,record_claimed_submission_locked,pending_bridge_order
from .ethereum_settlement_funding import assert_ethereum_funds_locked
from .shared_evm_router_approval_send import _finish as finish_approval
from .shared_evm_consolidated_wallet_policy import DIRECT_SETTLEMENT_POLICY,DIRECT_SETTLEMENT_ASSETS
from .shared_evm_central_runtime import _locked_original_evm_coin
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .eth_privy_p256_signer import sign_privy_rpc_payload
from .storage import _db_url,_ensure_schema,_safe_did


def check_direct_bridge_scope(quote,record,*,now):
    """Bind the exact ERC20 source, destination, recipient and approved cap."""
    route=check_evm_quote_route(quote)
    asset=quote["asset"];side=quote["side"];stable=quote["stable"]
    scope=record.get("scope",{})
    if (asset not in DIRECT_SETTLEMENT_ASSETS
            or record.get("authorization_method")!="PRIVY_CENTRAL_ONE_SIGNER"
            or record.get("status")!="GRANT_VERIFIED" or record.get("revoked_at") is not None
            or record.get("asset")!=asset or record.get("stable")!=stable
            or record.get("wallet_address")!=quote["wallet_address"]
            or scope.get("asset")!=asset or scope.get("stable")!=stable
            or scope.get("settlement_policy")!=DIRECT_SETTLEMENT_POLICY
            or scope.get("owner_max_fee_atomic")!="0"
            or scope.get("expires_at",0)<=now
            or not quote["issued_at"]<=now<quote["expires_at"]):
        raise HTTPException(409,"Original direct bridge wallet permission unavailable")
    coin=CROSS_COIN_CONTRACTS[asset]
    if (quote["source_token"],quote["destination_token"])!=(
            (route["settlement_contract"],coin) if side=="BUY" else (coin,route["settlement_contract"])):
        raise HTTPException(409,"Original wrapped inventory or Ethereum settlement token differs")
    cap=int(scope["buy_source_atomic_cap" if side=="BUY" else "sell_source_atomic_cap"])
    if not 0<int(quote["input_atomic"])<=cap:
        raise HTTPException(409,"Amount exceeds original user-approved policy")
    inspected=inspect_settlement_route(quote["unsigned_transaction"],terms=quote,now=now)
    if (inspected["provider"]!="ACROSS_V4" or inspected["source_swap"]
            or inspected["destination_swap"] or inspected["route_sha256"]!=quote["route_sha256"]
            or int(inspected["transaction"]["value"])!=0):
        raise HTTPException(409,"Route is not covered by direct token bridge policy")
    name=f"MiniNex {asset} {side} {stable} token"
    rules=[r for r in record["candidate_json"]["rules"] if r["name"]==name
           and r["method"]=="eth_sendTransaction" and r["action"]=="ALLOW"]
    if len(rules)!=1:
        raise HTTPException(409,"Original remote-reviewed direct policy rule missing")
    tx=inspected["transaction"]
    url="https://api.privy.io/v1/wallets/"+record["wallet_id"]+"/rpc"
    body={"method":"eth_sendTransaction","caip2":"eip155:"+str(tx["chainId"]),
          "params":{"transaction":{"to":tx["to"],"data":tx["data"],"value":tx["value"]}}}
    return url,body


def check_direct_bridge_approval(quote,record,plan,*,now):
    """Only the quote's ERC20, exact LI.FI spender and finite input amount."""
    url,swap=check_direct_bridge_scope(quote,record,now=now)
    amount=int(quote["input_atomic"])
    allowance=int(plan["verified_source_allowance_atomic"])
    balance=int(plan["verified_source_balance_atomic"])
    if (not 0<=allowance<amount<=balance
            or plan.get("gas_estimate_kind")!="APPROVAL_ONLY"
            or plan.get("route_sha256")!=quote["route_sha256"]
            or plan.get("quote_id")!=str(quote["quote_id"])):
        raise HTTPException(409,"Original insufficient bridge allowance not verified")
    zero=quote["source_chain_id"]==1 and quote["stable"]=="USDT" and quote["side"]=="BUY" and allowance>0
    value=0 if zero else amount
    expected={"from":quote["wallet_address"],"to":quote["source_token"],
        "data":"0x095ea7b3"+DIAMOND[2:].rjust(64,"0")+format(value,"064x"),"value":"0x0"}
    if plan.get("manual_approval")!=expected:
        raise HTTPException(409,"Original finite bridge approval differs")
    name=f"MiniNex {quote['asset']} {quote['side']} {quote['stable']} token allowance"
    rules=[r for r in record["candidate_json"]["rules"] if r["name"]==name
           and r["method"]=="eth_sendTransaction" and r["action"]=="ALLOW"]
    if len(rules)!=1:
        raise HTTPException(409,"Original remote-reviewed approval rule missing")
    body={"method":"eth_sendTransaction","caip2":swap["caip2"],
        "params":{"transaction":{"to":expected["to"],"data":expected["data"],"value":"0"}}}
    return url,body,("BRIDGE_USDT_ZERO" if zero else "BRIDGE_FINITE")


def _identity(did,asset,started,signal_time):
    if not isinstance(started,datetime) or started.tzinfo is None or type(signal_time) is not int:
        raise ValueError("Original user-started strategy event required")
    # One original market event per session/coin, independent of requotes.
    return "bridge_"+hashlib.sha256(json.dumps([did,asset,started.isoformat(),signal_time//60],
        separators=(",",":")).encode()).hexdigest()


def _claim(did,quote,record,started,key,digest,plan,funding,position_usd,gas_usd,stable_usd,approval_mode=None):
    db=_db_url();_ensure_schema(db)
    asset=quote["asset"];stable=quote["stable"];now=int(time.time())
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",(did,))
            if cur.fetchone()!={"privy_did":did}:
                raise HTTPException(409,"Original owner account unavailable")
            current=_locked_original_evm_coin(cur,did=did,asset=asset,stable=stable,
                revision=record["budget_revision"],now=now)
            if any(current[k]!=record[k] for k in ("wallet_id","wallet_address","policy_sha256","policy_id","signer_id","scope")):
                raise HTTPException(409,"Original Privy remote proof changed")
            check_direct_bridge_scope(quote,current,now=now)
            cur.execute("""SELECT status,started_by_owner_at,stable,budget_revision,wallet_address
                FROM mininex_live_evm_auto_sessions WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
            session=cur.fetchone()
            allowed_status=("ACTIVE","EXIT_PENDING") if quote["side"]=="SELL" else ("ACTIVE",)
            if (not session or session["status"] not in allowed_status or session["started_by_owner_at"]!=started
                    or session["stable"]!=stable or session["budget_revision"]!=record["budget_revision"]
                    or session["wallet_address"]!=record["wallet_address"]):
                raise HTTPException(409,"Original user session paused or replaced")
            # Both approval and swap use the existing account/chain journals.
            chain=quote["source_chain_id"]
            if approval_mode is not None:
                _,_,mode=check_direct_bridge_approval(quote,current,plan,now=now)
                if mode!=approval_mode:
                    raise HTTPException(409,"Bridge approval changed")
            for table in ("mininex_live_evm_router_approval_sends","mininex_live_evm_auto_broadcasts",
                          "mininex_evm_auto_fee_sends","mininex_live_evm_native_auto_steps",
                          "mininex_live_evm_native_sell_intents"):
                staged=(" UNION ALL SELECT 1 FROM mininex_avax_staged_steps "
                        "WHERE privy_did=%s AND chain_id=%s AND state IN ('SENDING','SUBMITTED','UNCERTAIN')"
                        if table=="mininex_live_evm_router_approval_sends" else "")
                cur.execute(f"""SELECT 1 FROM {table} WHERE privy_did=%s AND chain_id=%s
                    AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')"""+staged+" LIMIT 1",
                    (did,chain,did,chain) if staged else (did,chain))
                if cur.fetchone() is not None:
                    raise HTTPException(409,"Earlier same-chain wallet send unresolved")
            if chain==1:
                for table in ("mininex_live_eth_auto_broadcasts","mininex_live_eth_auto_native_steps"):
                    cur.execute(f"""SELECT 1 FROM {table} WHERE privy_did=%s
                        AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,))
                    if cur.fetchone() is not None:
                        raise HTTPException(409,"Earlier Ethereum wallet send unresolved")
                cur.execute("""SELECT 1 FROM mininex_live_native_order_intents WHERE privy_did=%s
                    AND chain='ethereum-mainnet' AND status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED') LIMIT 1""",(did,))
                if cur.fetchone() is not None:
                    raise HTTPException(409,"Earlier native Ethereum send unresolved")
            # A previous local-chain order must settle before switching that coin.
            cur.execute("""SELECT 1 FROM mininex_live_evm_budget_reservations
                WHERE privy_did=%s AND asset=%s AND state IN
                ('RESERVED','SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                UNION ALL SELECT 1 FROM mininex_live_evm_native_sell_intents
                WHERE privy_did=%s AND asset=%s AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                UNION ALL SELECT 1 FROM mininex_live_evm_native_auto_steps
                WHERE privy_did=%s AND asset=%s AND status IN
                ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
                LIMIT 1""",(did,asset,did,asset,did,asset))
            if cur.fetchone() is not None:
                raise HTTPException(409,"Previous local-chain order requires reconciliation")
            balance=int(plan["verified_source_balance_atomic"])
            extra={}
            if quote["side"]=="BUY":
                if (not funding or funding["wallet_id"]!=record["wallet_id"]
                        or funding["wallet"]!=record["wallet_address"]):
                    raise HTTPException(409,"Original funding wallet changed")
                balance=min(balance,int(funding["stable_atomic"]))
                cur.execute("""SELECT budget_usd FROM mininex_extra_coin_preferences
                    WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
                budget=cur.fetchone()
                if not budget:
                    raise HTTPException(409,"Original coin budget unavailable")
                extra=dict(buy_budget_usd=budget["budget_usd"],position_usd=position_usd,gas_usd=gas_usd)
            else:
                from .ethereum_settlement_auto_plan import strategy_bridge_position_locked
                owned=strategy_bridge_position_locked(cur,did,asset,record["wallet_address"])
                if owned<int(quote["input_atomic"]):
                    raise HTTPException(409,"Original strategy-owned position changed")
            if approval_mode is not None:
                if pending_bridge_order(cur,did,asset):
                    raise HTTPException(409,"Earlier bridge order unresolved")
                if balance<int(quote["input_atomic"]):
                    raise HTTPException(409,"Verified source balance insufficient")
                if quote["side"]=="BUY":
                    from decimal import Decimal,localcontext
                    pending=assert_ethereum_funds_locked(cur,did=did,asset=asset,stable=stable,
                        input_atomic=int(quote["input_atomic"]),balance_atomic=balance,stable_usd=stable_usd)
                    with localcontext() as ctx:
                        ctx.prec=100
                        if (Decimal(position_usd)+Decimal(gas_usd)+Decimal(pending["pending_coin_usd"])
                                +Decimal(quote["input_atomic"]).scaleb(-6)*Decimal(stable_usd)>Decimal(extra["buy_budget_usd"])):
                            raise HTTPException(409,"Original coin budget including approval gas already allocated")
                cur.execute("""INSERT INTO mininex_live_evm_router_approval_sends
                    (privy_did,asset,side,stable,chain_id,wallet_id,wallet_address,token_address,
                    router_address,amount_atomic,policy_sha256,budget_revision,idempotency_key,
                    request_digest,status,approval_mode)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SENDING',%s)
                    ON CONFLICT DO NOTHING RETURNING status""",
                    (did,asset,quote["side"],stable,chain,current["wallet_id"],current["wallet_address"],
                     quote["source_token"],DIAMOND,int(quote["input_atomic"]),current["policy_sha256"],
                     current["budget_revision"],key,digest,approval_mode))
                if cur.fetchone() is None:
                    raise HTTPException(409,"Original bridge allowance already claimed")
                return
            claim_authorized_submission_locked(cur,did=did,quote_id=str(quote["quote_id"]),
                intent_key=key,request_sha256=digest,now=int(time.time()),
                balance_atomic=balance,stable_usd=stable_usd,expected_quote=quote,**extra)


def _finish(did,quote_id,digest,tx_hash):
    with psycopg.connect(_db_url(),connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return record_claimed_submission_locked(cur,did=did,quote_id=quote_id,
                request_sha256=digest,source_tx_hash=tx_hash)


async def submit_direct_bridge_once(*,did,quote,record,plan,session_started_at,signal_time,client,
                                    funding=None,position_usd=None,gas_usd=None,stable_usd=None):
    """The remote audit is obtained by the internal candidate caller BEFORE
    the quote. The atomic claim pins the exact audited wallet and policy.
    A transport timeout remains unresolved; it never causes a second send.
    """
    did=_safe_did(did)
    url,body=check_direct_bridge_scope(quote,record,now=int(time.time()))
    if (plan.get("manual_approval") or plan.get("url")!=url or plan.get("body")!=body
            or plan.get("route_sha256")!=quote["route_sha256"]
            or plan.get("quote_id")!=str(quote["quote_id"]) or plan.get("gas_estimate_kind")!="SWAP"):
        raise HTTPException(409,"Exact source allowance and bridge preflight required")
    key=_identity(did,quote["asset"],session_started_at,signal_time)
    digest=hashlib.sha256(json.dumps({"url":url,"body":body,"key":key},
        sort_keys=True,separators=(",",":")).encode()).hexdigest()
    # No signer/transport access until the claim transaction has committed.
    await asyncio.to_thread(_claim,did,quote,record,session_started_at,key,digest,plan,
                            funding,position_usd,gas_usd,stable_usd)
    try:
        check_direct_bridge_scope(quote,record,now=int(time.time()))
        signed=sign_privy_rpc_payload(url=url,body=body,idempotency_key=key)
        tx_hash=await post_claimed_privy_evm_rpc(url=url,body=body,
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signed,idempotency_key=key,client=client)
    except BaseException:
        await asyncio.to_thread(_finish,did,str(quote["quote_id"]),digest,None)
        raise
    await asyncio.to_thread(_finish,did,str(quote["quote_id"]),digest,tx_hash)
    return {"asset":quote["asset"],"side":quote["side"],"state":"SUBMITTED",
            "quote_id":str(quote["quote_id"]),"tx_hash":tx_hash,"real_trade_sent":True,
            "settlement_verified":False,"can_submit":False}


async def submit_direct_bridge_approval_once(*,did,quote,record,plan,session_started_at,signal_time,client,
                                    funding=None,position_usd=None,gas_usd=None,stable_usd=None):
    """One approval only; the next tick must see actual finalized allowance."""
    did=_safe_did(did)
    url,body,mode=check_direct_bridge_approval(quote,record,plan,now=int(time.time()))
    event=_identity(did,quote["asset"],session_started_at,signal_time)
    key="bridge_appr_"+hashlib.sha256((event+":"+mode).encode()).hexdigest()
    digest=hashlib.sha256(json.dumps({"url":url,"body":body,"key":key},
        sort_keys=True,separators=(",",":")).encode()).hexdigest()
    await asyncio.to_thread(_claim,did,quote,record,session_started_at,key,digest,plan,
                            funding,position_usd,gas_usd,stable_usd,mode)
    try:
        check_direct_bridge_approval(quote,record,plan,now=int(time.time()))
        signed=sign_privy_rpc_payload(url=url,body=body,idempotency_key=key)
        tx_hash=await post_claimed_privy_evm_rpc(url=url,body=body,
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signed,idempotency_key=key,client=client)
    except BaseException:
        await asyncio.to_thread(finish_approval,did=did,asset=quote["asset"],key=key,digest=digest,tx_hash=None)
        raise
    await asyncio.to_thread(finish_approval,did=did,asset=quote["asset"],key=key,digest=digest,tx_hash=tx_hash)
    return {"asset":quote["asset"],"side":quote["side"],"state":"BRIDGE_APPROVAL_SUBMITTED",
            "quote_id":str(quote["quote_id"]),"approval_tx_hash":tx_hash,"real_trade_sent":False,
            "settlement_verified":False,"can_submit":False}
