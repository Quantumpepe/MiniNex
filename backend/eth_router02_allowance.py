"""Real Ethereum on-chain source balance and SwapRouter02 ERC20 allowance.

Read-only, authenticated wallet-bound preparation for the single-hop Uniswap
V3 path that matches the proposed Privy exactInputSingle policy.

BUY checks actual USDC/USDT allowance and balance.
SELL checks actual WETH allowance and balance (native ETH is NOT WETH).
Also reads native ETH gas reserves. Does not create ERC20 approve tx,
spend funds, sign, delegate, create Privy policies or trigger AUTO.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
import re

import httpx
from fastapi import HTTPException

from .eth_router02_live_quote import _rpc
from .eth_swaprouter02_preflight import SWAPROUTER02_MAINNET, WETH_MAINNET
from .live_eth_quotes import STABLES, ETH_GAS_RESERVE, _quantity
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import ETH_RPC, verified_addresses

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_UINT256 = re.compile(r"^0x[0-9a-fA-F]{64}$")
_QUANTITY = re.compile(r"^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$")
_UINT_MAX=2**256-1
_ALLOWANCE="0xdd62ed3e"
_BALANCE="0x70a08231"


def _abi_word(address: str) -> str:
    if type(address) is not str or not _ADDR.fullmatch(address):
        raise ValueError("Invalid verified Ethereum address")
    return address[2:].lower().rjust(64,"0")


def _decode_uint(raw: object) -> int:
    if type(raw) is not str or not _UINT256.fullmatch(raw):
        raise ValueError("ERC20 returned invalid ABI uint256")
    return int(raw,16)


def _decode_native(raw: object) -> int:
    if type(raw) is not str or not _QUANTITY.fullmatch(raw):
        raise ValueError("Native Ethereum balance unavailable")
    result=int(raw,16)
    if not 0<=result<=_UINT_MAX:
        raise ValueError("Invalid gas balance")
    return result


async def _contract_call(client: httpx.AsyncClient, *, token: str,
                         data: str, req_id: int) -> int:
    raw=await _rpc(client,method="eth_call",req_id=req_id,
                   params=[{"to":token,"data":data},"latest"])
    return _decode_uint(raw)


async def inspect_router02_approval(
    did: str, *, side: str, stable: str, amount: str,
) -> dict:
    if side not in ("BUY","SELL") or stable not in STABLES:
        raise HTTPException(422,"Unsupported Ethereum input for policy review")
    requested,units=_quantity(amount,6 if side=="BUY" else 18)
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if type(wallet) is not str or not _ADDR.fullmatch(wallet):
            raise ValueError("Unique owner wallet not found")
    except (ValueError,KeyError,TypeError):
        raise HTTPException(503,"Verified Privy ETH wallet required") from None
    token=STABLES[stable] if side=="BUY" else WETH_MAINNET
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            chain=await _rpc(client,method="eth_chainId",params=[],req_id=1)
            if chain!="0x1":
                raise ValueError("RPC network is not Ethereum mainnet")
            account=_abi_word(wallet)
            allowance_data=_ALLOWANCE+account+_abi_word(SWAPROUTER02_MAINNET)
            balance_data=_BALANCE+account
            approval,holding,gas=await asyncio.gather(
                _contract_call(client,token=token,
                               data=allowance_data,req_id=2),
                _contract_call(client,token=token,
                               data=balance_data,req_id=3),
                _rpc(client,method="eth_getBalance",
                     params=[wallet,"latest"],req_id=4),
            )
        native_wei=_decode_native(gas)
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"Router02 wallet allowances unavailable") from None
    decimals=6 if side=="BUY" else 18
    divisor=Decimal(10)**decimals
    needed=int(units)
    return {
        "mode":"ETH_ROUTER02_REAL_ALLOWANCE_READ_ONLY",
        "asset":"ETH","network":"ethereum-mainnet",
        "side":side,"stable":stable,"source_token":stable if side=="BUY" else "WETH",
        "source_token_network":"ethereum-mainnet",
        "spending_router":SWAPROUTER02_MAINNET,
        "verified_source_balance":format(Decimal(holding)/divisor,"f"),
        "verified_source_balance_atomic":str(holding),
        "existing_router_allowance":format(Decimal(approval)/divisor,"f"),
        "existing_router_allowance_atomic":str(approval),
        "source_funds_sufficient":holding>=needed,
        "router_allowance_sufficient":approval>=needed,
        "manual_token_approval_would_be_required":approval<needed,
        "native_eth_gas_balance":format(Decimal(native_wei)/Decimal(10**18),"f"),
        "conservative_native_gas_reserve_met":(
            Decimal(native_wei)/Decimal(10**18)>ETH_GAS_RESERVE),
        "input_amount":amount,
        "wallet_identity_verified":True,
        "signed_erc20_approval_created":False,
        "privy_grant_verified":False,
        "native_eth_unwrap_or_wrap_settled":False,
        "allowance_not_spend_authority":True,
        "execution_enabled":False,"can_submit":False,
    }
