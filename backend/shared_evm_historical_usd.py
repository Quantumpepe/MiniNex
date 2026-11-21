"""Historical USD price proof for already mined MiniNex EVM transactions.

A current spot quote is NEVER a valid historical gas/cost basis. This
small reader accepts only CoinGecko's near-execution 5-minute observation
within the supported recent public history. Older/missing data fails closed
rather than silently assuming stablecoin parity or zero network costs.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import logging
import time

import httpx
from fastapi import HTTPException

from .coingecko_provider import coingecko_request_settings, COIN_IDS

# Keep the historical EVM-asset allowlist; resolve IDs centrally.
_IDS={asset: COIN_IDS[asset] for asset in (
    "ETH", "BNB", "POL", "AVAX", "LINK", "UNI", "ARB", "USDC", "USDT"
)}
LOG=logging.getLogger("mininex.real_historical_usd")
_BASE="https://api.coingecko.com/api/v3/coins"
_MAX_QUOTE_GAP=180
_MIN_HISTORY_TIME=86400-3600


def pick_historical_usd_observation(
    *,asset:str,block_unix:int,now:int,payload:object,
)->dict:
    """Only an independently fetched close timestamp near the actual block."""
    if (type(asset) is not str or asset not in _IDS
            or type(block_unix) is not int or type(now) is not int
            or block_unix<=0 or block_unix>now+10
            or now-block_unix>_MIN_HISTORY_TIME
            or type(payload) is not dict
            or type(payload.get("prices")) is not list):
        raise ValueError("No near-execution historical USD price evidence")
    candidates=[]
    for point in payload["prices"]:
        if type(point) not in (list,tuple) or len(point)!=2:
            raise ValueError("Invalid historical rate series shape")
        when,raw=point
        if (type(when) not in (int,Decimal)
                or type(raw) not in (int,float,str,Decimal)
                or when<=0):
            raise ValueError("Invalid historical rate value")
        try:
            price=Decimal(str(raw))
        except (InvalidOperation,ValueError,TypeError):
            raise ValueError("Invalid historical USD rate") from None
        if not price.is_finite() or price<=0 or price>=Decimal("1e12"):
            raise ValueError("Invalid historical USD rate")
        second=int(Decimal(when)/Decimal(1000))
        gap=abs(second-block_unix)
        if gap<=_MAX_QUOTE_GAP:
            candidates.append((gap,second,price))
    if not candidates:
        raise ValueError("Provider has no real price near execution block")
    _,timestamp,price=min(candidates,key=lambda x:(x[0],x[1]))
    if asset in ("USDT","USDC") and not Decimal("0.95")<=price<=Decimal("1.05"):
        raise ValueError("Stablecoin depeg requires independent fee review")
    return {
        "mode":"HISTORICAL_EXECUTION_USD_REFERENCE_ONLY",
        "asset":asset,"provider":"COINGECKO_MARKET_CHART_RANGE",
        "provider_asset_id":_IDS[asset],
        "execution_block_unix":block_unix,
        "rate_observed_unix":timestamp,
        "rate_age_seconds":abs(timestamp-block_unix),
        "usd_price":format(price,"f"),
        "onchain_execution_finality_verified_by_this_rate":False,
        "fifo_cost_basis_proved_by_this_rate":False,
    }


async def fetch_recent_historical_usd_observation(
    *,asset:str,block_unix:int,client:httpx.AsyncClient,
    now:int|None=None,
)->dict:
    """No arbitrary client-selected markets or historical price placeholders."""
    current=int(time.time()) if now is None else now
    if (type(client) is not httpx.AsyncClient
            or asset not in _IDS
            or type(block_unix) is not int
            or block_unix>current+10
            or current-block_unix>_MIN_HISTORY_TIME):
        raise ValueError("Recent exact historical rate unavailable")
    start=max(current-86400,block_unix-3600)
    end=min(current,block_unix+3600)
    if end<=start:
        raise ValueError("Historical market range unavailable")
    endpoint, auth_headers, _ = coingecko_request_settings(
        f"/coins/{_IDS[asset]}/market_chart/range")
    try:
        res=await client.get(
            endpoint,
            params={"vs_currency":"usd","from":start,"to":end},
            headers={"Accept":"application/json", **auth_headers})
        res.raise_for_status()
        if len(res.content)>120000:
            raise ValueError("Oversized historical provider data")
        payload=json.loads(res.content.decode("utf-8"),parse_float=Decimal)
        return pick_historical_usd_observation(
            asset=asset,block_unix=block_unix,now=current,payload=payload)
    except (httpx.HTTPError,UnicodeDecodeError,ValueError,TypeError):
        LOG.warning("Original historical USD reference missing")
        raise HTTPException(503,"Cannot value execution costs at historical rate") from None
