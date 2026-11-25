"""Two explicit settlement legs for BNB, POL and ARB.

Local stablecoin is transit inventory of the original order, never wallet
cash or a separate strategy. This module compiles routes, not consent or
live authorization. Existing direct grants deliberately remain unchanged.
"""
from __future__ import annotations

from .ethereum_settlement_plan import ETHEREUM_STABLES, CROSS_COIN_CONTRACTS, _atomic
from .native_evm_v3_quote import CHAINS
from .ethereum_settlement_calldata import inspect_across_v4, DIAMOND, ACROSS_FACET
from .ethereum_settlement_policy_migration import FN, DIRECT_ABI
from .shared_evm_execution import EvmPair, unsigned_exact_input_single, ADDRESS, WALLET_ID
from .eth_privy_user_policy_blueprint import _tx, _call, _expiry
from .eth_swaprouter02_preflight import EXACT_INPUT_SINGLE_ABI
from .shared_evm_single_grant_policy import ERC20_APPROVE_ABI

STAGED_ACROSS_ASSETS=frozenset(("BNB","POL","ARB"))
STAGED_ACROSS_POLICY="ETHEREUM_ACROSS_STAGED_V1"


def staged_route(asset,side,stable):
    if asset not in STAGED_ACROSS_ASSETS or side not in ("BUY","SELL") or stable not in ETHEREUM_STABLES:
        raise ValueError("Unsupported staged Across settlement")
    chain=CHAINS[asset]
    buy=side=="BUY"
    eth=(1,ETHEREUM_STABLES[stable],6)
    transit=(chain.chain_id,chain.stable_contract.lower(),chain.stable_decimals)
    coin=(chain.chain_id,CROSS_COIN_CONTRACTS[asset],18)
    return {"asset":asset,"side":side,"stable":stable,"transit_symbol":chain.stable,
            "bridge_source":eth if buy else transit,
            "bridge_target":transit if buy else eth,
            "swap_source":transit if buy else coin,
            "swap_target":coin if buy else transit,
            "stages":("BRIDGE","SWAP") if buy else ("SWAP","BRIDGE"),
            "settlement_chain_id":1}


def bridge_terms(asset,side,stable,wallet,amount,minimum,issued_at):
    route=staged_route(asset,side,stable)
    if type(wallet) is not str or not ADDRESS.fullmatch(wallet):
        raise ValueError("Original owner wallet required")
    if type(issued_at) is not int or issued_at<=0:
        raise ValueError("Exact quote time required")
    source,target=route["bridge_source"],route["bridge_target"]
    return {"asset":asset,"side":side,"stable":stable,"wallet_address":wallet.lower(),
            "source_chain_id":source[0],"destination_chain_id":target[0],
            "source_token":source[1],"destination_token":target[1],
            "input_atomic":str(_atomic(amount,positive=True)),
            "minimum_output_atomic":str(_atomic(minimum,positive=True)),
            "issued_at":issued_at,"expires_at":issued_at+30}


def inspect_staged_bridge(terms,tx,*,now):
    expected=bridge_terms(terms["asset"],terms["side"],terms["stable"],
        terms["wallet_address"],terms["input_atomic"],terms["minimum_output_atomic"],terms["issued_at"])
    if any(terms.get(k)!=v for k,v in expected.items()) or not expected["issued_at"]<=now<expected["expires_at"]:
        raise ValueError("Original direct stablecoin stage differs or expired")
    reviewed=inspect_across_v4(tx,terms=expected,now=now)
    if reviewed["source_swap"] or reviewed["destination_swap"] or reviewed["transaction"]["value"]!="0":
        raise ValueError("Only a direct owner-to-owner stablecoin bridge is allowed")
    return {**reviewed,"provider":"ACROSS_V4","facet":ACROSS_FACET,"native_bridge_fee_atomic":0}


def staged_local_transaction(asset,side,stable,wallet,amount,minimum,fee):
    route=staged_route(asset,side,stable)
    chain=CHAINS[asset]
    source,target=route["swap_source"],route["swap_target"]
    return unsigned_exact_input_single(pair=EvmPair(asset,chain.chain_id,chain.network,
        source[1],target[1],chain.router.lower(),fee),wallet=wallet.lower(),
        amount_in=_atomic(amount,positive=True),min_out=_atomic(minimum,positive=True))


