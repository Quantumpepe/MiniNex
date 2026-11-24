"""Verified native LIVE BUY source evidence, no simulated funding or send.

Runs one targeted native address balance RPC and one existing ETH wallet
balance read; stale/missing explorer data is never coerced to zero. A shared
real USD reference is used only for owner-budget sizing, never to authorize
the THORChain swap. The actual coordinator still requires a fresh private
THORChain quote and immutable Privy grant.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal,InvalidOperation,localcontext
import logging
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .coingecko_provider import COIN_IDS
from .shared_evm_policy_reference_rates import fetch_evm_policy_reference_rates
from .native_coin_balances import (
    CHAINS,_read_one,verified_native_addresses,
)
from .native_live_budget import (
    _NATIVE,exact_owner_native_buy_amount,
)
from .privy_wallet_verification import fetch_privy_user
from .storage import _db_url,_ensure_schema,_safe_did
from .wallet_balances import get_wallet_balances
from .native_thor_swap_quote import _exact_owner_wallets,get_private_native_thornode_terms
from .ethereum_settlement_funding import read_pending_ethereum_funding

LOG=logging.getLogger("mininex.native_live_source")
_PRICE_TTL=180
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
    if asset not in _NATIVE or asset not in COIN_IDS:
        raise HTTPException(422,"Unknown original native live price asset")
    data=await fetch_evm_policy_reference_rates(asset,"USDC")
    now=int(time.time())
    try:
        return {coin:_native_rate(data,coin,now) for coin in (asset,"USDC","USDT")}
    except (ValueError,TypeError,KeyError):
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
    did:str,*,asset:str,stable:str,budget_usd:object=None,
    budget_percent:int|None=None,client:httpx.AsyncClient,
)->str|None:
    """One coin's actual available balance, holdings, pending and USD rates."""
    did=_safe_did(did)
    if (asset not in _NATIVE or stable not in _NATIVE[asset][2]
            or type(client) is not httpx.AsyncClient
            or (asset=="BTC" and (
                budget_usd is not None or type(budget_percent) is not int
                or not 0<=budget_percent<=100))
            or (asset!="BTC" and (
                budget_percent is not None or type(budget_usd) is not int
                or budget_usd<=0))):
        raise HTTPException(409,"Invalid owner-selected native LIVE allocation")
    user=await fetch_privy_user(did)
    try:
        if asset=="BTC":
            address,_=_exact_owner_wallets(user,did,asset)
        else:
            address=verified_native_addresses(user,did).get(asset)
    except (ValueError,KeyError,TypeError):
        address=None
    if not address:
        raise HTTPException(409,"Owner's unique native receiving wallet missing")
    try:
        if asset=="BTC":
            eth_result,prices,pending=await asyncio.gather(
                get_wallet_balances(did),fresh_native_usd_prices(asset),
                asyncio.to_thread(_pending_owner_native_buys,did,asset))
            btc=eth_result.get("balances",{}).get("BTC",{})
            if (eth_result.get("wallets",{}).get("BTC")!=address
                    or btc.get("status")!="verified" or btc.get("network")!="bitcoin-mainnet"):
                raise ValueError("Original confirmed Bitcoin balance unavailable")
            native_balance=btc["balance"]
        else:
            eth_result,native_balance,prices,pending=await asyncio.gather(
                get_wallet_balances(did),_read_one(client,asset,address),
                fresh_native_usd_prices(asset),
                asyncio.to_thread(_pending_owner_native_buys,did,asset),
            )
        source=eth_result.get("balances",{}).get(stable,{})
        if (eth_result.get("wallet_ownership_verified") is not True
                or type(source) is not dict
                or eth_result.get("wallets",{}).get("ETH") is None):
            raise ValueError("Original ETH funding owner unavailable")
        # A single account must own both wallets. No secondary imported key.
        pending_usd=_pending_usd(pending,asset,prices)
        if asset=="BTC":
            # The existing BTC setting is a percentage, not a second
            # invented USD field. Apply it to the actually verified
            # native BTC, current ETH stable and committed pending BUY
            # exposures. At 100% the owner can spend ALL real cash.
            native_value=Decimal(native_balance)*prices["BTC"]
            stable_value=Decimal(source["balance"])*prices[stable]
            budget_usd=(native_value+stable_value+pending_usd)*(
                Decimal(budget_percent)/Decimal(100))
        if asset!="DOGE":
            shared=await asyncio.to_thread(read_pending_ethereum_funding,did,
                asset=asset,stable=stable,stable_usd=prices[stable])
            cash=max(Decimal(0),Decimal(source["balance"])-
                     Decimal(shared["pending_wallet_stable_atomic"]).scaleb(-6))
            source={**source,"balance":format(cash,"f")}
        result=exact_owner_native_buy_amount(
            asset=asset,stable=stable,budget_usd=budget_usd,
            native_wallet_evidence={
                "asset":asset,"network":_NATIVE[asset][0],
                "status":"verified","balance":native_balance},
            stable_wallet_evidence=source,
            market_price_usd=prices[asset],
            stable_price_usd=prices[stable],
            pending_buy_usd=pending_usd,
        )
        return result
    except (ValueError,TypeError,ArithmeticError,psycopg.Error,
            httpx.HTTPError):
        LOG.warning("Native original BUY owner/source exposure unavailable")
        raise HTTPException(503,"Real native owner budget evidence unavailable") from None


