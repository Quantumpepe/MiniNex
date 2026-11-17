"""Archived BNB/POL/AVAX wallet balance conservation and actual paid gas.

This checks observed trace-filter value changes against archived native
balances at the exact before/after blocks, including gas of every observed
owner-initiated top-level transaction (not only MiniNex swaps). Independent
canonical block hashes and original mined receipts are mandatory.

Do not infer historical wallet creation, source of funds, or a complete
performance-fee basis from this interval check. Unsupported archive RPCs fail
closed. No signing, transaction sending, or HWM mutation exists here.
"""
from __future__ import annotations

import re

_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS=re.compile(r"^0x[0-9a-f]{40}$")
_HEX=re.compile(r"^0x(?:0|[1-9a-f][0-9a-f]*)$")


def _atomic(value:object)->int:
    if type(value) is not str or len(value)>66 or _HEX.fullmatch(value.lower()) is None:
        raise ValueError("Noncanonical archival wallet atomic amount")
    amount=int(value,16)
    if amount>=10**45:
        raise ValueError("Unusable archived native balance")
    return amount


def verify_owner_native_balance_and_gas(
    *,wallet:str,traces:object,canonical_blocks:object,receipts:object,
    start_block:int,end_block:int,balance_before:object,balance_after:object,
)->dict:
    """Prove balance delta from every observed native value and sender gas."""
    if (type(wallet) is not str or _ADDRESS.fullmatch(wallet) is None
            or type(traces) is not list
            or type(canonical_blocks) is not dict
            or type(receipts) is not dict
            or type(start_block) is not int or type(end_block) is not int
            or not 1<start_block<=end_block):
        raise ValueError("Untrusted historic native wallet conservation source")
    start_balance=_atomic(balance_before)
    end_balance=_atomic(balance_after)
    value_in=value_out=0
    roots={}
    seen=set()
    for trace in traces:
        if (type(trace) is not dict or trace.get("type")!="call"
                or type(trace.get("traceAddress")) is not list
                or any(type(p) is not int or p<0
                       for p in trace["traceAddress"])
                or type(trace.get("transactionHash")) is not str
                or _HASH.fullmatch(trace["transactionHash"].lower()) is None
                or type(trace.get("blockNumber")) is not int
                or not start_block<=trace["blockNumber"]<=end_block
                or type(trace.get("blockHash")) is not str
                or _HASH.fullmatch(trace["blockHash"].lower()) is None
                or trace["blockHash"].lower()!=canonical_blocks.get(
                    trace["blockNumber"])
                or type(trace.get("action")) is not dict):
            raise ValueError("Noncanonical native value/gas trace")
        action=trace["action"]
        source,dest=action.get("from"),action.get("to")
        if (type(source) is not str or type(dest) is not str
                or _ADDRESS.fullmatch(source.lower()) is None
                or _ADDRESS.fullmatch(dest.lower()) is None):
            raise ValueError("Native wallet sender/recipient not verified")
        source,dest=source.lower(),dest.lower()
        amount=_atomic(action.get("value"))
        if amount and action.get("callType")!="call":
            raise ValueError("Native value sent via unsupported trace operation")
        tx=trace["transactionHash"].lower()
        path=tuple(trace["traceAddress"])
        if (tx,path) in seen:
            raise ValueError("Repeated original native value/gas trace")
        seen.add((tx,path))
        if not path and source==wallet:
            if tx in roots:
                raise ValueError("Duplicate original owner transaction root")
            roots[tx]=(trace,source,dest)
        # Failed CALL has no lasting account value movement; its root
        # transaction *still* spends gas, validated below.
        if trace.get("error") is not None:
            continue
        if source==wallet and dest!=wallet:
            value_out+=amount
        if dest==wallet and source!=wallet:
            value_in+=amount
    if not roots or set(receipts)!=set(roots):
        raise ValueError("Original owner root transaction gas receipts missing")
    gas_total=0
    for tx,(root,source,dest) in roots.items():
        receipt=receipts.get(tx)
        if (type(receipt) is not dict
                or receipt.get("transactionHash")!=tx
                or str(receipt.get("from","")).lower()!=wallet
                or str(receipt.get("to","")).lower()!=dest
                or receipt.get("blockHash","").lower()!=root["blockHash"].lower()
                or receipt.get("blockNumber")!=hex(root["blockNumber"])
                or type(root.get("transactionPosition")) is not int
                or _atomic(receipt.get("transactionIndex"))
                    !=root["transactionPosition"]
                or receipt.get("status") not in ("0x0","0x1")):
            raise ValueError("Original wallet gas receipt does not match root")
        gas=_atomic(receipt.get("gasUsed"))
        price=_atomic(receipt.get("effectiveGasPrice"))
        if gas<=0 or price<=0:
            raise ValueError("Confirmed native wallet receipt has no paid gas")
        gas_total+=gas*price
    expected=start_balance+value_in-value_out-gas_total
    if expected<0 or expected!=end_balance:
        raise ValueError("Historical native wallet balance delta unexplained")
    return {
        "mode":"ARCHIVED_NATIVE_WALLET_BALANCE_GAS_CONSERVATION_ONLY",
        "wallet_address":wallet,"start_block":start_block,
        "end_block":end_block,
        "balance_before_wei":str(start_balance),
        "balance_after_wei":str(end_balance),
        "total_observed_native_in_wei":str(value_in),
        "total_observed_native_out_wei":str(value_out),
        "total_verified_sender_gas_wei":str(gas_total),
        "observed_wallet_sender_transaction_count":len(roots),
        "observed_interval_native_balance_conserved":True,
        "original_gas_receipts_verified":True,
        "wallet_lifetime_and_acquisition_basis_proven":False,
        "five_percent_hwm_writable":False,
        "funds_transferred":False,
    }
