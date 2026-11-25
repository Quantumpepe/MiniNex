"""Read-only execution preparation and independent direct Stargate taxi proof.

The existing user-approved staged policy determines the exact route. No
LI.FI status, wallet-wide balance increase, or provider quote establishes
a completed stage. Only a canonical source transaction and GUID-matched
destination credit may unlock the next stage. This module never sends.
"""
from __future__ import annotations
import asyncio
import re
import time
from eth_abi import encode,decode
from eth_utils import keccak
from .ethereum_stargate_staged_policy import (
    _route,ADDR,SEND_PARAM,FEE_PARAM,bridge_transaction,
    inspect_staged_bridge_transaction,
)
from .ethereum_settlement_stargate import CONFIG,_event
from .ethereum_settlement_bridge_proof import address_word
from .ethereum_settlement_status import (
    _rpc_url,_finalized_receipt,_net_token_credit,_eq,
)
from .shared_evm_swap_receipts import _receipt_rpc
from .live_eth_receipts import _num

_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_QUOTE_TYPES=["(uint256,uint256)","(int256,string)[]","(uint256,uint256)"]
_TRANSFER="0x"+keccak(text="Transfer(address,address,uint256)").hex()


def _abi_result(raw,types):
    if type(raw) is not str or len(raw)>32770 or not re.fullmatch(r"0x[0-9a-fA-F]+",raw):
        raise ValueError("Malformed Stargate RPC result")
    try:
        payload=bytes.fromhex(raw[2:])
        values=decode(types,payload,strict=True)
        if encode(types,values)!=payload:
            raise ValueError("Noncanonical Stargate RPC result")
        return values
    except Exception as exc:
        raise ValueError("Invalid Stargate ABI result") from exc


def _call_data(signature,types=(),values=()):
    return "0x"+(keccak(text=signature)[:4]+encode(list(types),list(values))).hex()


def _send_param(dst,wallet,amount,minimum):
    return (CONFIG[dst]["eid"],address_word(wallet),amount,minimum,b"",b"",b"")


