"""Read-only exact quotes and finalized credit evidence for staged Across.

No signing, sending or new live session. A caller must persist each original
stage, independently verify consent and reserve original capital before use.
"""
from __future__ import annotations
import asyncio
import os
import time
import uuid

from .ethereum_across_staged_plan import (
    staged_route, bridge_terms, inspect_staged_bridge, staged_local_transaction,
)
from .ethereum_settlement_quote import QUOTE_URL, _read_quote
from .ethereum_settlement_preflight import prepare_bridge_privy_request
from .ethereum_settlement_bridge_proof import prove_across_pair
from .ethereum_settlement_status import _rpc_url, _finalized_receipt, _net_token_credit, _eq
from .ethereum_stargate_staged_execution import _abi_result, _call_data, _HASH
from .shared_evm_swap_receipts import _receipt_rpc
from .live_eth_quotes import _quantity
from .live_eth_receipts import _num
from .eth_router02_live_quote import _decode_quote
from .native_evm_v3_quote import CHAINS
from .shared_evm_execution import ADDRESS


async def prepare_staged_across_bridge(client,*,asset,side,stable,wallet,wallet_id,amount_atomic):
    route=staged_route(asset,side,stable)
    src,dst=route["bridge_source"],route["bridge_target"]
    if type(wallet) is not str or not ADDRESS.fullmatch(wallet) or type(amount_atomic) is not int or amount_atomic<=0:
        raise ValueError("Exact owner and original bridge amount required")
    wallet=wallet.lower()
    headers={"Accept":"application/json"}
    api_key=os.getenv("MININEX_LIFI_API_KEY","").strip()
    if api_key:
        headers["x-lifi-api-key"]=api_key
    response=await client.get(QUOTE_URL,headers=headers,params={
        "fromChain":src[0],"toChain":dst[0],"fromToken":src[1],"toToken":dst[1],
        "fromAddress":wallet,"toAddress":wallet,"fromAmount":str(amount_atomic),
        "slippage":"0.005","allowBridges":"across","allowDestinationCall":"false"})
    response.raise_for_status()
    if len(response.content)>512000:
        raise ValueError("Oversized staged bridge quote")
    payload=response.json()
    _,minimum=_read_quote(payload,wallet=wallet,source_chain=src[0],dest_chain=dst[0],
        token_in=src[1],token_out=dst[1],source_decimals=src[2],dest_decimals=dst[2],
        units=str(amount_atomic))
    _,minimum_atomic=_quantity(minimum,dst[2])
    terms=bridge_terms(asset,side,stable,wallet,amount_atomic,minimum_atomic,int(time.time()))
    review=inspect_staged_bridge(terms,payload["transactionRequest"],now=int(time.time()))
    terms={**terms,"quote_id":str(uuid.uuid4()),"route_sha256":review["route_sha256"],
           "unsigned_transaction":review["transaction"]}
    # The shared preflight is read-only, including gas estimate and finite
    # allowance candidate. This ID is only an ephemeral stage quote ID.
    preflight=await prepare_bridge_privy_request(client,terms,wallet_id=wallet_id,
        now=int(time.time()),manual=True,staged_reset=(stable if side=="BUY" else route["transit_symbol"])=="USDT")
    target_url=_rpc_url(dst[0])
    chain,height=await asyncio.gather(
        _receipt_rpc(client,target_url,"eth_chainId",[],80),
        _receipt_rpc(client,target_url,"eth_blockNumber",[],81))
    if _num(chain)!=dst[0] or _num(height)<=0:
        raise ValueError("Wrong bridge destination or missing block baseline")
    return {"mode":"ACROSS_STAGED_BRIDGE_PREPARATION","terms":terms,"preflight":preflight,
            "destination_start_block":_num(height),"persisted":False,
            "execution_authorized":False,"can_submit":False}


