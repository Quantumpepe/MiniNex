"""BNB, POL and AVAX owner-bound same-chain Uniswap V3 routes.

Shared EVM exactInputSingle encoder and Privy wallet address; distinct
official mainnet routers, quoters, wrapped-native and settlement contracts.
These are real read-only quotes and optional OWNER-signable calldata,
not automatic execution: native wrap/unwrap, approval, receipts and fee
settlement remain separate before end-to-end LIVE.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib
import os
import re
import time

import httpx
from fastapi import HTTPException

from .eth_router02_live_quote import _quote_calldata, _decode_quote
from .live_eth_quotes import _quantity
from .multicoin_evm_balances import _RPC
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses
from .shared_evm_execution import EvmPair, unsigned_exact_input_single, chain_for_asset

_ADDR=re.compile(r"^0x[0-9a-fA-F]{40}$")
_WORD=re.compile(r"^0x[0-9a-fA-F]{64}$")
_SMALL_HEX=re.compile(r"^0x[0-9a-fA-F]{1,64}$")
_FEES=(500,3000)
_SLIPPAGE_BPS=50

@dataclass(frozen=True)
class ChainRoute:
    chain_id:int
    network:str
    wrapped:str
    router:str
    quoter:str
    stable:str
    stable_contract:str
    stable_decimals:int
    deployment_reference:str


# Uniswap's own per-chain v3 deployment references; native Circle USDC
# on Polygon/Avalanche, not bridged USDC.e. BNB USDT is a BEP20 token
# with 18 decimals, not Ethereum USDT's 6 decimals.
CHAINS={
    "BNB":ChainRoute(56,"bsc-mainnet",
        "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2",
        "0x78D78E420Da98ad378D7799bE8f4AF69033EB077",
        "USDT","0x55d398326f99059fF775485246999027B3197955",18,
        "https://developers.uniswap.org/docs/protocols/v3/deployments/v3-bnb-deployments"),
    "POL":ChainRoute(137,"polygon-pos-mainnet",
        "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
        "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "USDC","0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",6,
        "https://developers.uniswap.org/docs/protocols/v3/deployments/v3-polygon-deployments"),
    "ARB":ChainRoute(42161,"arbitrum-one-mainnet",
        "0x912CE59144191C1204E64559FE8253a0e49E6548",
        "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "USDC","0xaf88d065e77c8cC2239327C5EDb3A432268e5831",6,
        "https://developers.uniswap.org/docs/protocols/v3/deployments/v3-arbitrum-deployments"),
    "AVAX":ChainRoute(43114,"avalanche-c-mainnet",
        "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
        "0xbb00FF08d01D300023C629E8fFfFcb65A5a578cE",
        "0xbe0F5544EC67e9B3b2D979aaA43f18Fd87E6257F",
        "USDC","0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",6,
        "https://developers.uniswap.org/docs/protocols/v3/deployments/v3-avalanche-deployments"),
}


def _route(asset:str,stable:str)->ChainRoute:
    if type(asset) is not str or asset not in CHAINS:
        raise HTTPException(404,"Unsupported native EVM trading asset")
    route=CHAINS[asset]
    # ARB is an ERC-20 token, NOT Arbitrum gas. It trades against native
    # Circle USDC or genuine Arbitrum USDT0; ETH remains the gas currency.
    if asset=="ARB" and stable=="USDT":
        route=replace(route,stable="USDT",
            stable_contract="0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9")
    if stable!=route.stable or type(stable) is not str:
        raise HTTPException(422,"Stablecoin contract not verified for this EVM route")
    if chain_for_asset(asset)!=(route.chain_id,route.network):
        raise ValueError("EVM route registry mismatch")
    return route


def build_native_v3_transaction(*,asset:str,stable:str,side:str,
                                wallet:str,amount_in:int,min_out:int,fee:int)->dict:
    route=_route(asset,stable)
    if side not in ("BUY","SELL"):
        raise ValueError("Invalid EVM trade direction")
    return unsigned_exact_input_single(
        pair=EvmPair(
            asset,route.chain_id,route.network,
            route.stable_contract if side=="BUY" else route.wrapped,
            route.wrapped if side=="BUY" else route.stable_contract,
            route.router,fee,
        ),
        wallet=wallet,amount_in=amount_in,min_out=min_out,
    )


async def _rpc(client:httpx.AsyncClient,url:str,method:str,
               params:list,rid:int)->str:
    response=await client.post(
        url,json={"jsonrpc":"2.0","id":rid,"method":method,"params":params},
        headers={"Accept":"application/json","Content-Type":"application/json"},
    )
    response.raise_for_status()
    if len(response.content)>16_384:
        raise ValueError("Oversized EVM mainnet RPC answer")
    data=response.json()
    if (type(data) is not dict or data.get("jsonrpc")!="2.0"
            or data.get("id")!=rid or "error" in data
            or type(data.get("result")) is not str):
        raise ValueError("Untrusted EVM JSON RPC response")
    return data["result"]


def _decode_quantity(value:str,*,word:bool=False)->int:
    if type(value) is not str or not (
            _WORD.fullmatch(value) if word else _SMALL_HEX.fullmatch(value)):
        raise ValueError("Invalid EVM on-chain quantity")
    return int(value,16)


async def _token_balance(client,url,token,wallet,rid)->int:
    owner=wallet[2:].lower().rjust(64,"0")
    response=await _rpc(client,url,"eth_call",[
        {"to":token,"data":"0x70a08231"+owner},"latest"
    ],rid)
    return _decode_quantity(response,word=True)


async def quote_native_evm_trade(
    did:str,*,asset:str,side:str,stable:str,amount:str,
    include_user_transaction:bool=False,
)->dict:
    route=_route(asset,stable)
    if side not in ("BUY","SELL"):
        raise HTTPException(422,"Unsupported native EVM trade side")
    requested,atomic=_quantity(amount,route.stable_decimals if side=="BUY" else 18)
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if type(wallet) is not str or not _ADDR.fullmatch(wallet):
            raise ValueError("No owned Privy EVM wallet")
    except (ValueError,KeyError,TypeError):
        raise HTTPException(503,"User owned EVM wallet unavailable") from None
    chain,network,fallback,env=_RPC[
        "ARB_TOKENS" if asset=="ARB" else asset]
    if chain!=route.chain_id or network!=route.network:
        raise HTTPException(503,"EVM RPC network configuration mismatch")
    rpc_url=os.getenv(env,"").strip() or fallback
    if not rpc_url.startswith("https://") or any(ch in rpc_url for ch in " \n\t#@"):
        raise HTTPException(503,"Secure EVM network RPC required")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            observed=_decode_quantity(
                await _rpc(client,rpc_url,"eth_chainId",[],1))
            if observed!=route.chain_id:
                raise ValueError("EVM RPC answered with wrong chain")
            # Both contracts must be deployed, not user-provided addresses.
            for rid,contract in enumerate(
                (route.router,route.quoter,route.stable_contract,route.wrapped),
                start=2,
            ):
                code=await _rpc(client,rpc_url,"eth_getCode",[contract,"latest"],rid)
                if (type(code) is not str or not re.fullmatch(
                        r"0x(?:[0-9a-fA-F]{2})+",code) or code=="0x"):
                    raise ValueError("EVM verified route contract not deployed")
            precision=await _rpc(client,rpc_url,"eth_call",[
                {"to":route.stable_contract,"data":"0x313ce567"},"latest"
            ],6)
            if _decode_quantity(precision,word=True)!=route.stable_decimals:
                raise ValueError("Wrong settlement token decimals on this chain")
            native=_decode_quantity(
                await _rpc(client,rpc_url,"eth_getBalance",[wallet,"latest"],7))
            if native<=0:
                raise HTTPException(409,"Insufficient native coin for network gas")
            source=route.stable_contract if side=="BUY" else route.wrapped
            balance=await _token_balance(client,rpc_url,source,wallet,8)
            if balance<int(atomic):
                raise HTTPException(
                    409,"Insufficient verified ERC20 source balance; native wrap may be needed")
            token_in=source
            token_out=route.wrapped if side=="BUY" else route.stable_contract
            results=await asyncio.gather(*(
                _rpc(client,rpc_url,"eth_call",[
                    {"to":route.quoter,"data":_quote_calldata(
                        token_in=token_in,token_out=token_out,
                        amount=int(atomic),fee=fee,
                    )},"latest"
                ],fee) for fee in _FEES
            ),return_exceptions=True)
        candidates=[]
        for fee,raw in zip(_FEES,results):
            if isinstance(raw,str):
                try:
                    output,gas=_decode_quote(raw)
                    candidates.append((fee,output,gas))
                except (TypeError,ValueError):
                    continue
        if not candidates:
            raise ValueError("No verified Uniswap V3 pool quote")
        fee,expected,gas=max(candidates,key=lambda t:(t[1],-t[2],-t[0]))
        minimum=max(1,expected*(10_000-_SLIPPAGE_BPS)//10_000)
        tx=build_native_v3_transaction(
            asset=asset,stable=stable,side=side,wallet=wallet,
            amount_in=int(atomic),min_out=minimum,fee=fee,
        )
    except (httpx.HTTPError,TypeError,ValueError,OverflowError):
        raise HTTPException(502,"Actual mainnet EVM V3 quote unavailable") from None

    return {
        **({
            "transaction":{
                "from":wallet,"to":tx["to"],"chainId":route.chain_id,
                "value":"0x0","data":tx["data"],
            },
            "transaction_expires_at":int(time.time())+15,
            "requires_owner_wallet_signature":True,
        } if include_user_transaction else {}),
        "mode":("SHARED_EVM_ERC20_V3_QUOTE" if asset=="ARB"
                else "SHARED_NATIVE_EVM_V3_QUOTE"),
        "asset":asset,"network":network,"chain_id":route.chain_id,
        "side":side,"stable":stable,"input_amount":amount,
        "expected_output_amount":format(
            Decimal(expected)/Decimal(10**(18 if side=="BUY" else route.stable_decimals)),
            "f",
        ),
        "minimum_output_amount":format(
            Decimal(minimum)/Decimal(10**(18 if side=="BUY" else route.stable_decimals)),
            "f",
        ),
        "quote_fee_tier":fee,"quote_gas_estimate":gas,
        "router":route.router,
        "unsigned_swap_hash":hashlib.sha256(bytes.fromhex(tx["data"][2:])).hexdigest(),
        "native_wrap_unwrap_not_yet_settled":asset!="ARB",
        "source_token_allowance_not_checked":True,
        "wallet_identity_verified":True,
        "wallet_funds_verified":True,
        "onchain_quote_verified":True,
        "execution_enabled":False,"can_submit":False,
        "wallet_signed":False,"funds_transferred":False,
    }
