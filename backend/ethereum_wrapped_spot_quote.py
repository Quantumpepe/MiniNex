"""Real Ethereum QuoterV2 exact-path quotes; no signature or submission.

The versioned owner's path is immutable. No indicative price, arbitrary
router, pool or token substitution is accepted. Missing liquidity fails
closed and cannot create a spend reservation.
"""
from decimal import Decimal,localcontext
import time
import re
import httpx
from eth_abi import encode,decode
from eth_utils import keccak
from fastapi import HTTPException

from .ethereum_wrapped_spot_policy import wrapped_spot_route,wrapped_spot_transaction
from .eth_router02_live_quote import _rpc,QUOTER_V2_MAINNET,_USD_BPS_SLIPPAGE
from .live_eth_quotes import _quantity
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses

_SELECTOR=keccak(text="quoteExactInput(bytes,uint256)")[:4]
_RESULT_TYPES=["uint256","uint160[]","uint32[]","uint256"]
# Canonical Uniswap V2 Router02 used for getAmountsOut only; execution
# still targets the shared SwapRouter02 and its V2 swap method.
_V2_QUOTE_ROUTER="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
_V2_QUOTE_SELECTOR=keccak(text="getAmountsOut(uint256,address[])")[:4]


def decode_v2_path_quote(raw,*,input_atomic,hops):
    if type(raw) is not str or not re.fullmatch(r"0x[0-9a-fA-F]+",raw) or len(raw)>2048:
        raise ValueError("Unverified V2 quote")
    try:
        data=bytes.fromhex(raw[2:])
        amounts=decode(["uint256[]"],data)[0]
        if (encode(["uint256[]"],[amounts])!=data
                or len(amounts)!=hops+1 or amounts[0]!=input_atomic
                or any(not 0<v<10**45 for v in amounts)):
            raise ValueError("Invalid V2 path amounts")
        return amounts[-1]
    except Exception as exc:
        raise ValueError("Invalid V2 quote ABI") from exc



def decode_exact_path_quote(raw,hops):
    if type(raw) is not str or not re.fullmatch(r"0x[0-9a-fA-F]+",raw) or len(raw)>4096:
        raise ValueError("Unverified exact-path V3 result")
    try:
        data=bytes.fromhex(raw[2:])
        amount,prices,ticks,gas=decode(_RESULT_TYPES,data)
        if (encode(_RESULT_TYPES,[amount,prices,ticks,gas])!=data
                or len(prices)!=hops or len(ticks)!=hops
                or any(v<=0 for v in prices) or not 0<amount<10**45 or not 0<gas<=5000000):
            raise ValueError("Unusable exact-path V3 quote")
        return amount,gas
    except Exception as exc:
        raise ValueError("Invalid exact-path V3 ABI") from exc


async def quote_owner_wrapped_spot(did,*,asset,side,stable,amount):
    route=wrapped_spot_route(asset,stable,side)
    _,units=_quantity(amount,route["source_decimals"])
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"].lower()
        if not re.fullmatch(r"0x[0-9a-f]{40}",wallet):
            raise ValueError("Original Ethereum wallet unavailable")
        if route.get("protocol")=="UNISWAP_V2":
            path=route["path"]
            calldata="0x"+(_V2_QUOTE_SELECTOR+encode(["uint256","address[]"],[int(units),path])).hex()
            quoter=_V2_QUOTE_ROUTER
        else:
            path=bytes.fromhex(route["path"][2:])
            calldata="0x"+(_SELECTOR+encode(["bytes","uint256"],[path,int(units)])).hex()
            quoter=QUOTER_V2_MAINNET
        async with httpx.AsyncClient(timeout=9,follow_redirects=False) as client:
            chain=await _rpc(client,method="eth_chainId",params=[],req_id=1)
            if chain!="0x1":
                raise ValueError("Wrong actual Ethereum network")
            raw=await _rpc(client,method="eth_call",req_id=2,
                params=[{"to":quoter,"data":calldata},"latest"])
            # Verify actual precision for the new bridged tokens before any
            # amount is used. Do not assume native precision matches ERC20.
            if asset in ("SOL","TRX"):
                token=route["destination_token"] if side=="BUY" else route["source_token"]
                decimals=await _rpc(client,method="eth_call",req_id=3,
                    params=[{"to":token,"data":"0x313ce567"},"latest"])
                precision=route["destination_decimals"] if side=="BUY" else route["source_decimals"]
                if type(decimals) is not str or len(decimals)!=66 or int(decimals,16)!=precision:
                    raise ValueError("Original Ethereum token precision differs")
        if route.get("protocol")=="UNISWAP_V2":
            expected=decode_v2_path_quote(raw,input_atomic=int(units),hops=len(path)-1)
            gas=None # V2 getAmountsOut does not provide an actual gas estimate.
        else:
            expected,gas=decode_exact_path_quote(raw,(len(path)-20)//23)
        minimum=max(1,expected*(10000-_USD_BPS_SLIPPAGE)//10000)
        tx=wrapped_spot_transaction(asset=asset,stable=stable,side=side,wallet=wallet,
                                   amount_atomic=int(units),min_out_atomic=minimum)
        with localcontext() as ctx:
            ctx.prec=100
            output=format(Decimal(expected).scaleb(-route["destination_decimals"]),"f")
            min_output=format(Decimal(minimum).scaleb(-route["destination_decimals"]),"f")
        return {"mode":"ETHEREUM_WRAPPED_SPOT_EXACT_QUOTE","asset":asset,"side":side,
                "stable":stable,"input_amount":amount,"network":"ethereum-mainnet","chain_id":1,
                "expected_output_amount":output,"minimum_output_amount":min_output,
                "estimated_swap_gas":gas,"transaction":tx,"transaction_expires_at":int(time.time())+40,
                "wallet_identity_verified":True,"onchain_quote_verified":True,
                "execution_enabled":False,"can_submit":False}
    except (httpx.HTTPError,ValueError,TypeError,KeyError,AttributeError,ArithmeticError):
        raise HTTPException(502,"No verified Ethereum liquidity quote for selected trade") from None