def decode_oft_quote(raw,amount):
    if type(amount) is not int or not 0<amount<2**256:
        raise ValueError("Exact source amount required")
    limits,fees,receipt=_abi_result(raw,_QUOTE_TYPES)
    low,high=limits
    sent,received=receipt
    if not low<=sent==amount<=high or not 0<received<2**256:
        raise ValueError("Stargate liquidity or exact amount unavailable")
    # The deployed pools share six-decimal USDC/USDT. A possible protocol
    # reward may increase output, but must not increase the source spend.
    minimum=min(amount,max(1,received*9950//10000))
    return {"sent_atomic":sent,"expected_atomic":received,"minimum_atomic":minimum}


def decode_native_fee(raw):
    native,lz=_abi_result(raw,[FEE_PARAM])[0]
    if native<=0 or lz!=0:
        raise ValueError("Unreviewed Stargate messaging fee")
    return native


async def prepare_direct_taxi(client,*,side,stable,wallet,scope,amount_atomic):
    """Quote exact direct sendToken or finite allowance on real source RPC.

    Destination block baseline is captured BEFORE any send; it may later
    be used for a GUID-filtered log search, never as proof by itself.
    """
    src,dst,pool,token=_route(side,stable)
    if type(wallet) is not str or not ADDR.fullmatch(wallet):
        raise ValueError("Original Ethereum wallet required")
    if type(amount_atomic) is not int or not 0<amount_atomic<2**256:
        raise ValueError("Exact staged source amount required")
    wallet=wallet.lower()
    src_url,dst_url=_rpc_url(src),_rpc_url(dst)
    async def rpc(method,params,rid):
        return await _receipt_rpc(client,src_url,method,params,rid)
    async def target_rpc(method,params,rid):
        return await _receipt_rpc(client,dst_url,method,params,rid)
    chains=await asyncio.gather(rpc("eth_chainId",[],1),target_rpc("eth_chainId",[],2))
    if tuple(map(_num,chains))!=(src,dst):
        raise ValueError("Wrong staged settlement network")
    source_height,destination_height=await asyncio.gather(
        rpc("eth_blockNumber",[],3),target_rpc("eth_blockNumber",[],4))
    if min(_num(source_height),_num(destination_height))<=0:
        raise ValueError("Stargate block baseline unavailable")
    async def call(contract,data,rid):
        return await rpc("eth_call",[{"from":wallet,"to":contract,"data":data},source_height],rid)
    dst_pool=CONFIG[dst]["pools"][stable]
    dst_token=CONFIG[dst]["tokens"][stable]
    source_asset,target_asset,source_precision,target_precision=await asyncio.gather(
        call(pool,_call_data("token()"),5),
        target_rpc("eth_call",[{"to":dst_pool,"data":_call_data("token()")},destination_height],6),
        call(token,_call_data("decimals()"),7),
        target_rpc("eth_call",[{"to":dst_token,"data":_call_data("decimals()")},destination_height],8))
    if (_abi_result(source_asset,["address"])[0].lower()!=token.lower()
            or _abi_result(target_asset,["address"])[0].lower()!=dst_token.lower()
            or _abi_result(source_precision,["uint8"])!=(6,)
            or _abi_result(target_precision,["uint8"])!=(6,)):
        raise ValueError("Original stablecoin pool identity changed")
    param=_send_param(dst,wallet,amount_atomic,0)
    raw=await call(pool,_call_data("quoteOFT("+SEND_PARAM+")",[SEND_PARAM],[param]),9)
    quote=decode_oft_quote(raw,amount_atomic)
    minimum=quote["minimum_atomic"]
    param=_send_param(dst,wallet,amount_atomic,minimum)
    raw_fee=await call(pool,_call_data("quoteSend("+SEND_PARAM+",bool)",[SEND_PARAM,"bool"],[param,False]),10)
    fee=decode_native_fee(raw_fee)
    tx=bridge_transaction(side=side,stable=stable,wallet=wallet,amount_atomic=amount_atomic,
                          min_out_atomic=minimum,native_fee_atomic=fee)
    inspect_staged_bridge_transaction(tx,side=side,stable=stable,wallet=wallet,
        scope=scope,amount_atomic=amount_atomic,min_out_atomic=minimum,native_fee_atomic=fee)
    balance,allowance,native,price=await asyncio.gather(
        call(token,_call_data("balanceOf(address)",["address"],[wallet]),11),
        call(token,_call_data("allowance(address,address)",["address","address"],[wallet,pool]),12),
        rpc("eth_getBalance",[wallet,source_height],13),rpc("eth_gasPrice",[],14))
    balance=_abi_result(balance,["uint256"])[0]
    allowance=_abi_result(allowance,["uint256"])[0]
    if balance<amount_atomic:
        raise ValueError("Insufficient exact staged source funds")
    approval=None
    if allowance<amount_atomic:
        # ERC20 USDT may require a zero reset before replacing an allowance.
        value=0 if stable=="USDT" and allowance>0 else amount_atomic
        approval={"from":wallet,"to":token.lower(),"chainId":src,"value":"0",
            "data":_call_data("approve(address,uint256)",["address","uint256"],[pool,value])}
    candidate=approval or tx
    envelope={k:v for k,v in candidate.items() if k!="chainId"}
    envelope["value"]=hex(int(candidate["value"]))
    gas=_num(await rpc("eth_estimateGas",[envelope],15))
    price=_num(price)
    if gas<=0 or price<=0 or _num(native)<gas*price+fee:
        raise ValueError("Staged bridge gas and messaging funds unavailable")
    return {"mode":"AVAX_DIRECT_STARGATE_TAXI_PREPARATION","asset":"AVAX",
            "side":side,"stable":stable,"wallet_address":wallet,
            "source_chain_id":src,"destination_chain_id":dst,
            "source_token":token,"destination_token":dst_token,
            "input_atomic":str(amount_atomic),
            "expected_output_atomic":str(quote["expected_atomic"]),
            "minimum_output_atomic":str(minimum),"native_bridge_fee_atomic":str(fee),
            "estimated_gas_atomic":str(gas*price),"source_balance_atomic":str(balance),
            "source_allowance_atomic":str(allowance),
            "destination_start_block":_num(destination_height),
            "transaction":tx,"approval":approval,
            "issued_at":int(time.time()),"can_submit":False}


def _source_event(logs,*,side,stable,wallet,amount,minimum):
    src,dst,pool,token=_route(side,stable)
    ((guid,sender),(eid,sent,credited))=_event(logs,pool,
        "OFTSent(bytes32,uint32,address,uint256,uint256)",
        ["uint32","uint256","uint256"],2)
    if (guid==bytes(32) or sender!=address_word(wallet) or eid!=CONFIG[dst]["eid"]
            or sent!=amount or credited<minimum
            or _net_token_credit(logs,token,wallet)!=-sent):
        raise ValueError("Source taxi event differs from exact original debit")
    return guid,credited


def prove_direct_taxi_credit(source_logs,destination_logs,*,side,stable,wallet,amount,minimum):
    """Same GUID, original sender, original recipient and actual net credit."""
    src,dst,_,_=_route(side,stable)
    guid,credited=_source_event(source_logs,side=side,stable=stable,
                                 wallet=wallet,amount=amount,minimum=minimum)
    ((target_guid,recipient),(origin,received))=_event(
        destination_logs,CONFIG[dst]["pools"][stable],
        "OFTReceived(bytes32,uint32,address,uint256)",["uint32","uint256"],2)
    actual=_net_token_credit(destination_logs,CONFIG[dst]["tokens"][stable],wallet)
    if (target_guid!=guid or recipient!=address_word(wallet) or origin!=CONFIG[src]["eid"]
            or received!=credited or actual!=received or received<minimum):
        raise ValueError("Destination taxi credit differs from original GUID")
    return {"guid":"0x"+guid.hex(),"received_atomic":str(actual),
            "bridge_message_verified":True,"destination_credit_verified":True}


async def reconcile_direct_taxi(client,*,prepared,source_hash,destination_hash=None):
    """Independently finalized transaction pair; read-only, never retries a send.

    A candidate destination hash can come from a later GUID-filtered log
    discovery. This function independently verifies the actual pair.
    """
    if not isinstance(source_hash,str) or not _HASH.fullmatch(source_hash):
        raise ValueError("Original source transaction hash required")
    side,stable,wallet=(prepared[k] for k in ("side","stable","wallet_address"))
    src,dst,pool,token=_route(side,stable)
    amount=int(prepared["input_atomic"]);minimum=int(prepared["minimum_output_atomic"])
    fee=int(prepared["native_bridge_fee_atomic"])
    expected=bridge_transaction(side=side,stable=stable,wallet=wallet,
        amount_atomic=amount,min_out_atomic=minimum,native_fee_atomic=fee)
    if prepared["transaction"]!=expected:
        raise ValueError("Persisted direct taxi transaction changed")
    source=await _finalized_receipt(client,src,source_hash)
    if source is None:
        return {"state":"SOURCE_PENDING"}
    tx,receipt=source
    if (not _eq(tx.get("from"),wallet) or not _eq(tx.get("to"),pool)
            or not _eq(tx.get("input"),expected["data"])
            or _num(tx.get("value"))!=fee):
        raise ValueError("Finalized source differs from direct taxi intent")
    gas=_num(receipt["gasUsed"])*_num(receipt["effectiveGasPrice"])
    status=_num(receipt["status"])
    if status==0:
        return {"state":"SOURCE_REVERTED","source_gas_atomic":str(gas)}
    if status!=1:
        raise ValueError("Unexpected source receipt status")
    guid,credited=_source_event(receipt["logs"],side=side,stable=stable,
                          wallet=wallet,amount=amount,minimum=minimum)
    pending={"state":"DESTINATION_PENDING","guid":"0x"+guid.hex(),
             "source_gas_atomic":str(gas),"source_hash":source_hash.lower(),
             "expected_credit_atomic":str(credited)}
    if destination_hash is None:
        return pending
    if not isinstance(destination_hash,str) or not _HASH.fullmatch(destination_hash):
        raise ValueError("Invalid destination candidate hash")
    target=await _finalized_receipt(client,dst,destination_hash)
    if target is None:
        return pending
    _,target_receipt=target
    if _num(target_receipt["status"])!=1:
        raise ValueError("Destination receipt was not successful")
    proof=prove_direct_taxi_credit(receipt["logs"],target_receipt["logs"],
            side=side,stable=stable,wallet=wallet,amount=amount,minimum=minimum)
    return {**pending,**proof,"state":"BRIDGE_CREDIT_VERIFIED",
            "destination_hash":destination_hash.lower()}
