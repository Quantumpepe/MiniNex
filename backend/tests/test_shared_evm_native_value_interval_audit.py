"""Full original native value interval must disclose any foreign wallet movement."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import httpx
import pytest
from fastapi import HTTPException

from backend import shared_evm_native_value_interval_audit as audit
from backend.shared_evm_swap_receipts import _pair

OWNER="0x"+"ab"*20
OTHER="0x"+"de"*20
BLOCK_HASH="0x"+"cd"*32
BUY_SWAP="0x"+"11"*32
BUY_CONV="0x"+"12"*32
SELL_SWAP="0x"+"22"*32
SELL_CONV="0x"+"23"*32
FOREIGN="0x"+"44"*32
UNIT=10**18


def data(asset="POL"):
    stable="USDT" if asset=="BNB" else "USDC"
    chain=_pair(asset,stable,"SELL")["chain_id"]
    wrapped=_pair(asset,stable,"SELL")["source_token"].lower()
    buysell=[
        ("BUY",BUY_SWAP,BUY_CONV,110,111,"2"),
        ("SELL",SELL_SWAP,SELL_CONV,190,189,"1"),
    ]
    fills=[]
    for side,swap,tx,block,conversion_block,amount in buysell:
        proof=dict(
            mode="INDEPENDENT_ORIGINAL_NATIVE_CONVERSION_TRACE_ONLY",
            asset=asset,side=side,chain_id=chain,wallet_address=OWNER,
            tx_hash=tx,swap_tx_hash=swap,
            original_finalized_internal_value_proven=True,
            full_historical_wallet_activity_proven=False,
        )
        cost=dict(
            mode="INDEPENDENT_FINALIZED_NATIVE_EVM_CONVERSION_COST",
            asset=asset,side=side,chain_id=chain,wallet_address=OWNER,
            tx_hash=tx,swap_tx_hash=swap,
            intent_key="sig_original_"+side.lower()+"12345",
            quantity_native=amount,block_number=conversion_block,
            finalized=True,
        )
        fills.append(dict(
            asset=asset,side=side,stable=stable,chain_id=chain,
            wallet_address=OWNER,tx_hash=swap,intent_key=cost["intent_key"],
            block_number=block,onchain_finalized=True,
            native_cycle_complete=True,native_conversion=cost,
            native_conversion_trace=proof,
        ))
    def trace(tx,block,path,frm,to,amount):
        return {
            "transactionHash":tx,"blockNumber":block,
            "blockHash":BLOCK_HASH,"traceAddress":path,
            "type":"call","action":{
                "from":frm,"to":to,"value":hex(amount),
                "callType":"call",
            },
        }
    traces=[
        trace(BUY_CONV,111,[],OWNER,wrapped,0),
        trace(BUY_CONV,111,[0],wrapped,OWNER,2*UNIT),
        trace(SELL_CONV,189,[],OWNER,wrapped,UNIT),
    ]
    return fills,traces


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_real_native_value_scans_enforce_both_original_conversions(asset):
    fills,traces=data(asset)
    result=audit.verify_original_native_value_interval(
        asset=asset,wallet=OWNER,fills=fills,traces=traces,
        canonical_blocks={111:BLOCK_HASH,189:BLOCK_HASH},
        start_block=110,end_block=190,
    )
    assert result["verified_native_conversion_count"]==2
    assert result["unexpected_native_value_movements"]==[]
    assert result["external_native_value_free_interval"] is True
    assert result["complete_wallet_lifetime_and_cost_basis_proven"] is False
    assert result["all_gas_spending_transactions_reconciled"] is False
    assert result["eligible_to_assess_five_percent_fee"] is False
    assert result["fee_transferred"] is False


def test_unknown_native_transfer_during_trading_is_visible_not_hidden():
    fills,traces=data()
    extra=deepcopy(traces[1])
    extra.update(transactionHash=FOREIGN,blockNumber=150,traceAddress=[])
    extra["action"]={"from":OTHER,"to":OWNER,"value":hex(5*UNIT),
                     "callType":"call"}
    traces.append(extra)
    report=audit.verify_original_native_value_interval(
        asset="POL",wallet=OWNER,fills=fills,traces=traces,
        canonical_blocks={111:BLOCK_HASH,150:BLOCK_HASH,189:BLOCK_HASH},
        start_block=110,end_block=190,
    )
    assert report["external_native_value_free_interval"] is False
    assert report["unexpected_native_value_movements"]==[
        {"tx_hash":FOREIGN,"block_number":150,
         "direction":"IN","atomic_amount":str(5*UNIT)},
    ]
    assert report["eligible_to_assess_five_percent_fee"] is False


@pytest.mark.parametrize("mutate",[
    lambda fills,traces:traces.pop(1),
    lambda fills,traces:traces[1]["action"].update(
        value=hex(UNIT)),
    lambda fills,traces:traces[1]["action"].update(
        from=OTHER),
    lambda fills,traces:traces[2].update(transactionHash=FOREIGN),
    lambda fills,traces:traces[2].update(blockHash=FOREIGN),
    lambda fills,traces:traces.append(deepcopy(traces[2])),
    lambda fills,traces:fills[0]["native_conversion_trace"].update(
        full_historical_wallet_activity_proven=True),
    lambda fills,traces:fills[0]["native_conversion_trace"].update(
        original_finalized_internal_value_proven=False),
    lambda fills,traces:fills[0]["native_conversion"].update(
        wallet_address=OTHER),
    lambda fills,traces:fills[0]["native_conversion"].update(
        finalized=False),
    lambda fills,traces:fills[0].update(native_cycle_complete=False),
])
def test_missing_or_tampered_native_transfer_cannot_be_marked_complete(mutate):
    fills,traces=data()
    mutate(fills,traces)
    with pytest.raises((ValueError,TypeError,KeyError)):
        audit.verify_original_native_value_interval(
            asset="POL",wallet=OWNER,fills=fills,traces=traces,
            canonical_blocks={111:BLOCK_HASH,189:BLOCK_HASH},
            start_block=110,end_block=190,
        )


def test_owner_native_value_trace_collector_paginates_and_reads_finalized_blocks():
    fills,traces=data("BNB")
    rpc=[]
    chain=_pair("BNB","USDT","BUY")["chain_id"]
    async def provider(request):
        body=json.loads(request.content)
        method=body["method"]
        args=body["params"]
        rpc.append((method,args))
        if method=="eth_chainId":
            result=hex(chain)
        elif method=="eth_getBlockByNumber":
            if args[0]=="finalized":
                result={"number":"0xc9"}
            else:
                result={"number":args[0],"hash":BLOCK_HASH}
        elif method=="trace_filter":
            key=args[0]
            result=(
                [traces[0],traces[2]]
                if "fromAddress" in key else [traces[1]]
            )
        else:raise AssertionError(method)
        return httpx.Response(200,json={
            "jsonrpc":"2.0","id":body["id"],"result":result,
        })
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)) as client:
            return await audit.audit_original_native_value_interval(
                asset="BNB",wallet=OWNER,fills=fills,client=client)
    result=asyncio.run(run())
    assert result["verified_native_conversion_count"]==2
    assert len([m for m,_ in rpc if m=="trace_filter"])==2
    assert len([m for m,_ in rpc if m=="eth_getBlockByNumber"])==3
    assert all(a[0]["fromBlock"]=="0x6e" for m,a in rpc if m=="trace_filter")


def test_trace_filter_rpc_unavailable_fails_closed_without_hwm():
    fills,traces=data("AVAX")
    chain=_pair("AVAX","USDC","BUY")["chain_id"]
    async def provider(request):
        body=json.loads(request.content)
        method=body["method"]
        if method=="trace_filter":
            return httpx.Response(200,json={
                "jsonrpc":"2.0","id":body["id"],
                "error":{"code":-32601,"message":"Trace API not supported"},
            })
        result=hex(chain) if method=="eth_chainId" else {"number":"0xc9"}
        return httpx.Response(200,json={
            "jsonrpc":"2.0","id":body["id"],"result":result,
        })
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)) as client:
            return await audit.audit_original_native_value_interval(
                asset="AVAX",wallet=OWNER,fills=fills,client=client)
    with pytest.raises(HTTPException) as e:
        asyncio.run(run())
    assert e.value.status_code==502
