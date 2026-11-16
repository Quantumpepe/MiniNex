"""Actual per-owner ETH wallet equity sourced from confirmed on-chain balances.

One Ethereum network slice, not global 11-coin portfolio, PnL or profit fee.
Includes native ETH and WETH exactly once (as distinct actual holdings) and
both USDC/USDT, with a fresh independently validated USD price reference.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
import re
import time

from fastapi import HTTPException

from .wallet_balances import get_wallet_balances
from .eth_router02_allowance import inspect_router02_approval
from .eth_privy_policy_plan import fetch_reference_prices,_parsed_price
from .storage import _safe_did

_ASSETS=("ETH","WETH","USDC","USDT")
_USD=Decimal("0.000001")
_ETH_ADDRESS=re.compile(r"^0x[0-9a-fA-F]{40}$")


def _exact_quantity(value:object)->Decimal:
    if type(value) is not str or len(value)>88:
        raise ValueError("Verified wallet quantity missing")
    try:
        amount=Decimal(value)
    except (InvalidOperation,ValueError):
        raise ValueError("Malformed wallet quantity") from None
    if not amount.is_finite() or amount<0 or amount>=Decimal("1e25"):
        raise ValueError("Wallet quantity invalid")
    return amount


def calculate_eth_wallet_equity(
    balances:dict,weth:dict,prices:dict,*,now:int,
)->dict:
    if (type(balances) is not dict
            or balances.get("wallet_ownership_verified") is not True
            or type(weth) is not dict
            or weth.get("wallet_identity_verified") is not True
            or weth.get("source_token")!="WETH"
            or weth.get("side")!="SELL"
            or type(now) is not int):
        raise ValueError("Only user-owned real ETH wallets are valued")
    # Both independent balance readers must identify the SAME Privy owner
    # address. A wallet change between queries cannot produce a combined
    # equity value from two different wallets.
    wallets=balances.get("wallets")
    owner=wallets.get("ETH") if type(wallets) is dict else None
    allowance_owner=weth.get("wallet_address")
    if (type(owner) is not str or not _ETH_ADDRESS.fullmatch(owner)
            or type(allowance_owner) is not str
            or not _ETH_ADDRESS.fullmatch(allowance_owner)
            or owner.lower()!=allowance_owner.lower()):
        raise ValueError("ETH and WETH wallet identity mismatch")
    holdings=balances.get("balances")
    if not isinstance(holdings,dict):
        raise ValueError("Wallet holdings unavailable")
    quantities={}
    for asset in ("ETH","USDC","USDT"):
        entry=holdings.get(asset)
        if type(entry) is not dict or entry.get("status")!="verified":
            raise ValueError("Confirmed Ethereum token balance unavailable")
        quantities[asset]=_exact_quantity(entry.get("balance"))
    quantities["WETH"]=_exact_quantity(weth.get("verified_source_balance"))
    rates={
        symbol:_parsed_price(prices,symbol,now)
        for symbol in ("ETH","USDC","USDT")
    }
    rows=[]
    total=Decimal(0)
    for asset in _ASSETS:
        price=rates["ETH" if asset=="WETH" else asset]
        value=quantities[asset]*price
        total+=value
        rows.append({
            "asset":asset,"network":"ethereum-mainnet",
            "native_units":format(quantities[asset],"f"),
            "reference_usd_price":format(price,"f"),
            "reference_value_usd":format(
                value.quantize(_USD,rounding=ROUND_HALF_UP),"f"),
            "onchain_balance_verified":True,
        })
    return {
        "asset":"ETH","network":"ethereum-mainnet",
        "source":"OWNER_VERIFIED_LIVE_ETH_WALLET",
        "includes_paper":False,"includes_other_chains":False,
        "real_wallet_balances_verified":True,
        "reference_prices_verified":True,
        "total_reference_value_usd":format(
            total.quantize(_USD,rounding=ROUND_HALF_UP),"f"),
        "balances":rows,
        "as_of_unix":now,
        "realized_profit_usd":None,
        "performance_fee_usd":None,
        "high_water_mark":None,
    }


async def read_owner_eth_live_wallet_value(did:str)->dict:
    did=_safe_did(did)
    balances,weth,prices=await asyncio.gather(
        get_wallet_balances(did),
        inspect_router02_approval(
            did,side="SELL",stable="USDC",
            amount="0.000000000000000001"),
        fetch_reference_prices())
    try:
        return calculate_eth_wallet_equity(
            balances,weth,prices,now=int(time.time()))
    except (ValueError,TypeError,KeyError,InvalidOperation,ArithmeticError):
        raise HTTPException(503,"Verified real ETH wallet equity unavailable") from None
