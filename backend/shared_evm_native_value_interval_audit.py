"""Read-only archived native-wallet value-transfer audit between EVM trades.

Native BNB/POL/AVAX moves are not ERC20 Transfer events. They require a
complete remote trace_filter scan on the original network, with pagination,
canonical finalized block hashes and no unexplained incoming/outgoing value.
This audit is *strictly limited* to the observed MiniNex trading interval:
it cannot establish the owner's pre-existing native balance/cost basis or
prove every gas-consuming EOA transaction. It NEVER enables HWM/auto-fees.
RPCs without archive trace_filter fail closed; no swaps are blocked by this
optional accounting reader.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import os
import re

import httpx
from fastapi import HTTPException

from .multicoin_evm_balances import _RPC
from .shared_evm_swap_receipts import _pair, _receipt_rpc

_ASSETS=frozenset(("BNB","POL","AVAX"))
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ADDR=re.compile(r"^0x[0-9a-f]{40}$")
_HEX=re.compile(r"^0x(?:0|[1-9a-f][0-9a-f]*)$")
_BLOCKS_PER_RPC=128
_TRACES_PER_RPC=75
_ATOMIC=Decimal(10)**18


def _hexint(raw:object)->int:
    if type(raw) is not str or len(raw)>66 or _HEX.fullmatch(raw.lower()) is None:
        raise ValueError("Noncanonical historical native quantity")
    return int(raw,16)


def _amount_wei(raw:object)->int:
    if type(raw) is not str or not raw or len(raw)>70 or "e" in raw.lower():
        raise ValueError("Original native trade quantity is not exact")
    try:atomic=Decimal(raw)*_ATOMIC
    except (InvalidOperation,ArithmeticError):
        raise ValueError("Invalid original native units") from None
    if (not atomic.is_finite() or atomic<=0
            or atomic>=Decimal(10)**45
            or atomic!=atomic.to_integral_value()):
        raise ValueError("Original native trade units cannot be proven")
    return int(atomic)


def verify_original_native_value_interval(
    *,asset:str,wallet:str,fills:object,traces:object,
    canonical_blocks:object,start_block:int,end_block:int,
)->dict:
    """Match complete observed native-value movements to known conversions.

    The input traces must be supplied by the reader below, NOT from a
    frontend. Because an earlier deposit/balance may still exist, this
    result never marks complete FIFO or performance-fee provenance.
    """
    if (asset not in _ASSETS or type(wallet) is not str
            or _ADDR.fullmatch(wallet) is None
            or type(start_block) is not int or type(end_block) is not int
            or not 1<start_block<=end_block
            or type(fills) is not list or not fills
            or type(traces) is not list
            or type(canonical_blocks) is not dict):
        raise ValueError("Missing original wallet value audit source")
    expected={}
    period_fills=set()
    for original in fills:
        if (type(original) is not dict
                or original.get("asset")!=asset
                or original.get("wallet_address")!=wallet
                or original.get("onchain_finalized") is not True
                or original.get("native_cycle_complete") is not True
                or type(original.get("block_number")) is not int
                or not start_block<=original["block_number"]<=end_block):
            raise ValueError("Unverified source native owner trade")
        cost=original.get("native_conversion")
        proof=original.get("native_conversion_trace")
        side=original.get("side")
        stable=original.get("stable")
        pair=_pair(asset,stable,side)
        if (original.get("chain_id")!=pair["chain_id"]
                or type(cost) is not dict or type(proof) is not dict
                or cost.get("mode")!="INDEPENDENT_FINALIZED_NATIVE_EVM_CONVERSION_COST"
                or cost.get("asset")!=asset or cost.get("side")!=side
                or cost.get("wallet_address")!=wallet
                or cost.get("swap_tx_hash")!=original.get("tx_hash")
                or cost.get("intent_key")!=original.get("intent_key")
                or cost.get("finalized") is not True
                or type(cost.get("block_number")) is not int
                or not start_block<=cost["block_number"]<=end_block
                or proof.get("mode")!="INDEPENDENT_ORIGINAL_NATIVE_CONVERSION_TRACE_ONLY"
                or proof.get("original_finalized_internal_value_proven") is not True
                or proof.get("full_historical_wallet_activity_proven") is not False
                or proof.get("wallet_address")!=wallet
                or proof.get("tx_hash")!=cost.get("tx_hash")
                or proof.get("swap_tx_hash")!=original.get("tx_hash")
                or proof.get("asset")!=asset or proof.get("side")!=side):
            raise ValueError("Source native custody conversion is not trace verified")
        tx=cost.get("tx_hash")
        if type(tx) is not str or _HASH.fullmatch(tx) is None:
            raise ValueError("Missing original conversion hash")
        if tx in period_fills:
            raise ValueError("Native wallet conversion reused by two fills")
        period_fills.add(tx)
        wrapped=_pair(asset,stable,"SELL")["source_token"].lower()
        quantity=_amount_wei(cost["quantity_native"])
        direction="IN" if side=="BUY" else "OUT"
        key=(tx,direction,quantity)
        if key in expected:raise ValueError("Duplicate original native conversion")
        expected[key]=(wrapped,cost["block_number"])
    found=set()
    seen=set()
    unexpected=[]
    for entry in traces:
        if (type(entry) is not dict or entry.get("type")!="call"
                or type(entry.get("action")) is not dict
                or type(entry.get("traceAddress")) is not list
                or type(entry.get("transactionHash")) is not str
                or _HASH.fullmatch(entry["transactionHash"].lower()) is None
                or type(entry.get("blockHash")) is not str
                or _HASH.fullmatch(entry["blockHash"].lower()) is None
                or type(entry.get("blockNumber")) is not int
                or not start_block<=entry["blockNumber"]<=end_block):
            raise ValueError("Untrusted or incomplete historical native trace page")
        action=entry["action"]
        sender,receiver=action.get("from"),action.get("to")
        if (type(sender) is not str or type(receiver) is not str
                or _ADDR.fullmatch(sender.lower()) is None
                or _ADDR.fullmatch(receiver.lower()) is None
                or type(entry.get("traceAddress")) is not list
                or any(type(p) is not int or p<0 for p in entry["traceAddress"])):
            raise ValueError("Malformed original wallet value trace")
        value=_hexint(action.get("value"))
        # Failed child calls do not transfer native coin. They cannot prove
        # a conversion, but they are not foreign wallet funds either.
        if entry.get("error") is not None:
            continue
        if (value and action.get("callType")!="call"):
            raise ValueError("Unclassified native-value call semantics")
        sender,receiver=sender.lower(),receiver.lower()
        if not value or (sender!=wallet and receiver!=wallet):
            continue
        block=entry["blockNumber"]
        tx=entry["transactionHash"].lower()
        position=(tx,tuple(entry["traceAddress"]))
        if position in seen:
            raise ValueError("Trace pagination repeated the same wallet value movement")
        seen.add(position)
        if canonical_blocks.get(block)!=entry["blockHash"].lower():
            raise ValueError("Native value trace does not belong to canonical block")
        direction="IN" if receiver==wallet else "OUT"
        other=sender if direction=="IN" else receiver
        match_key=(tx,direction,value)
        previous=expected.get(match_key)
        if (previous==(other,block) and match_key not in found):
            found.add(match_key)
        else:
            unexpected.append({
                "tx_hash":tx,"block_number":block,
                "direction":direction,"atomic_amount":str(value),
            })
    if found!=set(expected):
        raise ValueError("A verified native conversion is missing from archive trace")
    return {
        "mode":"READ_ONLY_NATIVE_VALUE_INTERVAL_AUDIT",
        "asset":asset,"wallet_address":wallet,
        "start_block":start_block,"end_block":end_block,
        "original_conversion_count":len(expected),
        "verified_native_conversion_count":len(found),
        "unexpected_native_value_movements":unexpected,
        "external_native_value_free_interval":not unexpected,
        "complete_wallet_lifetime_and_cost_basis_proven":False,
        "all_gas_spending_transactions_reconciled":False,
        "eligible_to_assess_five_percent_fee":False,
        "fee_assessed":False,"fee_transferred":False,
    }


async def _scan_trace_filter(client:httpx.AsyncClient,url:str,
                             wallet:str,start:int,end:int)->list[dict]:
    """Archive parity trace_filter with complete paged in/out coverage."""
    word=wallet.lower()
    events=[]
    reqid=400
    for low in range(start,end+1,_BLOCKS_PER_RPC):
        high=min(end,low+_BLOCKS_PER_RPC-1)
        for address_key in ("fromAddress","toAddress"):
            offset=0
            page_keys=set()
            while True:
                args={
                    "fromBlock":hex(low),"toBlock":hex(high),
                    address_key:[word],"after":offset,
                    "count":_TRACES_PER_RPC,
                }
                page=await _receipt_rpc(
                    client,url,"trace_filter",[args],reqid)
                reqid+=1
                if type(page) is not list:
                    raise ValueError("Incomplete historical value trace filter")
                if len(page)>_TRACES_PER_RPC:
                    raise ValueError("Archive RPC exceeded requested value trace page")
                for trace in page:
                    if type(trace) is not dict or type(trace.get("traceAddress")) is not list:
                        raise ValueError("Untrusted native trace page")
                    key=(trace.get("transactionHash"),tuple(trace["traceAddress"]))
                    if key in page_keys:
                        raise ValueError("Archive RPC ignored pagination offset")
                    page_keys.add(key)
                events.extend(page)
                if len(page)<_TRACES_PER_RPC:break
                offset+=len(page)
    # The same trace might appear in both FROM and TO (owner self-calls).
    unique={}
    for event in events:
        if type(event) is not dict:
            raise ValueError("Malformed archive trace response")
        hash_=event.get("transactionHash")
        path=event.get("traceAddress")
        if (type(hash_) is not str or _HASH.fullmatch(hash_.lower()) is None
                or type(path) is not list or
                any(type(p) is not int or p<0 for p in path)):
            raise ValueError("Archive trace lacks unique transaction position")
        key=(hash_.lower(),tuple(path))
        if key in unique:
            if unique[key]!=event:
                raise ValueError("Archive FROM/TO pages disagree on same trace")
        else:
            unique[key]=event
    return list(unique.values())


async def audit_original_native_value_interval(
    *,asset:str,wallet:str,fills:list[dict],client:httpx.AsyncClient,
)->dict:
    if (asset not in _ASSETS or type(wallet) is not str
            or _ADDR.fullmatch(wallet) is None
            or type(fills) is not list or not fills
            or type(client) is not httpx.AsyncClient):
        raise ValueError("Original owner native wallet audit requires server evidence")
    heights=[]
    for item in fills:
        if type(item) is not dict or type(item.get("native_conversion")) is not dict:
            raise ValueError("Source owner trade has no native custody conversion")
        heights.extend((item.get("block_number"),
                        item["native_conversion"].get("block_number")))
    if any(type(h) is not int or h<=1 for h in heights):
        raise ValueError("Original native trade time window is unknown")
    start,end=min(heights),max(heights)
    chain,network,fallback,env=_RPC[asset]
    if chain!=_pair(asset,"USDT" if asset=="BNB" else "USDC","SELL")["chain_id"]:
        raise ValueError("Native wallet audit RPC chain not consistent")
    url=os.getenv(env,"").strip() or fallback
    if not url.startswith("https://") or any(c in url for c in " \t\n\r#@"):
        raise HTTPException(503,"Archive native value trace RPC not configured")
    try:
        rpc_chain=await _receipt_rpc(client,url,"eth_chainId",[],300)
        if _hexint(rpc_chain)!=chain:
            raise ValueError("Archive wallet history RPC wrong chain")
        finalized=await _receipt_rpc(
            client,url,"eth_getBlockByNumber",["finalized",False],301)
        if type(finalized) is not dict or _hexint(finalized["number"])<end:
            raise ValueError("Native wallet history not yet finalized")
        events=await _scan_trace_filter(client,url,wallet,start,end)
        canonical={}
        for i,height in enumerate(sorted({
                event.get("blockNumber") for event in events
                if type(event) is dict and type(event.get("blockNumber")) is int
        })):
            block=await _receipt_rpc(
                client,url,"eth_getBlockByNumber",[hex(height),False],500+i)
            if type(block) is not dict or _hexint(block["number"])!=height:
                raise ValueError("Missing canonical native event block")
            digest=block.get("hash")
            if type(digest) is not str or _HASH.fullmatch(digest.lower()) is None:
                raise ValueError("Invalid canonical trace block hash")
            canonical[height]=digest.lower()
        return verify_original_native_value_interval(
            asset=asset,wallet=wallet,fills=fills,traces=events,
            canonical_blocks=canonical,start_block=start,end_block=end)
    except (httpx.HTTPError,ValueError,TypeError,KeyError,OverflowError):
        raise HTTPException(
            502,"Full original native wallet value trace interval unavailable") from None
