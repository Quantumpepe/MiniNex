"""Independently fetched USD spot reference for owner-selected EVM policy caps.

Policy creation uses ONLY the user's configured per-asset budget. This is
not a fixed MiniNex trading size limit. Runtime trades MUST get a fresh
on-chain Router02 quote, actual coin/stable balances, gas and allowance.
A spot price can NEVER authorize an actual swap or represent realized PnL.
"""
from __future__ import annotations

from decimal import Decimal,ROUND_FLOOR,InvalidOperation
import logging
import time

import httpx
from fastapi import HTTPException

from .shared_evm_swap_receipts import _pair

LOG=logging.getLogger("mininex.evm_policy_market_caps")
_PRICE_URL="https://api.coingecko.com/api/v3/simple/price"
_IDS={
    "BNB":"binancecoin",
    "POL":"polygon-ecosystem-token",
    "AVAX":"avalanche-2",
    "LINK":"chainlink",
    "UNI":"uniswap",
    "USDC":"usd-coin",
    "USDT":"tether",
}
_MAX_STALENESS_SEC=90


def _price(payload:object,asset:str,now:int)->Decimal:
    if type(payload) is not dict or asset not in _IDS:
        raise ValueError("Unknown EVM USD reference")
    entry=payload.get(_IDS[asset])
    if type(entry) is not dict:
        raise ValueError("Unavailable USD market reference")
    updated=entry.get("last_updated_at")
    raw=entry.get("usd")
    if (type(updated) is not int
            or updated>now+5 or now-updated>_MAX_STALENESS_SEC
            or type(raw) not in (int,float,str,Decimal)):
        raise ValueError("Missing fresh EVM USD reference")
    try:
        value=Decimal(str(raw))
    except (ValueError,InvalidOperation):
        raise ValueError("Invalid EVM USD rate") from None
    if not value.is_finite() or value<=0:
        raise ValueError("Invalid EVM USD price")
    if asset in ("USDC","USDT") and not Decimal("0.95")<=value<=Decimal("1.05"):
        raise ValueError("Stablecoin reference diverged from USD")
    return value


def derive_user_budget_evm_atomic_caps(
    *,asset:str,stable:str,budget_usd:int,snapshot:dict,now:int,
)->dict:
    """Convert exact user's coin budget to conservative token atomic units.

    No app-imposed max order, max daily turnover or per-asset default. The
    coin owner can change their configured budget; that changes its revision
    and requires a new signed policy, not silent reuse of an old grant.
    """
    if (type(budget_usd) is not int or budget_usd<=0
            or type(now) is not int or now<=0):
        raise ValueError("Exact user-selected USD budget required")
    pair=_pair(asset,stable,"BUY")
    native=_pair(asset,stable,"SELL")
    price_coin=_price(snapshot,asset,now)
    price_stable=_price(snapshot,stable,now)
    budget=Decimal(budget_usd)
    buy=int((budget/price_stable*
             (Decimal(10)**pair["source_decimals"])).to_integral_value(
                 rounding=ROUND_FLOOR))
    sell=int((budget/price_coin*
              (Decimal(10)**native["source_decimals"])).to_integral_value(
                  rounding=ROUND_FLOOR))
    if not 0<buy<10**45 or not 0<sell<10**45:
        raise ValueError("Budget not representable in approved atomic units")
    return {
        "buy_source_atomic_cap":str(buy),
        "sell_source_atomic_cap":str(sell),
        "native_wrap_atomic_cap":(
            str(sell) if asset in ("BNB","POL","AVAX") else None),
        "budget_usd_user_selected":budget_usd,
        "pricing_snapshot_unix":now,
        "price_feed":"COINGECKO_REFERENCE_ONLY",
        "swap_quote_ready":False,
        "privy_grant_created":False,
    }


async def fetch_evm_policy_reference_rates(asset:str,stable:str)->dict:
    pair=_pair(asset,stable,"BUY")
    symbols=(_IDS[asset],_IDS[stable])
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(7.0),follow_redirects=False,
        ) as client:
            response=await client.get(
                _PRICE_URL,params={
                    "ids":",".join(symbols),
                    "vs_currencies":"usd",
                    "include_last_updated_at":"true",
                },headers={"Accept":"application/json"})
            response.raise_for_status()
            if len(response.content)>24_000:
                raise ValueError("Oversized EVM policy reference quote")
            payload=response.json()
        now=int(time.time())
        _price(payload,asset,now)
        _price(payload,stable,now)
        return payload
    except (httpx.HTTPError,TypeError,ValueError):
        LOG.warning("EVM user policy market reference unavailable")
        raise HTTPException(503,"Fresh token USD references unavailable") from None
