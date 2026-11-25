"""AVAX staged worker: the original grant authorizes only these exact legs.
No public send endpoint. The passive reconciler never signs, even after a
pause, revocation or service restart. Only a committed once-only step claim
can reach the shared Privy transport.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from decimal import Decimal,localcontext
import httpx
from fastapi import HTTPException
from .ethereum_stargate_staged_policy import STARGATE_STAGED_POLICY
from .ethereum_stargate_staged_execution import (
    prepare_direct_taxi,prepare_staged_swap,reconcile_direct_taxi,
    reconcile_staged_swap,find_direct_taxi_destination,
)
from .ethereum_stargate_staged_journal import (
    stage_kind,exact_stage_amount,create_order,read_open_order,read_pending_step,
    claim_step,finish_submission,pending_reconcile_steps,record_step_proof,
    cancel_unspent_buy_for_exit,
)
from .ethereum_settlement_status import _finalized_receipt,_eq
from .live_eth_receipts import _num
from .live_eth_quotes import _quantity
from .shared_evm_central_runtime import audit_original_evm_coin,original_evm_coin_record
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .eth_privy_p256_signer import sign_privy_rpc_payload

LOG=logging.getLogger("mininex.avax_staged")


async def _audited(did,stable,revision):
    report=await audit_original_evm_coin(did,"AVAX")
    record=await asyncio.to_thread(original_evm_coin_record,did,"AVAX")
    if (record["stable"]!=stable or record["budget_revision"]!=revision
            or record["scope"].get("settlement_policy")!=STARGATE_STAGED_POLICY
            or report["remote_verified_policy_sha256"]!=record["policy_sha256"]
            or report["remote_verified_wallet_id"]!=record["wallet_id"]
            or report["remote_verified_signer_id"]!=record["signer_id"]
            or report["current_user_budget_revision"]!=revision
            or any(report.get(k) is not True for k in (
              "owner_wallet_current","exact_remote_policy_rules_verified",
              "privy_additional_signer_scoped","backend_quorum_current",
              "signed_coin_budget_intent_in_ledger"))):
        raise HTTPException(409,"Original AVAX owner grant unavailable")
    return record


async def _prepare(client,order,record):
    fn=prepare_direct_taxi if stage_kind(order["side"],order["stage"])=="BRIDGE" else prepare_staged_swap
    return await fn(client,side=order["side"],stable=order["stable"],
        wallet=record["wallet_address"],scope=record["scope"],amount_atomic=exact_stage_amount(order))


async def _submit(did,order,record,prepared,client):
    claim=await asyncio.to_thread(claim_step,did,order["order_id"],record,prepared)
    if claim is None:
        return {"asset":"AVAX","state":"CANCELLED","reason":"OWNER_REQUESTED_EXIT"}
    try:
        signature=sign_privy_rpc_payload(url=claim["url"],body=claim["body"],idempotency_key=claim["key"])
        tx_hash=await post_claimed_privy_evm_rpc(url=claim["url"],body=claim["body"],
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signature,idempotency_key=claim["key"],client=client)
    except BaseException:
        await asyncio.to_thread(finish_submission,claim,None)
        raise
    await asyncio.to_thread(finish_submission,claim,tx_hash)
    return {"asset":"AVAX","state":"SUBMITTED","order_id":str(order["order_id"]),
            "side":order["side"],"stage":order["stage"],"kind":claim["kind"],
            "tx_hash":tx_hash,"real_trade_sent":claim["kind"]!="APPROVAL",
            "settlement_verified":False,"can_submit":False}


async def advance_avax_order(did,*,row,client):
    """None means no existing order; otherwise never start another signal."""
    order=await asyncio.to_thread(read_open_order,did)
    if order is None:
        return None
    if await asyncio.to_thread(read_pending_step,order["order_id"]):
        return {"asset":"AVAX","state":"WAIT","reason":"ORIGINAL_STAGED_STEP_PENDING"}
    if row.get("session_status")=="EXIT_PENDING" and order["side"]=="BUY" and order["stage"]==1:
        if await asyncio.to_thread(cancel_unspent_buy_for_exit,did,order["order_id"]):
            return {"asset":"AVAX","state":"CANCELLED","reason":"OWNER_REQUESTED_EXIT"}
    record=await _audited(did,order["stable"],order["budget_revision"])
    if (row["stable"]!=order["stable"] or row["budget_revision"]!=order["budget_revision"]
            or record["scope"]!=order["scope_json"]):
        raise HTTPException(409,"Original staged order settings changed")
    prepared=await _prepare(client,order,record)
    return await _submit(did,order,record,prepared,client)


async def start_avax_staged_order(*,did,side,stable,budget_revision,amount,client,
                                  budget_usd,record=None):
    from .ethereum_settlement_auto_plan import read_cross_evm_funding
    from .ethereum_settlement_funding import read_pending_ethereum_funding
    from .ethereum_settlement_plan import ethereum_buy_amount
    from .shared_evm_policy_reference_rates import fetch_evm_policy_reference_rates,_price
    record=record or await _audited(did,stable,budget_revision)
    if record["scope"].get("settlement_policy")!=STARGATE_STAGED_POLICY:
        raise HTTPException(409,"AVAX requires original Ethereum staged grant")
    _,units=_quantity(amount,6 if side=="BUY" else 18)
    cap=int(record["scope"]["buy_source_atomic_cap" if side=="BUY" else "sell_source_atomic_cap"])
    selected=min(int(units),cap)
    if selected<=0:
        return {"asset":"AVAX","state":"WAIT","reason":"NO_VERIFIED_STRATEGY_FUNDS"}
    order={"side":side,"stable":stable,"stage":1,"original_input_atomic":selected,
            "remaining_atomic":0,"scope_json":record["scope"]}
    prepared=await _prepare(client,order,record)
    position=rate=cost=None
    if side=="BUY":
        evidence=await read_cross_evm_funding(did,"AVAX",stable,client)
        if evidence["wallet_id"]!=record["wallet_id"] or evidence["wallet"]!=record["wallet_address"]:
            raise HTTPException(409,"Original AVAX funding wallet changed")
        prices=await fetch_evm_policy_reference_rates("AVAX",stable)
        now=int(time.time())
        rate=_price(prices,stable,now)
        native_price=_price(prices,"ETH",now)
        position=Decimal(evidence["coin_atomic"]).scaleb(-18)*_price(prices,"AVAX",now)
        pending=await asyncio.to_thread(read_pending_ethereum_funding,did,
            asset="AVAX",stable=stable,stable_usd=rate)
        # Include current quoted messaging fee and source gas. Requote
        # after sizing once; later stage costs are separately journaled.
        for attempt in range(2):
            with localcontext() as ctx:
                ctx.prec=100
                cost=Decimal(int(prepared["estimated_gas_atomic"])+int(prepared["native_bridge_fee_atomic"])).scaleb(-18)*native_price
                available=ethereum_buy_amount(budget_usd=budget_usd,position_usd=position,
                    pending_coin_usd=pending["pending_coin_usd"],
                    wallet_stable_atomic=min(int(evidence["stable_atomic"]),int(prepared["source_balance_atomic"])),
                    pending_wallet_stable_atomic=pending["pending_wallet_stable_atomic"],
                    stable_usd=rate,estimated_cost_usd=cost)
            resized=min(selected,int(available["input_atomic"]))
            if resized==selected:
                break
            if resized<=0 or attempt==1:
                return {"asset":"AVAX","state":"WAIT","reason":"COIN_BUDGET_INCLUDES_BRIDGE_COST"}
            selected=resized;order["original_input_atomic"]=selected
            prepared=await _prepare(client,order,record)
    saved=await asyncio.to_thread(create_order,did,record,side=side,prepared=prepared,
        budget_usd=budget_usd,position_usd=position,stable_usd=rate,cost_usd=cost)
    return await _submit(did,saved,record,prepared,client)


async def _approval_proof(client,step):
    prepared=step["prepared"];expected=prepared["approval"]
    tx_hash=step["tx_hash"]
    result=await _finalized_receipt(client,step["chain_id"],tx_hash)
    if result is None:
        return {"state":"SOURCE_PENDING"}
    tx,receipt=result
    if (not _eq(tx.get("from"),expected["from"]) or not _eq(tx.get("to"),expected["to"])
            or not _eq(tx.get("input"),expected["data"]) or _num(tx.get("value"))!=0):
        raise ValueError("Finalized AVAX allowance differs from original")
    status=_num(receipt["status"])
    gas=_num(receipt["gasUsed"])*_num(receipt["effectiveGasPrice"])
    if status not in (0,1):
        raise ValueError("Invalid AVAX approval receipt")
    return {"state":"APPROVAL_FINALIZED" if status else "SOURCE_REVERTED",
            "source_hash":tx_hash,"source_gas_atomic":str(gas)}


async def reconcile_one_avax_step(client,step):
    prepared=step["prepared"];tx_hash=step["tx_hash"];cursor=None
    if step["kind"]=="APPROVAL":
        proof=await _approval_proof(client,step)
    elif step["kind"]=="SWAP":
        proof=await reconcile_staged_swap(client,prepared=prepared,tx_hash=tx_hash)
    elif step["kind"]=="BRIDGE":
        proof=await reconcile_direct_taxi(client,prepared=prepared,source_hash=tx_hash)
        if proof["state"]=="DESTINATION_PENDING":
            scan=await find_direct_taxi_destination(client,prepared=prepared,guid=proof["guid"],
                start_block=step["scan_block"])
            cursor=scan["next_block"]
            if scan["destination_hash"]:
                proof=await reconcile_direct_taxi(client,prepared=prepared,source_hash=tx_hash,
                    destination_hash=scan["destination_hash"])
    else:
        raise ValueError("Unknown original AVAX step")
    return await asyncio.to_thread(record_step_proof,step,proof,cursor)


async def reconcile_avax_staged_steps_once(client):
    steps=await asyncio.to_thread(pending_reconcile_steps)
    for step in steps:
        try:
            await reconcile_one_avax_step(client,step)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Original AVAX step needs receipt reconciliation")
