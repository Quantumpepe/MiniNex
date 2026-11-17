"""Original finalized native BNB/POL/AVAX value custody cannot be inferred from WETH logs."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
import json

import httpx
import pytest
from fastapi import HTTPException

from backend.shared_evm_native_trace_provenance import (
    verify_original_native_conversion_trace,
    read_original_native_conversion_trace,
)
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
SWAP="0x"+"11"*32
CONVERT="0x"+"22"*32
BLOCK_HASH="0x"+"cc"*32
UNIT=10**18
BLOCK=105


def sample(asset="BNB",side="BUY"):
    stable="USDT" if asset=="BNB" else "USDC"
    route=_pair(asset,stable,side)
    wrapped=_pair(asset,stable,"SELL")["source_token"].lower()
    fill=dict(
        asset=asset,side=side,stable=stable,chain_id=route["chain_id"],
        wallet_address=WALLET,tx_hash=SWAP,intent_key="sig_original_0123456",
        onchain_finalized=True,source_amount="2.5",destination_amount="2.5",
    )
    cost=dict(
        mode="INDEPENDENT_FINALIZED_NATIVE_EVM_CONVERSION_COST",
        asset=asset,side=side,chain_id=route["chain_id"],
        wallet_address=WALLET,tx_hash=CONVERT,swap_tx_hash=SWAP,
        intent_key=fill["intent_key"],operation="WITHDRAW" if side=="BUY" else "DEPOSIT",
        block_number=BLOCK,quantity_native="2.5",network_fee_native="0.0001",
        finalized=True,
    )
    receipt={
        "transactionHash":CONVERT,"from":WALLET,"to":wrapped,
        "blockHash":BLOCK_HASH,"blockNumber":hex(BLOCK),
        "transactionIndex":"0x2","gasUsed":"0x186a0",
        "effectiveGasPrice":hex(10**9),"status":"0x1",
    }
    def trace(position,frm,to,value):
        return {
            "type":"call","action":{
                "callType":"call","from":frm,"to":to,"value":hex(value),
                "gas":"0x1e8480","input":"0x",
            },
            "result":{"gasUsed":"0x5208","output":"0x"},
            "traceAddress":position,"subtraces":0,
            "transactionHash":CONVERT,"blockHash":BLOCK_HASH,
            "blockNumber":BLOCK,"transactionPosition":2,
        }
    if side=="BUY":
        traces=[
            trace([],WALLET,wrapped,0),
            trace([0],wrapped,WALLET,UNIT*5//2),
        ]
        traces[0]["subtraces"]=1
    else:
        traces=[trace([],WALLET,wrapped,UNIT*5//2)]
    final={"number":"0x70"}
    canonical={"number":hex(BLOCK),"hash":BLOCK_HASH}
    return dict(asset=asset,side=side,stable=stable,fill=fill,
                conversion=cost,receipt=receipt,traces=traces,
                finalized=final,canonical_block=canonical)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_native_original_conversion_traces_prove_real_user_value_and_gas(
    asset,side,
):
    evidence=sample(asset,side)
    report=verify_original_native_conversion_trace(**evidence)
    assert report["mode"]=="INDEPENDENT_ORIGINAL_NATIVE_CONVERSION_TRACE_ONLY"
    assert report["original_finalized_internal_value_proven"] is True
    assert report["unexpected_native_value_in_conversion_absent"] is True
    assert report["paid_network_gas_wei"]==str(100000*10**9)
    assert report["native_atomic_received"]==str(UNIT*5//2 if side=="BUY" else 0)
    assert report["native_atomic_spent"]==str(UNIT*5//2 if side=="SELL" else 0)
    assert report["full_historical_wallet_activity_proven"] is False
    assert report["hwm_fee_assessed"] is False


@pytest.mark.parametrize("change",[
    lambda a:a["fill"].update(wallet_address="0x"+"ee"*20),
    lambda a:a["fill"].update(tx_hash=CONVERT),
    lambda a:a["fill"].update(chain_id=1),
    lambda a:a["conversion"].update(tx_hash=SWAP),
    lambda a:a["conversion"].update(network_fee_native="0"),
    lambda a:a["conversion"].update(quantity_native="2.500000000000000001"),
    lambda a:a["conversion"].update(operation="DEPOSIT"),
    lambda a:a["conversion"].update(finalized=False),
    lambda a:a["receipt"].update(blockHash=SWAP),
    lambda a:a["receipt"].update(status="0x0"),
    lambda a:a["receipt"].update(gasUsed="0x0"),
    lambda a:a["receipt"].update(effectiveGasPrice="0x0"),
    lambda a:a["receipt"].update(transactionIndex="0x9"),
    lambda a:a["receipt"].update(to=WALLET),
    lambda a:a["canonical_block"].update(hash=SWAP),
    lambda a:a["canonical_block"].update(number="0x6a"),
    lambda a:a["finalized"].update(number="0x60"),
    lambda a:a["traces"][0]["action"].update(value=hex(100)),
    lambda a:a["traces"][0].update(transactionPosition=3),
    lambda a:a["traces"][0].update(error="Reverted"),
    lambda a:a["traces"][0].update(blockHash=SWAP),
    lambda a:a["traces"].append(deepcopy(a["traces"][0])),
    lambda a:a["traces"].append({
        **deepcopy(a["traces"][0]),"traceAddress":[1],
    }),
])
def test_buy_original_native_withdraw_is_rejected_if_any_proof_is_modified(change):
    evidence=sample()
    change(evidence)
    with pytest.raises((ValueError,KeyError,TypeError)):
        verify_original_native_conversion_trace(**evidence)


@pytest.mark.parametrize("mutation",[
    lambda a:a["traces"][1]["action"].update(
        to="0x"+"dd"*20),
    lambda a:a["traces"][1]["action"].update(
        **{"from":"0x"+"dd"*20}),
    lambda a:a["traces"][1]["action"].update(
        value=hex(2*UNIT)),
    lambda a:a["traces"][1].update(traceAddress=[]),
    lambda a:a["traces"][1].update(type="suicide"),
    lambda a:a["traces"].clear(),
])
def test_missing_or_wrong_withdrawal_cannot_be_treated_as_wallet_custody(mutation):
    e=sample("POL","BUY")
    mutation(e)
    with pytest.raises((ValueError,TypeError)):
        verify_original_native_conversion_trace(**e)


def test_unrelated_extra_native_transfer_inside_conversion_blocks_proof():
    evidence=sample("AVAX","BUY")
    another=deepcopy(evidence["traces"][1])
    another["traceAddress"]=[1]
    another["action"]["value"]=hex(UNIT)
    another["action"]["from"]=WALLET
    another["action"]["to"]="0x"+"ef"*20
    evidence["traces"].append(another)
    with pytest.raises(ValueError):
        verify_original_native_conversion_trace(**evidence)


def test_readonly_archive_rpc_uses_original_chain_receipt_and_real_trace(monkeypatch):
    from backend import shared_evm_native_trace_provenance as module
    evidence=sample("BNB","SELL")
    chain=_pair("BNB","USDT","SELL")["chain_id"]
    requested=[]
    async def provider(request):
        body=json.loads(request.content)
        requested.append((body["method"],body["params"]))
        if body["method"]=="eth_chainId":
            result=hex(chain)
        elif body["method"]=="eth_getTransactionReceipt":
            result=evidence["receipt"]
        elif body["method"]=="eth_getBlockByNumber":
            result=(evidence["finalized"] if
                body["params"][0]=="finalized" else evidence["canonical_block"])
        elif body["method"]=="trace_transaction":
            result=evidence["traces"]
        else:
            raise AssertionError("Unexpected RPC method")
        return httpx.Response(200,json={
            "jsonrpc":"2.0","id":body["id"],"result":result,
        })
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)) as client:
            return await read_original_native_conversion_trace(
                asset="BNB",side="SELL",stable="USDT",
                fill=evidence["fill"],conversion=evidence["conversion"],
                client=client,
            )
    got=asyncio.run(check())
    assert got["original_finalized_internal_value_proven"] is True
    assert [m for m,_ in requested]==[
        "eth_chainId","eth_getTransactionReceipt",
        "eth_getBlockByNumber","eth_getBlockByNumber","trace_transaction",
    ]
    assert requested[-1][1]==[CONVERT]


def test_trace_rpc_missing_is_not_silently_accepted(monkeypatch):
    evidence=sample("AVAX","BUY")
    chain=_pair("AVAX","USDC","BUY")["chain_id"]
    async def provider(request):
        body=json.loads(request.content)
        if body["method"]=="trace_transaction":
            return httpx.Response(200,json={
                "jsonrpc":"2.0","id":body["id"],
                "error":{"code":-32601,"message":"Method not found"},
            })
        if body["method"]=="eth_chainId":
            result=hex(chain)
        elif body["method"]=="eth_getTransactionReceipt":
            result=evidence["receipt"]
        elif body["method"]=="eth_getBlockByNumber":
            result=(evidence["finalized"] if
                body["params"][0]=="finalized" else evidence["canonical_block"])
        else:raise AssertionError(body["method"])
        return httpx.Response(200,json={
            "jsonrpc":"2.0","id":body["id"],"result":result,
        })
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(provider)) as client:
            return await read_original_native_conversion_trace(
                asset="AVAX",side="BUY",stable="USDC",
                fill=evidence["fill"],conversion=evidence["conversion"],
                client=client,
            )
    with pytest.raises(HTTPException) as err:
        asyncio.run(check())
    assert err.value.status_code==502


def test_truncated_native_internal_trace_tree_is_not_a_complete_wallet_proof():
    evidence=sample("BNB","BUY")
    evidence["traces"].pop()  # root says it has one child
    with pytest.raises(ValueError):
        verify_original_native_conversion_trace(**evidence)


def test_unreported_extra_trace_is_rejected_even_when_wallet_balance_unchanged():
    evidence=sample("AVAX","BUY")
    evidence["traces"][0]["subtraces"]=2
    another=deepcopy(evidence["traces"][1])
    another["traceAddress"]=[1]
    another["action"]["from"]="0x"+"dd"*20
    another["action"]["to"]="0x"+"ee"*20
    another["action"]["value"]="0x0"
    evidence["traces"].append(another)
    assert verify_original_native_conversion_trace(
        **evidence)["full_historical_wallet_activity_proven"] is False
    evidence["traces"].pop()
    with pytest.raises(ValueError):
        verify_original_native_conversion_trace(**evidence)
