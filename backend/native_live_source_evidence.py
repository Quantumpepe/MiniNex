"""Verified native LIVE BUY source evidence, no simulated funding or send.

Runs one targeted native address balance RPC and one existing ETH wallet
balance read; stale/missing explorer data is never coerced to zero. A shared
real USD reference is used only for owner-budget sizing, never to authorize
the THORChain swap. The actual coordinator still requires a fresh private
THORChain quote and immutable Privy grant.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal,InvalidOperation
import logging
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .coingecko_provider import COIN_IDS,coingecko_request_settings
from .native_coin_balances import (
    CHAINS,_read_one,verified_native_addresses,
)
from .native_live_budget import (
    _NATIVE,exact_owner_native_buy_amount,
)
from .privy_wallet_verification import fetch_privy_user
from .storage import _db_url,_ensure_schema,_safe_did
from .wallet_balances import get_wallet_balances

LOG=logging.getLogger("mininex.native_live_source")
_PRICE_TTL=180
_REF_CACHE:dict|None=None
_REF_LOCK=asyncio.Lock()
_SOURCES=frozenset(("USDC","USDT"))


def _native_rate(data:object,asset:str,now:int)->Decimal:
    """Only actual recently timestamped USD oracle values, no 1 USD guess."""
    if (asset not in _NATIVE and asset not in _SOURCES
            or type(now) is not int or now<=0
            or type(data) is not dict):
        raise ValueError("Invalid original native USD source")
    rate=data.get(COIN_IDS[asset])
    if (type(rate) is not dict or type(rate.get("last_updated_at")) is not int
            or rate["last_updated_at"]>now+5
            or now-rate["last_updated_at"]>_PRICE_TTL):
        raise ValueError("Native USD reference not independently fresh")
    raw=rate.get("usd")
    if type(raw) not in (int,float,str,Decimal) or type(raw) is bool:
        raise ValueError("Malformed native USD reference")
    try:
        price=Decimal(str(raw))
    except (InvalidOperation,ValueError):
        raise ValueError("Invalid original native USD quote") from None
    if (not price.is_finite() or price<=0
            or asset in _SOURCES and
            not Decimal("0.95")<=price<=Decimal("1.05")):
        raise ValueError("Missing reliable native USD quote")
    return price


async def fresh_native_usd_prices(asset:str)->dict[str,Decimal]:
    """One shared CoinGecko reference read for native coin and stable sources.

    This is reference pricing for a capital calculation only; never a
    substitute for a fresh THORChain destination/fee/amount quote.
    """
    if asset not in _NATIVE:
        raise HTTPException(422,"Unknown original native live price asset")
    endpoint,headers,interval=coingecko_request_settings("/simple/price")
    global _REF_CACHE
    async with _REF_LOCK:
        now=int(time.time())
        cache=_REF_CACHE
        if (type(cache) is dict and cache.get("endpoint")==endpoint
                and type(cache.get("read_at")) is int
                and 0<=now-cache["read_at"]<=interval):
            try:
                return {coin:_native_rate(cache["data"],coin,now)
                        for coin in (asset,"USDC","USDT")}
            except (ValueError,TypeError,KeyError):
                pass
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(9.0),follow_redirects=False,
            ) as client:
                response=await client.get(
                    endpoint,params={
                        "ids":",".join(dict.fromkeys(COIN_IDS[x] for x in
                                                   (*_NATIVE.keys(),"USDC","USDT"))),
                        "vs_currencies":"usd",
                        "include_last_updated_at":"true",
                    },headers={"Accept":"application/json",**headers})
                response.raise_for_status()
                if len(response.content)>35_000:
                    raise ValueError("Oversized native USD reference")
                data=response.json()
            rates={coin:_native_rate(data,coin,now)
                   for coin in (asset,"USDC","USDT")}
            _REF_CACHE={"endpoint":endpoint,"read_at":now,"data":data}
            return rates
        except (httpx.HTTPError,ValueError,KeyError,TypeError):
            LOG.warning("Original native real-price evidence unavailable")
            raise HTTPException(503,"Current native USD reference unavailable") from None


def _pending_owner_native_buys(did:str,asset:str)->list[dict]:
    """Read only this owner's original unresolved native BUY source claims."""
    did=_safe_did(did)
    if asset not in _NATIVE:
        raise ValueError("Invalid native pending-amount asset")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT asset,side,stable,status,input_amount
                   FROM mininex_live_native_order_intents
                   WHERE privy_did=%s AND asset=%s AND side='BUY'
                     AND ((status='PREPARED' AND quote_expires_at>now())
                          OR status IN ('SENDING','SUBMITTED',
                               'RECONCILIATION_REQUIRED','SOURCE_FINALIZED'))""",
                (did,asset))
            return list(cur.fetchall())


def _pending_usd(rows:list[dict],asset:str,prices:dict)->Decimal:
    """Pending USDC and USDT both count, even after settings change."""
    if type(rows) is not list or asset not in _NATIVE:
        raise ValueError("No original native pending evidence")
    total=Decimal(0)
    for row in rows:
        if (type(row) is not dict or row.get("asset")!=asset
                or row.get("side")!="BUY"
                or row.get("stable") not in _NATIVE[asset][2]
                or row.get("status") not in (
                    "PREPARED","SENDING","SUBMITTED",
                    "RECONCILIATION_REQUIRED","SOURCE_FINALIZED")):
            raise ValueError("Untrusted original native BUY budget entry")
        value=row.get("input_amount")
        if type(value) not in (str,int,Decimal):
            raise ValueError("Non-exact original native stable input")
        amount=Decimal(str(value))
        if not amount.is_finite() or amount<=0:
            raise ValueError("Invalid unclosed native USD investment")
        total+=amount*prices[row["stable"]]
    return total


async def verified_owner_native_buy_amount(
    did:str,*,asset:str,stable:str,budget_usd:int,
    client:httpx.AsyncClient,
)->str|None:
    """One coin's actual available balance, holdings, pending and USD rates."""
    did=_safe_did(did)
    if (asset not in _NATIVE or stable not in _NATIVE[asset][2]
            or type(budget_usd) is not int or budget_usd<=0
            or type(client) is not httpx.AsyncClient):
        raise HTTPException(409,"Invalid owner-selected native LIVE allocation")
    user=await fetch_privy_user(did)
    native=verified_native_addresses(user,did)
    address=native.get(asset)
    if not address:
        raise HTTPException(409,"Owner's unique native receiving wallet missing")
    try:
        eth_result,native_balance,prices,pending=await asyncio.gather(
            get_wallet_balances(did),
            _read_one(client,asset,address),
            fresh_native_usd_prices(asset),
            asyncio.to_thread(_pending_owner_native_buys,did,asset),
        )
        source=eth_result.get("balances",{}).get(stable,{})
        if (eth_result.get("wallet_ownership_verified") is not True
                or type(source) is not dict
                or eth_result.get("wallets",{}).get("ETH") is None):
            raise ValueError("Original ETH funding owner unavailable")
        # A single account must own both wallets. No secondary imported key.
        result=exact_owner_native_buy_amount(
            asset=asset,stable=stable,budget_usd=budget_usd,
            native_wallet_evidence={
                "asset":asset,"network":_NATIVE[asset][0],
                "status":"verified","balance":native_balance},
            stable_wallet_evidence=source,
            market_price_usd=prices[asset],
            stable_price_usd=prices[stable],
            pending_buy_usd=_pending_usd(pending,asset,prices),
        )
        return result
    except (ValueError,TypeError,ArithmeticError,psycopg.Error,
            httpx.HTTPError):
        LOG.warning("Native original BUY owner/source exposure unavailable")
        raise HTTPException(503,"Real native owner budget evidence unavailable") from None