def finalized_native_position_atomic(rows,*,asset):
    if asset not in _NATIVE:
        raise ValueError("Unknown native position")
    with localcontext() as ctx:
        ctx.prec=100
        position=0
        scale=10**_NATIVE[asset][1]
        for row in rows:
            if row["side"]=="BUY":
                amount=Decimal(row["destination_received_atomic"])
                if not amount.is_finite() or amount<=0 or amount!=amount.to_integral_value():
                    raise ValueError("Invalid original native credit")
                position+=int(amount)
            elif row["side"]=="SELL":
                amount=Decimal(row["input_amount"])*scale
                if not amount.is_finite() or amount<=0 or amount!=amount.to_integral_value():
                    raise ValueError("Invalid original native debit")
                position=max(0,position-int(amount))
            else:
                raise ValueError("Invalid original order side")
        return position


def read_native_position_atomic(did,*,asset,wallet):
    """Read only original owner-specific, fully finalized strategy orders."""
    did=_safe_did(did)
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT 1 FROM mininex_live_native_order_intents
                WHERE privy_did=%s AND asset=%s
                  AND ((status='PREPARED' AND quote_expires_at>now()) OR
                    status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED','SOURCE_FINALIZED'))
                LIMIT 1""",(did,asset))
            if cur.fetchone() is not None:
                return None
            cur.execute("""SELECT side,input_amount,destination_received_atomic
                FROM mininex_live_native_order_intents
                WHERE privy_did=%s AND asset=%s AND status='DESTINATION_FINALIZED'
                  AND source_proof_sha256 IS NOT NULL
                  AND destination_proof_sha256 IS NOT NULL
                  AND ((side='BUY' AND destination_wallet=%s)
                       OR (side='SELL' AND source_wallet=%s))
                ORDER BY destination_finalized_at,intent_key""",(did,asset,wallet,wallet))
            return finalized_native_position_atomic(cur.fetchall(),asset=asset)


def exact_native_sell_amount(*,asset,position_atomic,wallet_balance):
    if asset not in _NATIVE or type(position_atomic) is not int or position_atomic<0:
        raise ValueError("Invalid verified strategy position")
    with localcontext() as ctx:
        ctx.prec=100
        scale=10**_NATIVE[asset][1]
        wallet=Decimal(wallet_balance)*scale
        if not wallet.is_finite() or wallet<0 or wallet!=wallet.to_integral_value():
            raise ValueError("Invalid confirmed native wallet balance")
        available=min(position_atomic,int(wallet))
        return format(Decimal(available)/scale,"f") if available else None


async def prepare_native_sell_candidate(did,*,asset,stable,client):
    """Read-only SELL preparation; fee reserve and actual signer remain required."""
    did=_safe_did(did)
    if asset not in ("BTC","SOL","XRP"):
        return {"asset":asset,"state":"WAIT","reason":"NATIVE_SELL_SOURCE_COORDINATOR_REQUIRED"}
    user=await fetch_privy_user(did)
    wallet,eth=_exact_owner_wallets(user,did,asset)
    position=await asyncio.to_thread(read_native_position_atomic,did,asset=asset,wallet=wallet)
    if position is None:
        return {"asset":asset,"state":"WAIT","reason":"PREVIOUS_NATIVE_ORDER_UNRESOLVED"}
    if position==0:
        return {"asset":asset,"state":"WAIT","reason":"NO_FINALIZED_NATIVE_POSITION"}
    if asset=="BTC":
        evidence=await get_wallet_balances(did)
        balance=evidence.get("balances",{}).get("BTC",{})
        if (evidence.get("wallet_ownership_verified") is not True
            or evidence.get("wallets",{}).get("BTC")!=wallet
            or balance.get("status")!="verified" or balance.get("network")!="bitcoin-mainnet"):
            raise ValueError("Original Bitcoin wallet balance unavailable")
        amount=exact_native_sell_amount(asset=asset,position_atomic=position,wallet_balance=balance["balance"])
    else:
        balance=await _read_one(client,asset,wallet)
        amount=exact_native_sell_amount(asset=asset,position_atomic=position,wallet_balance=balance)
    if amount is None:
        return {"asset":asset,"state":"WAIT","reason":"NO_VERIFIED_NATIVE_SELL_BALANCE"}
    # THORChain quotes use at most eight decimal places. Round DOWN in native
    # units only, not in the value of the Ethereum stablecoin.
    from decimal import ROUND_DOWN
    with localcontext() as ctx:
        ctx.prec=100
        amount=format(Decimal(amount).quantize(Decimal(1).scaleb(-min(8,_NATIVE[asset][1])),rounding=ROUND_DOWN),"f")
    if Decimal(amount)==0:
        return {"asset":asset,"state":"WAIT","reason":"NO_REPRESENTABLE_NATIVE_SELL_AMOUNT"}
    quote=await get_private_native_thornode_terms(did,asset=asset,side="SELL",stable=stable,amount=amount)
    if quote["source_wallet"]!=wallet or quote["destination_wallet"].lower()!=eth.lower():
        raise ValueError("Original native SELL wallet changed")
    return {"asset":asset,"side":"SELL","state":"NATIVE_SELL_QUOTE_PREPARED",
            "input_amount":amount,"stable":stable,
            "expected_output_units_1e8":quote["expected_output_units_1e8"],
            "venue_fee_units_1e8":quote["venue_fee_units_1e8"],
            "needs_extended_btc_memo":quote["needs_extended_btc_memo"],
            "can_submit":False,"real_trade_sent":False,
            "reason":"NATIVE_SELL_FEE_AND_SIGNER_REQUIRED"}
