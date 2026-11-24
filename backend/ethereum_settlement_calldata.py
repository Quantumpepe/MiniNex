"""Restricted LI.FI Across V4 calldata inspection (no wallet execution).

ABI from lifinance/contracts, ILiFi.BridgeData, AcrossFacetV4.AcrossV4Data,
LibSwap.SwapData. Unknown facets, arbitrary calls and destination messages
remain indicative quotes only. No provider calldata is sent to the UI.
"""
from __future__ import annotations

import hashlib
import json
import re

from eth_abi import decode, encode
from eth_utils import keccak

from .live_eth_quotes import ETH_NATIVE, STABLES
from .native_evm_v3_quote import CHAINS
from .eth_swaprouter02_preflight import SWAPROUTER02_MAINNET, WETH_MAINNET

DIAMOND = "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"
BRIDGE = "(bytes32,string,string,address,address,address,uint256,uint256,bool,bool)"
ACROSS = "(bytes32,bytes32,bytes32,bytes32,uint256,uint128,bytes32,uint32,uint32,uint32,bytes)"
SWAP = "(address,address,address,address,uint256,bytes,bool)"
DIRECT = keccak(text="startBridgeTokensViaAcrossV4("+BRIDGE+","+ACROSS+")")[:4]
WITH_SWAP = keccak(text="swapAndStartBridgeTokensViaAcrossV4("+BRIDGE+","+SWAP+"[],"+ACROSS+")")[:4]
EXACT_SINGLE = bytes.fromhex("04e45aaf")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_DATA = re.compile(r"^0x(?:[0-9a-fA-F]{2}){4,16384}$")


def _address(value):
    if not isinstance(value,str) or not _ADDR.fullmatch(value):
        raise ValueError("Invalid route address")
    return value.lower()


def _word_address(value):
    if not isinstance(value,bytes) or len(value)!=32 or value[:12]!=bytes(12):
        raise ValueError("Not an EVM receiver")
    return "0x"+value[12:].hex()


def _uint(value):
    if isinstance(value,str) and re.fullmatch(r"0x[0-9a-fA-F]{1,64}",value):
        return int(value,16)
    if isinstance(value,str) and re.fullmatch(r"(0|[1-9][0-9]{0,77})",value):
        value=int(value)
    if type(value) is not int or not 0<=value<2**256:
        raise ValueError("Invalid EVM quantity")
    return value


def normalized_transaction(tx, chain):
    """Canonical identity also usable against an actual mined transaction."""
    if type(tx) is not dict or _uint(tx["chainId"])!=chain:
        raise ValueError("Wrong bridge source chain")
    data=tx.get("data",tx.get("input"))
    if not isinstance(data,str) or not _DATA.fullmatch(data):
        raise ValueError("Invalid bridge calldata")
    return {"chainId":chain,"from":_address(tx["from"]),
            "to":_address(tx["to"]),"data":data.lower(),
            "value":str(_uint(tx["value"]))}


def route_digest(tx, chain):
    normalized=normalized_transaction(tx,chain)
    return hashlib.sha256(json.dumps(normalized,sort_keys=True,
        separators=(",",":")).encode("ascii")).hexdigest()


def _source_registry(chain):
    if chain==1:
        return SWAPROUTER02_MAINNET.lower(), {x.lower() for x in (*STABLES.values(),WETH_MAINNET)}
    item=next((r for r in CHAINS.values() if r.chain_id==chain),None)
    if not item:
        raise ValueError("Unregistered source chain")
    return item.router.lower(), {item.stable_contract.lower(),item.wrapped.lower()}


def inspect_across_v4(tx, *, terms, now):
    """Bind exact source spend, destination token, minimum and refund owner.

    Support only a direct bridge or a single registered Router02 swap followed
    by a bridge with no destination call. This proves calldata shape, not
    deployed facet identity, liquidity, remote Privy permission or execution.
    """
    src,dst=terms["source_chain_id"],terms["destination_chain_id"]
    wallet=terms["wallet_address"].lower()
    token_in=terms["source_token"].lower()
    token_out=terms["destination_token"].lower()
    amount=int(terms["input_atomic"])
    minimum=int(terms["minimum_output_atomic"])
    tx=normalized_transaction(tx,src)
    if tx["from"]!=wallet or tx["to"]!=DIAMOND:
        raise ValueError("Unowned bridge sender or router")
    raw=bytes.fromhex(tx["data"][2:])
    selector=raw[:4]
    if selector==DIRECT:
        types=[BRIDGE,ACROSS]
    elif selector==WITH_SWAP:
        types=[BRIDGE,SWAP+"[]",ACROSS]
    else:
        raise ValueError("Unsupported bridge facet")
    values=decode(types,raw[4:],strict=True)
    if encode(types,values)!=raw[4:]:
        raise ValueError("Noncanonical bridge ABI")
    bridge,across=values[0],values[-1]
    tid,_,_,_,bridge_token,receiver,bridge_min,chain,swaps,dest_call=bridge
    if (tid==bytes(32) or receiver.lower()!=wallet or chain!=dst
            or dest_call or swaps!=(selector==WITH_SWAP) or bridge_min<=0):
        raise ValueError("Bridge destination or flags differ")
    (receiver32,refund32,sending32,receiving32,output,multiplier,
     _,timestamp,deadline,_,message)=across
    if (_word_address(receiver32)!=wallet or _word_address(refund32)!=wallet
            or _word_address(receiving32)!=token_out or message
            or timestamp>now+30 or deadline<=now):
        raise ValueError("Bridge receiver, refund, token or deadline differs")
    if token_in==ETH_NATIVE:
        if int(tx["value"])!=amount:
            raise ValueError("Wrong native bridge value")
    elif int(tx["value"])!=0:
        raise ValueError("Unexpected additional native spend")
    if selector==DIRECT:
        if bridge_token.lower()!=token_in or bridge_min!=amount or output<minimum:
            raise ValueError("Direct bridge amount or minimum differs")
        if token_in!=ETH_NATIVE and _word_address(sending32)!=token_in:
            raise ValueError("Direct bridge source token differs")
    else:
        if token_in==ETH_NATIVE or len(values[1])!=1:
            raise ValueError("Unsupported source swap sequence")
        call_to,approve_to,sending,receiving,from_amount,calldata,deposit=values[1][0]
        router,allowed=_source_registry(src)
        if (call_to.lower()!=router or approve_to.lower()!=router
                or sending.lower()!=token_in or receiving.lower()!=bridge_token.lower()
                or receiving.lower() not in allowed or sending.lower()==receiving.lower()
                or from_amount!=amount or not deposit or len(calldata)!=228
                or calldata[:4]!=EXACT_SINGLE
                or _word_address(sending32)!=receiving.lower()):
            raise ValueError("Unverified source swap")
        words=decode(["address","address","uint24","address","uint256","uint256","uint160"],calldata[4:],strict=True)
        a,b,fee,recipient,spend,min_swap,limit=words
        if (a.lower()!=token_in or b.lower()!=receiving.lower()
                or recipient.lower()!=DIAMOND or fee not in (100,500,3000,10000)
                or spend!=amount or min_swap<bridge_min or limit!=0
                or multiplier<=0 or bridge_min*multiplier//10**18<minimum):
            raise ValueError("Source swap or bridge minimum differs")
    return {"transaction":tx,"route_sha256":route_digest(tx,src),
            "bridge_transaction_id":"0x"+tid.hex(),
            "calldata_verified":True,"execution_authorized":False}
