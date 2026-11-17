"""Independent native-coin conversion custody via archived EVM call traces.

Receipt/event verification alone proves a WETH9 deposit/withdraw was emitted,
not that the wallet received/paid the correct native coin without an unrelated
native transfer. Parity's trace_transaction provides the original internal
native-value calls, including the withdraw payout to the user.

This is a strict historical *single transaction* proof, NOT a claim that all
other wallet activity across the trading period was audited. RPCs without a
complete trace_transaction implementation fail closed. No fee, HWM, signing,
order throttling, or wallet grant is performed.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

import httpx
from fastapi import HTTPException

from .multicoin_evm_balances import _RPC
from .shared_evm_swap_receipts import _pair, _receipt_rpc

_NATIVE = frozenset(("BNB","POL","AVAX"))
_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_HEX = re.compile(r"^0x(?:0|[1-9a-f][0-9a-f]*)$")
_ATOMIC = Decimal(10)**18


def _inthex(raw:object)->int:
    if type(raw) is not str or len(raw)>66 or _HEX.fullmatch(raw.lower()) is None:
        raise ValueError("Invalid original onchain integer")
    return int(raw,16)


def _quantity(value:object)->int:
    if type(value) is not str or not value or len(value)>70 or "e" in value.lower():
        raise ValueError("Original native quantity must be exact")
    try:
        atom=Decimal(value)*_ATOMIC
    except (ArithmeticError,InvalidOperation):
        raise ValueError("Malformed original native quantity") from None
    if (not atom.is_finite() or atom<=0 or atom>=Decimal(10)**45
            or atom!=atom.to_integral_value()):
        raise ValueError("Original native amount is not exact atomic units")
    return int(atom)


def verify_original_native_conversion_trace(
    *,asset:str,side:str,stable:str,fill:object,
    conversion:object,receipt:object,traces:object,finalized:object,
    canonical_block:object,
)->dict:
    """Require exact original wrap/unwrap native-value movement and paid gas.

    A provider-reported trace is checked against independently verified original
    conversion cost, canonical mined receipt, and finalized block. Every
    wallet-related native-value call must be the one expected conversion.
    An incomplete, forged, reverted, or unrelated transfer is rejected.
    """
    if (asset not in _NATIVE or side not in ("BUY","SELL")
            or type(fill) is not dict or type(conversion) is not dict
            or type(receipt) is not dict or type(finalized) is not dict
            or type(canonical_block) is not dict
            or type(traces) is not list or not 1<=len(traces)<=512):
        raise ValueError("Complete original native conversion trace required")
    route=_pair(asset,stable,side)
    wallet=fill.get("wallet_address")
    tx=conversion.get("tx_hash")
    swapped=fill.get("tx_hash")
    if (type(wallet) is not str or _ADDRESS.fullmatch(wallet) is None
            or type(tx) is not str or _HASH.fullmatch(tx) is None
            or type(swapped) is not str or _HASH.fullmatch(swapped) is None
            or tx==swapped
            or fill.get("asset")!=asset or fill.get("side")!=side
            or fill.get("stable")!=stable
            or fill.get("chain_id")!=route["chain_id"]
            or fill.get("onchain_finalized") is not True
            or conversion.get("mode")!="INDEPENDENT_FINALIZED_NATIVE_EVM_CONVERSION_COST"
            or conversion.get("asset")!=asset or conversion.get("side")!=side
            or conversion.get("chain_id")!=route["chain_id"]
            or conversion.get("wallet_address")!=wallet
            or conversion.get("swap_tx_hash")!=swapped
            or conversion.get("intent_key")!=fill.get("intent_key")
            or conversion.get("finalized") is not True
            or receipt.get("transactionHash")!=tx
            or str(receipt.get("from","")).lower()!=wallet
            or type(receipt.get("blockHash")) is not str
            or _HASH.fullmatch(receipt["blockHash"].lower()) is None
            or receipt.get("status")!="0x1"):
        raise ValueError("Wrong owner, chain, original native cost or receipt")
    wrapped=_pair(asset,stable,"SELL")["source_token"].lower()
    if (str(receipt.get("to","")).lower()!=wrapped
            or conversion.get("tx_hash")!=tx):
        raise ValueError("Native conversion must use canonical wrapped contract")
    try:
        block=_inthex(receipt["blockNumber"])
        transaction_index=_inthex(receipt["transactionIndex"])
        final_height=_inthex(finalized["number"])
        conv_height=conversion.get("block_number")
        canonical_height=_inthex(canonical_block["number"])
        canonical_hash=canonical_block["hash"]
        units=_quantity(conversion.get("quantity_native"))
        gas=_inthex(receipt["gasUsed"])
        price=_inthex(receipt["effectiveGasPrice"])
        if (type(conv_height) is not int or block!=conv_height or
                block!=canonical_height or
                type(canonical_hash) is not str or
                canonical_hash.lower()!=receipt["blockHash"].lower() or
                not 0<block<=final_height or gas<=0 or price<=0
                or Decimal(gas*price)/_ATOMIC !=
                    Decimal(conversion["network_fee_native"])):
            raise ValueError("Native original paid gas or canonical block differs")
        swap_amount=(fill.get("destination_amount") if side=="BUY"
                     else fill.get("source_amount"))
        if units!=_quantity(swap_amount):
            raise ValueError("Original swap and conversion native amounts differ")
    except (KeyError,InvalidOperation,ArithmeticError,TypeError):
        raise ValueError("Unverifiable original native transaction receipt") from None
    wanted="WITHDRAW" if side=="BUY" else "DEPOSIT"
    if conversion.get("operation")!=wanted:
        raise ValueError("Wrong conversion direction")
    paths=set()
    matches=[]
    root=None
    for line in traces:
        if (type(line) is not dict or line.get("type")!="call"
                or line.get("error") is not None
                or type(line.get("action")) is not dict
                or type(line.get("result")) is not dict
                or type(line.get("traceAddress")) is not list
                or type(line.get("transactionHash")) is not str
                or line["transactionHash"].lower()!=tx
                or type(line.get("blockHash")) is not str
                or line["blockHash"].lower()!=receipt["blockHash"].lower()
                or type(line.get("blockNumber")) is not int
                or line["blockNumber"]!=block
                or type(line.get("transactionPosition")) is not int
                or line["transactionPosition"]!=transaction_index):
            raise ValueError("Incomplete, reverted, or foreign native transaction trace")
        path=line["traceAddress"]
        if len(path)>32 or any(type(x) is not int or x<0 for x in path):
            raise ValueError("Noncanonical trace position")
        position=tuple(path)
        if position in paths:
            raise ValueError("Repeated trace branch")
        paths.add(position)
        action=line["action"]
        sender=action.get("from")
        receiver=action.get("to")
        if (type(sender) is not str or _ADDRESS.fullmatch(sender.lower()) is None
                or type(receiver) is not str or _ADDRESS.fullmatch(receiver.lower()) is None):
            raise ValueError("Untrusted native trace transfer address")
        sender,receiver=sender.lower(),receiver.lower()
        value=_inthex(action.get("value"))
        if position==():
            root=(sender,receiver,value)
        if value and (sender==wallet or receiver==wallet):
            matches.append((position,sender,receiver,value))
    if root is None or root[:2]!=(wallet,wrapped):
        raise ValueError("Missing owned original conversion root call")
    expected=((),wallet,wrapped,units) if side=="SELL" else (
        None,wrapped,wallet,units)
    if side=="SELL":
        if root[2]!=units or matches!=[expected]:
            raise ValueError("Unexpected native deposit/external value movement")
    else:
        if root[2]!=0 or len(matches)!=1:
            raise ValueError("Missing unique native withdrawal payout")
        path,sender,receiver,value=matches[0]
        if not path or (sender,receiver,value)!=(wrapped,wallet,units):
            raise ValueError("Native payout belongs to another internal call")
    return {
        "mode":"INDEPENDENT_ORIGINAL_NATIVE_CONVERSION_TRACE_ONLY",
        "asset":asset,"side":side,"chain_id":route["chain_id"],
        "wallet_address":wallet,"tx_hash":tx,
        "swap_tx_hash":swapped,
        "native_atomic_received" : str(units if side=="BUY" else 0),
        "native_atomic_spent":str(units if side=="SELL" else 0),
        "paid_network_gas_wei":str(gas*price),
        "original_finalized_internal_value_proven":True,
        "unexpected_native_value_in_conversion_absent":True,
        "full_historical_wallet_activity_proven":False,
        "hwm_fee_assessed":False,"auto_fee_transferred":False,
    }


async def read_original_native_conversion_trace(
    *,asset:str,side:str,stable:str,fill:dict,
    conversion:dict,client:httpx.AsyncClient,
)->dict:
    """Get original trace/receipt over the operator's pinned network RPC."""
    if (asset not in _NATIVE or type(client) is not httpx.AsyncClient
            or type(fill) is not dict or type(conversion) is not dict):
        raise ValueError("Unsupported original native conversion audit")
    route=_pair(asset,stable,side)
    chain,network,fallback,env=_RPC[asset]
    if chain!=route["chain_id"] or network!=route["network"]:
        raise ValueError("Original native conversion chain mismatch")
    import os
    url=os.getenv(env,"").strip() or fallback
    if not url.startswith("https://") or any(ch in url for ch in " \r\n\t#@"):
        raise HTTPException(503,"Archive native EVM RPC not configured")
    tx=conversion.get("tx_hash")
    if type(tx) is not str or _HASH.fullmatch(tx) is None:
        raise ValueError("Original native conversion transaction hash missing")
    try:
        rpc_chain=await _receipt_rpc(client,url,"eth_chainId",[],90)
        if _inthex(rpc_chain)!=chain:
            raise ValueError("Archive RPC chain mismatch")
        receipt=await _receipt_rpc(client,url,"eth_getTransactionReceipt",[tx],91)
        finalized=await _receipt_rpc(
            client,url,"eth_getBlockByNumber",["finalized",False],92)
        canonical=await _receipt_rpc(
            client,url,"eth_getBlockByNumber",[receipt["blockNumber"],False],93)
        traces=await _receipt_rpc(client,url,"trace_transaction",[tx],94)
        return verify_original_native_conversion_trace(
            asset=asset,side=side,stable=stable,fill=fill,
            conversion=conversion,receipt=receipt,traces=traces,
            finalized=finalized,canonical_block=canonical)
    except (httpx.HTTPError,ValueError,TypeError,KeyError,OverflowError):
        raise HTTPException(
            502,"Original native archive trace unsupported or unverified") from None