async def prepare_staged_across_local_swap(client,*,asset,side,stable,wallet,amount_atomic):
    route=staged_route(asset,side,stable)
    src,dst=route["swap_source"],route["swap_target"]
    if type(wallet) is not str or not ADDRESS.fullmatch(wallet) or type(amount_atomic) is not int or amount_atomic<=0:
        raise ValueError("Exact original swap owner and amount required")
    wallet=wallet.lower()
    chain=CHAINS[asset]
    router,quoter=chain.router.lower(),chain.quoter.lower()
    url=_rpc_url(src[0])
    async def rpc(method,params,rid):
        return await _receipt_rpc(client,url,method,params,rid)
    if _num(await rpc("eth_chainId",[],1))!=src[0]:
        raise ValueError("Wrong local stage network")
    height=await rpc("eth_blockNumber",[],2)
    async def call(contract,data,rid):
        return await rpc("eth_call",[{"from":wallet,"to":contract,"data":data},height],rid)
    p1,p2,balance,allowance,native,gas_price=await asyncio.gather(
        call(src[1],_call_data("decimals()"),3),
        call(dst[1],_call_data("decimals()"),4),
        call(src[1],_call_data("balanceOf(address)",["address"],[wallet]),5),
        call(src[1],_call_data("allowance(address,address)",["address","address"],[wallet,router]),6),
        rpc("eth_getBalance",[wallet,height],7),rpc("eth_gasPrice",[],8))
    if (_abi_result(p1,["uint8"])[0],_abi_result(p2,["uint8"])[0])!=(src[2],dst[2]):
        raise ValueError("Original local stablecoin precision changed")
    balance=_abi_result(balance,["uint256"])[0]
    allowance=_abi_result(allowance,["uint256"])[0]
    if balance<amount_atomic:
        raise ValueError("Original swap funds unavailable")
    quote_type="(address,address,uint256,uint24,uint160)"
    fees=(100,500,3000,10000)
    results=await asyncio.gather(*(call(quoter,_call_data(
        "quoteExactInputSingle("+quote_type+")",[quote_type],[(src[1],dst[1],amount_atomic,fee,0)]),20+i)
        for i,fee in enumerate(fees)),return_exceptions=True)
    candidates=[]
    for fee,raw in zip(fees,results):
        if isinstance(raw,str):
            try:
                output,gas=_decode_quote(raw)
                if output>0:
                    candidates.append((output,-gas,-fee))
            except ValueError:
                continue
    if not candidates:
        raise ValueError("No verified local swap liquidity")
    expected,_,neg_fee=max(candidates)
    fee=-neg_fee
    minimum=max(1,expected*9950//10000)
    tx=staged_local_transaction(asset,side,stable,wallet,amount_atomic,minimum,fee)
    approval=None
    if allowance<amount_atomic:
        reset=side=="BUY" and chain.stable=="USDT" and allowance>0
        approval={"from":wallet,"to":src[1],"chainId":src[0],"value":"0",
            "data":_call_data("approve(address,uint256)",["address","uint256"],[router,0 if reset else amount_atomic])}
    envelope={k:v for k,v in (approval or tx).items() if k!="chainId"}
    envelope["value"]="0x0"
    gas=_num(await rpc("eth_estimateGas",[envelope],30))
    price=_num(gas_price)
    if gas<=0 or price<=0 or _num(native)<gas*price:
        raise ValueError("Local swap network gas unavailable")
    return {"mode":"ACROSS_STAGED_SWAP_PREPARATION","asset":asset,"side":side,"stable":stable,
        "wallet_address":wallet,"source_chain_id":src[0],"destination_chain_id":dst[0],
        "source_token":src[1],"destination_token":dst[1],"source_decimals":src[2],
        "destination_decimals":dst[2],"input_atomic":str(amount_atomic),
        "expected_output_atomic":str(expected),"minimum_output_atomic":str(minimum),"fee_tier":fee,
        "source_balance_atomic":str(balance),"source_allowance_atomic":str(allowance),
        "estimated_gas_atomic":str(gas*price),"transaction":tx,"approval":approval,
        "issued_at":int(time.time()),"can_submit":False}


def _receipt_matches(tx,expected):
    if (not _eq(tx.get("from"),expected["from"]) or not _eq(tx.get("to"),expected["to"])
            or not _eq(tx.get("input"),expected["data"]) or _num(tx.get("value"))!=int(expected["value"])):
        raise ValueError("Finalized transaction differs from original staged order")


async def reconcile_staged_across_local_swap(client,*,prepared,tx_hash):
    if type(tx_hash) is not str or not _HASH.fullmatch(tx_hash):
        raise ValueError("Original local swap hash required")
    asset,side,stable,wallet=(prepared[k] for k in ("asset","side","stable","wallet_address"))
    route=staged_route(asset,side,stable)
    src,dst=route["swap_source"],route["swap_target"]
    amount,minimum=int(prepared["input_atomic"]),int(prepared["minimum_output_atomic"])
    expected=staged_local_transaction(asset,side,stable,wallet,amount,minimum,prepared["fee_tier"])
    if (prepared["transaction"]!=expected or prepared["source_token"]!=src[1]
            or prepared["destination_token"]!=dst[1] or prepared["source_chain_id"]!=src[0]
            or prepared["destination_chain_id"]!=dst[0]):
        raise ValueError("Original local stage identity changed")
    result=await _finalized_receipt(client,src[0],tx_hash)
    if result is None:
        return {"state":"SOURCE_PENDING"}
    tx,receipt=result
    _receipt_matches(tx,expected)
    gas=_num(receipt["gasUsed"])*_num(receipt["effectiveGasPrice"])
    status=_num(receipt["status"])
    if status==0:
        return {"state":"SOURCE_REVERTED","source_gas_atomic":str(gas)}
    if status!=1:
        raise ValueError("Invalid local swap receipt status")
    received=_net_token_credit(receipt["logs"],dst[1],wallet)
    if -_net_token_credit(receipt["logs"],src[1],wallet)!=amount or received<minimum:
        raise ValueError("Original local swap debit or credit differs")
    return {"state":"SWAP_CREDIT_VERIFIED","source_hash":tx_hash.lower(),
            "received_atomic":str(received),"source_gas_atomic":str(gas),"destination_credit_verified":True}


async def reconcile_staged_across_bridge(client,*,terms,source_hash,destination_hash=None):
    if type(source_hash) is not str or not _HASH.fullmatch(source_hash):
        raise ValueError("Original bridge source hash required")
    review=inspect_staged_bridge(terms,terms["unsigned_transaction"],now=terms["issued_at"])
    if review["route_sha256"]!=terms["route_sha256"]:
        raise ValueError("Original direct bridge digest changed")
    src,dst=terms["source_chain_id"],terms["destination_chain_id"]
    source=await _finalized_receipt(client,src,source_hash)
    if source is None:
        return {"state":"SOURCE_PENDING"}
    tx,receipt=source
    _receipt_matches(tx,review["transaction"])
    gas=_num(receipt["gasUsed"])*_num(receipt["effectiveGasPrice"])
    status=_num(receipt["status"])
    if status==0:
        return {"state":"SOURCE_REVERTED","source_gas_atomic":str(gas)}
    wallet=terms["wallet_address"]
    if status!=1 or -_net_token_credit(receipt["logs"],terms["source_token"],wallet)!=int(terms["input_atomic"]):
        raise ValueError("Original direct bridge debit differs")
    pending={"state":"DESTINATION_PENDING","source_hash":source_hash.lower(),"source_gas_atomic":str(gas)}
    if destination_hash is None:
        return pending
    if type(destination_hash) is not str or not _HASH.fullmatch(destination_hash):
        raise ValueError("Original bridge destination hash required")
    destination=await _finalized_receipt(client,dst,destination_hash)
    if destination is None:
        return pending
    _,target=destination
    if _num(target["status"])!=1:
        raise ValueError("Destination bridge receipt failed")
    received=_net_token_credit(target["logs"],terms["destination_token"],wallet)
    proof=prove_across_pair(receipt["logs"],target["logs"],src=src,dst=dst,wallet=wallet,
        route=review,received=received)
    if not proof or received!=review["direct_output_atomic"] or received<int(terms["minimum_output_atomic"]):
        raise ValueError("No original direct bridge fill proved")
    return {**pending,**proof,"state":"BRIDGE_CREDIT_VERIFIED","destination_hash":destination_hash.lower(),
            "received_atomic":str(received),"destination_credit_verified":True}