def build_staged_across_policy(*,asset,stable,wallet,owner_id,executor_id,
        eth_buy_atomic_cap,coin_sell_atomic_cap,transit_atomic_cap,expires_at,now):
    """Review-only proposal: finite distinct atomics per token precision.

    Requires original owner consent, combined-policy integration, remote
    verification and an atomic original-order journal before any signing.
    """
    if (type(wallet) is not str or not ADDRESS.fullmatch(wallet)
            or any(type(x) is not str or not WALLET_ID.fullmatch(x) for x in (owner_id,executor_id))
            or owner_id==executor_id or type(now) is not int or now<=0
            or type(expires_at) is not int or expires_at<=now):
        raise ValueError("Original scoped owner permission required")
    wallet=wallet.lower()
    word=lambda address:"0x"+address[2:].rjust(64,"0")
    buy_cap=str(_atomic(eth_buy_atomic_cap,positive=True))
    sell_cap=str(_atomic(coin_sell_atomic_cap,positive=True))
    transit_cap=str(_atomic(transit_atomic_cap,positive=True))
    chain=CHAINS[asset] if asset in STAGED_ACROSS_ASSETS else None
    if chain is None:
        raise ValueError("Unsupported staged asset")
    rules=[]
    def rule(name,conditions):
        rules.append({"name":f"MiniNex {asset} {name}","method":"eth_sendTransaction",
                      "action":"ALLOW","conditions":conditions})
    def base(chain_id,contract):
        return [_tx("chain_id","eq",str(chain_id)),_tx("to","eq",contract),
                _tx("value","eq","0"),_expiry(expires_at)]
    def approval(name,source,spender,cap,reset):
        conditions=base(source[0],source[1])+[
            _call("function_name","eq","approve",ERC20_APPROVE_ABI),
            _call("approve.spender","eq",spender,ERC20_APPROVE_ABI),
            _call("approve.value","lte",cap,ERC20_APPROVE_ABI)]
        rule(name+" allowance",conditions+[_call("approve.value","gt","0",ERC20_APPROVE_ABI)])
        if reset:
            rule(name+" reset",conditions+[_call("approve.value","eq","0",ERC20_APPROVE_ABI)])
    for side in ("BUY","SELL"):
        route=staged_route(asset,side,stable)
        src,dst=route["bridge_source"],route["bridge_target"]
        buy=side=="BUY"
        cap=buy_cap if buy else transit_cap
        call=lambda field,op,value:_call(FN+"."+field,op,value,DIRECT_ABI)
        name=f"{side} {stable} staged direct bridge"
        rule(name,base(src[0],DIAMOND)+[_call("function_name","eq",FN,DIRECT_ABI),
            call("_bridgeData.sendingAssetId","eq",src[1]),
            call("_bridgeData.receiver","eq",wallet),
            call("_bridgeData.destinationChainId","eq",str(dst[0])),
            call("_bridgeData.minAmount","gt","0"),
            call("_bridgeData.minAmount","lte",cap),
            call("_bridgeData.hasSourceSwaps","eq",False),
            call("_bridgeData.hasDestinationCall","eq",False),
            call("_acrossData.receiverAddress","eq",word(wallet)),
            call("_acrossData.refundAddress","eq",word(wallet)),
            call("_acrossData.sendingAssetId","eq",word(src[1])),
            call("_acrossData.receivingAssetId","eq",word(dst[1])),
            call("_acrossData.outputAmount","gt","0"),
            call("_acrossData.message","eq","0x")])
        approval(name,src,DIAMOND,cap,(stable if buy else chain.stable)=="USDT")
        src,dst=route["swap_source"],route["swap_target"]
        cap=transit_cap if buy else sell_cap
        router=chain.router.lower()
        name=f"{side} {stable} staged local swap"
        abi=EXACT_INPUT_SINGLE_ABI
        call=lambda field,op,value:_call("exactInputSingle.params."+field,op,value,abi)
        rule(name,base(src[0],router)+[_call("function_name","eq","exactInputSingle",abi),
            call("tokenIn","eq",src[1]),call("tokenOut","eq",dst[1]),call("recipient","eq",wallet),
            call("fee","in",["100","500","3000","10000"]),call("sqrtPriceLimitX96","eq","0"),
            call("amountIn","gt","0"),call("amountIn","lte",cap),call("amountOutMinimum","gt","0")])
        approval(name,src,router,cap,buy and chain.stable=="USDT")
    return {"settlement_policy":STAGED_ACROSS_POLICY,
            "policy":{"version":"1.0","chain_type":"ethereum","owner_id":owner_id,
                      "name":f"MiniNex {asset} staged {wallet[-8:]}","rules":rules},
            "remote_policy_verified":False,"requires_owner_consent":True,"can_submit":False}
