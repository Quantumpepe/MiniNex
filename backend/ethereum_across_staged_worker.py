"""Internal worker for two-leg Ethereum-settled BNB/POL/ARB orders.
Only an independently re-read original owner scope and committed step
claim can reach the existing Privy sender. Receipt polling never signs.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from decimal import Decimal,localcontext
from fastapi import HTTPException
from .ethereum_across_staged_plan import STAGED_ACROSS_POLICY,STAGED_ACROSS_ASSETS,staged_route
from .ethereum_across_staged_execution import (
    prepare_staged_across_bridge,prepare_staged_across_local_swap,
    reconcile_staged_across_bridge,reconcile_staged_across_local_swap,find_staged_across_destination,
)
from .ethereum_across_staged_journal import (
    stage_kind,exact_stage_amount,create_order,read_open_order,read_pending_step,
    claim_step,finish_submission,pending_reconcile_steps,record_step_proof,
    cancel_unspent_buy_for_exit,
)
from .ethereum_stargate_staged_worker import _approval_proof
from .live_eth_quotes import _quantity
from .shared_evm_central_runtime import audit_original_evm_coin,original_evm_coin_record
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .eth_privy_p256_signer import sign_privy_rpc_payload

LOG=logging.getLogger("mininex.across_staged")


async def _audited(did,asset,stable,revision):
    report=await audit_original_evm_coin(did,asset)
    record=await asyncio.to_thread(original_evm_coin_record,did,asset)
    if (record["asset"]!=asset or record["stable"]!=stable or record["budget_revision"]!=revision
            or record["scope"].get("settlement_policy")!=STAGED_ACROSS_POLICY
            or report["remote_verified_policy_sha256"]!=record["policy_sha256"]
            or report["remote_verified_wallet_id"]!=record["wallet_id"]
            or report["remote_verified_signer_id"]!=record["signer_id"]
            or report["current_user_budget_revision"]!=revision
            or any(report.get(k) is not True for k in (
                "owner_wallet_current","exact_remote_policy_rules_verified",
                "privy_additional_signer_scoped","backend_quorum_current",
                "signed_coin_budget_intent_in_ledger"))):
        raise HTTPException(409,"Original staged Across owner grant unavailable")
    return record


async def _prepare(client,order,record):
    args=dict(asset=order["asset"],side=order["side"],stable=order["stable"],
        wallet=record["wallet_address"],amount_atomic=exact_stage_amount(order))
    if stage_kind(order["side"],order["stage"])=="BRIDGE":
        return await prepare_staged_across_bridge(client,**args,wallet_id=record["wallet_id"])
    return await prepare_staged_across_local_swap(client,**args)


async def _submit(did,order,record,prepared,client):
    stable_usd=None
    if order["side"]=="BUY" and order["stage"]==1:
        from .shared_evm_policy_reference_rates import fetch_evm_policy_reference_rates,_price
        prices=await fetch_evm_policy_reference_rates(order["asset"],order["stable"])
        stable_usd=_price(prices,order["stable"],int(time.time()))
    claim=await asyncio.to_thread(claim_step,did,order["order_id"],record,prepared,
                                  expected_updated_at=order["updated_at"],stable_usd=stable_usd)
    if claim is None:
        return {"asset":order["asset"],"state":"CANCELLED","reason":"OWNER_REQUESTED_EXIT"}
    try:
        signature=sign_privy_rpc_payload(url=claim["url"],body=claim["body"],idempotency_key=claim["key"])
        tx_hash=await post_claimed_privy_evm_rpc(url=claim["url"],body=claim["body"],
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signature,idempotency_key=claim["key"],client=client)
    except BaseException:
        await asyncio.to_thread(finish_submission,claim,None)
        raise
    await asyncio.to_thread(finish_submission,claim,tx_hash)
    return {"asset":order["asset"],"state":"SUBMITTED","order_id":str(order["order_id"]),
            "side":order["side"],"stage":order["stage"],"kind":claim["kind"],
            "tx_hash":tx_hash,"real_trade_sent":claim["kind"]!="APPROVAL",
            "settlement_verified":False,"can_submit":False}


async def advance_across_order(did,*,row,client):
    asset=row["asset"]
    order=await asyncio.to_thread(read_open_order,did,asset)
    if order is None:
        return None
    if row.get("session_status") not in ("ACTIVE","EXIT_PENDING"):
        return {"asset":asset,"state":"WAIT","reason":"ORIGINAL_SESSION_PAUSED"}
    if await asyncio.to_thread(read_pending_step,order["order_id"]):
        return {"asset":asset,"state":"WAIT","reason":"ORIGINAL_STAGED_STEP_PENDING"}
    if row.get("session_status")=="EXIT_PENDING" and order["side"]=="BUY" and order["stage"]==1:
        if await asyncio.to_thread(cancel_unspent_buy_for_exit,did,order["order_id"]):
            return {"asset":asset,"state":"CANCELLED","reason":"OWNER_REQUESTED_EXIT"}
    record=await _audited(did,asset,order["stable"],order["budget_revision"])
    if (row["stable"]!=order["stable"] or row["budget_revision"]!=order["budget_revision"]
            or row["started_by_owner_at"]!=order["started_by_owner_at"]
            or record["scope"]!=order["scope_json"]):
        raise HTTPException(409,"Original staged order settings changed")
    return await _submit(did,order,record,await _prepare(client,order,record),client)


async def start_across_staged_order(*,did,asset,side,stable,budget_revision,amount,client,
        budget_usd,session_started_at,signal_time):
    from .ethereum_settlement_auto_plan import read_cross_evm_funding
    from .ethereum_settlement_funding import read_pending_ethereum_funding
    from .ethereum_settlement_plan import ethereum_buy_amount
    from .shared_evm_policy_reference_rates import fetch_evm_policy_reference_rates,_price
    if asset not in STAGED_ACROSS_ASSETS:
        raise ValueError("Unsupported staged coin")
    record=await _audited(did,asset,stable,budget_revision)
    _,units=_quantity(amount,6 if side=="BUY" else 18)
    cap=int(record["scope"]["buy_source_atomic_cap" if side=="BUY" else "sell_source_atomic_cap"])
    selected=min(int(units),cap)
    if selected<=0:
        return {"asset":asset,"state":"WAIT","reason":"NO_VERIFIED_STRATEGY_FUNDS"}
    order={"asset":asset,"side":side,"stable":stable,"stage":1,"original_input_atomic":selected,
           "remaining_atomic":0,"scope_json":record["scope"]}
    prepared=await _prepare(client,order,record)
    position=rate=cost=None
    if side=="BUY":
        evidence=await read_cross_evm_funding(did,asset,stable,client)
        if evidence["wallet_id"]!=record["wallet_id"] or evidence["wallet"]!=record["wallet_address"]:
            raise HTTPException(409,"Original staged funding wallet changed")
        prices=await fetch_evm_policy_reference_rates(asset,stable)
        now=int(time.time())
        rate=_price(prices,stable,now)
        native_price=_price(prices,"ETH",now)
        position=Decimal(evidence["coin_atomic"]).scaleb(-18)*_price(prices,asset,now)
        pending=await asyncio.to_thread(read_pending_ethereum_funding,did,asset=asset,stable=stable,stable_usd=rate)
        for attempt in range(2):
            preflight=prepared["preflight"]
            with localcontext() as ctx:
                ctx.prec=100
                cost=Decimal(int(preflight["estimated_gas_atomic"])+int(preflight["native_bridge_fee_atomic"])).scaleb(-18)*native_price
                available=ethereum_buy_amount(budget_usd=budget_usd,position_usd=position,
                    pending_coin_usd=pending["pending_coin_usd"],
                    wallet_stable_atomic=min(int(evidence["stable_atomic"]),int(preflight["verified_source_balance_atomic"])),
                    pending_wallet_stable_atomic=pending["pending_wallet_stable_atomic"],stable_usd=rate,estimated_cost_usd=cost)
            resized=min(selected,int(available["input_atomic"]))
            if resized==selected:
                break
            if resized<=0 or attempt==1:
                return {"asset":asset,"state":"WAIT","reason":"COIN_BUDGET_INCLUDES_BRIDGE_COST"}
            selected=resized
            order["original_input_atomic"]=selected
            prepared=await _prepare(client,order,record)
    saved=await asyncio.to_thread(create_order,did,record,side=side,prepared=prepared,
        budget_usd=budget_usd,position_usd=position,stable_usd=rate,cost_usd=cost,
        session_started_at=session_started_at,signal_time=signal_time)
    return await _submit(did,saved,record,prepared,client)


async def reconcile_one_across_step(client,step):
    prepared=step["prepared"];tx_hash=step["tx_hash"];cursor=None
    if step["kind"]=="APPROVAL":
        proof=await _approval_proof(client,step)
    elif step["kind"]=="SWAP":
        proof=await reconcile_staged_across_local_swap(client,prepared=prepared,tx_hash=tx_hash)
    elif step["kind"]=="BRIDGE":
        terms=prepared["terms"]
        proof=await reconcile_staged_across_bridge(client,terms=terms,source_hash=tx_hash)
        if proof["state"]=="DESTINATION_PENDING":
            await asyncio.to_thread(record_step_proof,step,proof)
            scan=await find_staged_across_destination(client,prepared=prepared,
                deposit_id=proof["deposit_id"],start_block=step["scan_block"])
            cursor=scan["next_block"]
            destination=scan["destination_hash"]
            if destination:
                proof=await reconcile_staged_across_bridge(client,terms=terms,source_hash=tx_hash,destination_hash=destination)
    else:
        raise ValueError("Unknown original staged step")
    return await asyncio.to_thread(record_step_proof,step,proof,cursor)


async def reconcile_across_staged_steps_once(client):
    steps=await asyncio.to_thread(pending_reconcile_steps)
    for step in steps:
        try:
            await reconcile_one_across_step(client,step)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Original Across step needs receipt reconciliation")
