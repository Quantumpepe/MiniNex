"""Exact two-stage Ethereum/Avalanche settlement grant.

Separate a direct Stargate taxi (same owner, no composed message) and a
Router02 swap. Never authorize opaque nested LI.FI calldata. Intermediate
Avalanche stablecoins belong to a pending settlement, not available capital.
This is a policy and exact transaction compiler, NOT an execution engine.
"""
from __future__ import annotations
import re
from eth_abi import encode,decode
from eth_utils import keccak
from .ethereum_settlement_stargate import CONFIG
from .ethereum_settlement_plan import CROSS_COIN_CONTRACTS
from .native_evm_v3_quote import CHAINS
from .eth_swaprouter02_preflight import EXACT_INPUT_SINGLE_ABI
from .eth_privy_user_policy_blueprint import _tx,_call,_expiry,_positive_atomic
from .shared_evm_single_grant_policy import ERC20_APPROVE_ABI

STARGATE_STAGED_POLICY="ETHEREUM_STARGATE_STAGED_V1"
SEND_PARAM="(uint32,bytes32,uint256,uint256,bytes,bytes,bytes)"
FEE_PARAM="(uint256,uint256)"
SEND_TYPES=[SEND_PARAM,FEE_PARAM,"address"]
SEND_SELECTOR=keccak(text="sendToken("+",".join(SEND_TYPES)+")")[:4]
ADDR=re.compile(r"^0x[0-9a-fA-F]{40}$")
ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")
def _fields(items):
    return [{"name":n,"type":t} for n,t in items]
SEND_ABI=[{"type":"function","name":"sendToken","stateMutability":"payable",
           "inputs":[{"name":"_sendParam","type":"tuple","components":_fields([
               ("dstEid","uint32"),("to","bytes32"),("amountLD","uint256"),
               ("minAmountLD","uint256"),("extraOptions","bytes"),("composeMsg","bytes"),
               ("oftCmd","bytes")])},
               {"name":"_fee","type":"tuple","components":_fields([
                   ("nativeFee","uint256"),("lzTokenFee","uint256")])},
               {"name":"_refundAddress","type":"address"}],
           "outputs":[{"name":"msgReceipt","type":"tuple","components":[
               {"name":"guid","type":"bytes32"},{"name":"nonce","type":"uint64"},
               {"name":"fee","type":"tuple","components":_fields([("nativeFee","uint256"),("lzTokenFee","uint256")])}]},
               {"name":"oftReceipt","type":"tuple","components":_fields([
                   ("amountSentLD","uint256"),("amountReceivedLD","uint256")])},
               {"name":"ticket","type":"tuple","components":_fields([
                   ("ticketId","uint72"),("passengerBytes","bytes")])}]}]


def _route(side,stable):
    if side not in ("BUY","SELL") or stable not in ("USDC","USDT"):
        raise ValueError("Unsupported staged AVAX route")
    src,dst=(1,43114) if side=="BUY" else (43114,1)
    return src,dst,CONFIG[src]["pools"][stable],CONFIG[src]["tokens"][stable]


def bridge_transaction(*,side,stable,wallet,amount_atomic,min_out_atomic,native_fee_atomic):
    src,dst,pool,_=_route(side,stable)
    if type(wallet) is not str or not ADDR.fullmatch(wallet):
        raise ValueError("Original owner wallet required")
    if (any(type(x) is not int or not 0<x<2**256 for x in
            (amount_atomic,min_out_atomic)) or min_out_atomic>amount_atomic
            or type(native_fee_atomic) is not int or not 0<=native_fee_atomic<2**256):
        raise ValueError("Exact staged bridge amounts required")
    recipient=bytes.fromhex(wallet[2:]).rjust(32,b"\x00")
    params=(CONFIG[dst]["eid"],recipient,amount_atomic,min_out_atomic,b"",b"",b"")
    data="0x"+(SEND_SELECTOR+encode(SEND_TYPES,[params,(native_fee_atomic,0),wallet.lower()])).hex()
    return {"from":wallet.lower(),"to":pool.lower(),"chainId":src,
            "data":data,"value":str(native_fee_atomic)}


def inspect_staged_bridge_transaction(tx,*,side,stable,wallet,scope,amount_atomic,
                                      min_out_atomic,native_fee_atomic):
    if (scope.get("asset")!="AVAX" or scope.get("stable")!=stable
            or scope.get("settlement_policy")!=STARGATE_STAGED_POLICY):
        raise ValueError("Original staged bridge permission required")
    src,_,_,_=_route(side,stable)
    if (amount_atomic>int(scope["buy_source_atomic_cap"])
            or native_fee_atomic>int(scope["bridge_fee_caps_atomic"][str(src)])):
        raise ValueError("Bridge exceeds selected owner capital or fee scope")
    expected=bridge_transaction(side=side,stable=stable,wallet=wallet,
        amount_atomic=amount_atomic,min_out_atomic=min_out_atomic,native_fee_atomic=native_fee_atomic)
    if tx!=expected:
        raise ValueError("Direct staged bridge calldata differs")
    return expected


def build_stargate_staged_policy(*,stable,wallet,owner_id,executor_id,
        buy_source_atomic_cap,sell_source_atomic_cap,bridge_fee_caps_atomic,expires_at,now):
    if (type(wallet) is not str or not ADDR.fullmatch(wallet)
        or any(type(x) is not str or not ID.fullmatch(x) for x in (owner_id,executor_id))
        or owner_id==executor_id or type(now) is not int or now<=0
        or type(expires_at) is not int or expires_at<=now
        or type(bridge_fee_caps_atomic) is not dict
        or set(bridge_fee_caps_atomic)!={"1","43114"}):
        raise ValueError("Original staged owner scope invalid")
    buy=_positive_atomic(buy_source_atomic_cap);sell=_positive_atomic(sell_source_atomic_cap)
    fees={key:_positive_atomic(v) for key,v in bridge_fee_caps_atomic.items()}
    wallet=wallet.lower();word="0x"+wallet[2:].rjust(64,"0")
    rules=[]
    def rule(name,conditions):
        rules.append({"name":"MiniNex AVAX "+name,"method":"eth_sendTransaction",
                      "action":"ALLOW","conditions":conditions})
    def approve(name,chain,token,spender,cap,reset):
        base=[_tx("chain_id","eq",str(chain)),_tx("to","eq",token),_expiry(expires_at),
              _tx("value","eq","0"),_call("function_name","eq","approve",ERC20_APPROVE_ABI),
              _call("approve.spender","eq",spender,ERC20_APPROVE_ABI),
              _call("approve.value","lte",cap,ERC20_APPROVE_ABI)]
        rule(name+" allowance",base+[_call("approve.value","gt","0",ERC20_APPROVE_ABI)])
        if reset:
            rule(name+" allowance reset",base+[_call("approve.value","eq","0",ERC20_APPROVE_ABI)])
    for side in ("BUY","SELL"):
        src,dst,pool,token=_route(side,stable)
        fee=fees[str(src)]
        fn="sendToken"
        call=lambda field,op,value:_call(fn+"."+field,op,value,SEND_ABI)
        name=side+" "+stable+" staged Stargate"
        rule(name,[
            _tx("chain_id","eq",str(src)),_tx("to","eq",pool),_expiry(expires_at),
            _tx("value","lte",fee),_call("function_name","eq",fn,SEND_ABI),
            call("_sendParam.dstEid","eq",str(CONFIG[dst]["eid"])),
            call("_sendParam.to","eq",word),call("_sendParam.amountLD","gt","0"),
            call("_sendParam.amountLD","lte",buy),call("_sendParam.minAmountLD","gt","0"),
            call("_sendParam.extraOptions","eq","0x"),call("_sendParam.composeMsg","eq","0x"),
            call("_sendParam.oftCmd","eq","0x"),call("_fee.nativeFee","lte",fee),
            call("_fee.lzTokenFee","eq","0"),call("_refundAddress","eq",wallet)])
        approve(name,src,token,pool,buy,stable=="USDT")
        local=CONFIG[43114]["tokens"][stable];wrapped=CROSS_COIN_CONTRACTS["AVAX"]
        source,target=(local,wrapped) if side=="BUY" else (wrapped,local)
        cap=buy if side=="BUY" else sell
        router=CHAINS["AVAX"].router.lower()
        name=side+" "+stable+" staged swap"
        abi=EXACT_INPUT_SINGLE_ABI
        call=lambda field,op,value:_call("exactInputSingle.params."+field,op,value,abi)
        rule(name,[_tx("chain_id","eq","43114"),_tx("to","eq",router),_expiry(expires_at),
            _tx("value","eq","0"),_call("function_name","eq","exactInputSingle",abi),
            call("tokenIn","eq",source),call("tokenOut","eq",target),call("recipient","eq",wallet),
            call("fee","in",["100","500","3000","10000"]),call("sqrtPriceLimitX96","eq","0"),
            call("amountIn","gt","0"),call("amountIn","lte",cap),call("amountOutMinimum","gt","0")])
        approve(name,43114,source,router,cap,side=="BUY" and stable=="USDT")
    return {"version":"1.0","chain_type":"ethereum","owner_id":owner_id,
            "name":"MiniNex-AVAX-staged-"+wallet[-8:],"rules":rules}
