"""Real ETH AUTO strategy tick: verified market + real wallet balances -> Privy trade.

This is the LIVE engine input, NOT an adapter for PAPER simulated fills.
Each funded order is sent through the existing durable Privy AUTO dispatcher.
The account scanner and owner-controlled start/stop are wired separately.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, ROUND_DOWN
import math
import time

import httpx
from fastapi import HTTPException

from .eth_live_auto_dispatch import EthAutoSignal,execute_eth_auto_signal
from .eth_router02_allowance import inspect_router02_approval
from .live_eth_controls import get_live_eth_limits
from .wallet_balances import get_wallet_balances
from .storage import _safe_did

_D = Decimal
_MIN_ORDER = _D("15")
_ZERO = _D("0")
_RESERVE = _D("0.000000000000000001")


def verified_live_eth_momentum(market, *, now: float) -> dict:
    """Momentum/trailing signal from actual fresh external prices, never PAPER."""
    if type(now) not in (int,float) or not math.isfinite(now) or now<=0:
        raise ValueError("Invalid market timestamp")
    feed=market.snapshot(now=now)["markets"]["ETH"]
    price=feed.get("price_usdt")
    if (feed.get("market_data_valid") is not True
            or type(price) not in (int,float)
            or not math.isfinite(price) or price<=0):
        return {"action":"WAIT","reason":"LIVE_MARKET_DATA_UNVERIFIED"}
    points=[]
    for entry in market.history("ETH",limit=3600):
        try:
            t=float(entry["timestamp"]);v=float(entry["price_usdt"])
        except (ValueError,TypeError,KeyError,OverflowError):
            continue
        if math.isfinite(t) and math.isfinite(v) and v>0 and 0<=now-t<=900:
            points.append((t,v))
    points.sort()
    if len(points)<20 or now-points[0][0]<180 or now-points[-1][0]>12:
        return {"action":"WAIT","reason":"LIVE_OBSERVATION_INCOMPLETE"}
    def history_price(age):
        older=[value for t,value in points if t<=now-age]
        return older[-1] if older else None
    p60=history_price(60);p180=history_price(180)
    if not p60 or not p180:
        return {"action":"WAIT","reason":"LIVE_MOMENTUM_INCOMPLETE"}
    m60=(price/p60-1)*100
    m180=(price/p180-1)*100
    high=max(v for _,v in points)
    high_drawdown=(1-price/high)*100
    if m60>=0.12 and m180>=0.22 and high_drawdown<0.6:
        return {"action":"BUY","reason":"LIVE_CONFIRMED_UPTREND",
                "market_price":str(price),
                "m60":m60,"m180":m180}
    if ((m60<=-0.13 and m180<=-0.22)
            or (high_drawdown>=0.8 and m60<=-0.06)):
        return {"action":"SELL","reason":"LIVE_CONFIRMED_REVERSAL",
                "market_price":str(price),
                "m60":m60,"m180":m180}
    return {"action":"WAIT","reason":"LIVE_NO_TRADE_SIGNAL"}


def _positive_decimal(value,what):
    if type(value) is not str or len(value)>60:
        raise ValueError(what+" must be exact")
    try: d=_D(value)
    except Exception: raise ValueError(what+" invalid") from None
    if not d.is_finite() or d<0 or d>=_D("1e20"):
        raise ValueError(what+" invalid")
    return d


def live_trade_quantity(*, decision: dict, live_limits: dict,
                        balances: dict, allowance: dict) -> str|None:
    """Amount is real source-token units, capped by own LIVE spending limits.

    Never silently approve new tokens. If allowance is unavailable, the owner
    can approve via the existing explicit Privy wallet interface.
    """
    action=decision.get("action")
    if action not in ("BUY","SELL"):
        return None
    if live_limits.get("trading_paused") is not False:
        return None
    if (allowance.get("wallet_identity_verified") is not True
            or allowance.get("side")!=action
            or allowance.get("router_allowance_sufficient") is not True
            or allowance.get("source_funds_sufficient") is not True
            or allowance.get("conservative_native_gas_reserve_met") is not True):
        return None
    cap=min(_positive_decimal(live_limits.get("planned_capital_usd"),"capital"),
            _positive_decimal(live_limits.get("max_order_usd"),"limit"))
    if cap<_MIN_ORDER:
        return None
    if action=="BUY":
        stable=allowance["stable"]
        b=balances.get("balances",{}).get(stable,{})
        if b.get("status")!="verified" or balances.get("wallet_ownership_verified") is not True:
            return None
        source=_positive_decimal(b["balance"],"stable balance")
        # Preserve some cash for gas/volatility; a new BUY deploys at most 50%.
        quantity=min(cap,source*_D("0.50"))
        if quantity<_MIN_ORDER:
            return None
        quantity=quantity.quantize(_D("0.000001"),rounding=ROUND_DOWN)
    else:
        weth=_positive_decimal(allowance["verified_source_balance"],"WETH")
        price=_positive_decimal(decision.get("market_price"),"market price")
        quantity=min(weth,cap/price)
        quantity=quantity.quantize(_D("0.000000000000000001"),rounding=ROUND_DOWN)
        if quantity*price<_MIN_ORDER:
            return None
    if quantity<=0:
        return None
    return format(quantity,"f")


async def execute_live_eth_strategy_tick(did: str, *, market,
                                         run_id: str,stable: str,
                                         client: httpx.AsyncClient,
                                         now: float|None=None) -> dict:
    """Evaluate one independently enabled LIVE user; submit once or wait."""
    did=_safe_did(did)
    if stable not in ("USDC","USDT"):
        raise ValueError("Unsupported live stablecoin")
    if type(run_id) is not str or len(run_id)<8 or len(run_id)>96:
        raise ValueError("Invalid live strategy session")
    now=time.time() if now is None else now
    decision=verified_live_eth_momentum(market,now=now)
    if decision["action"]=="WAIT":
        return {"state":"WAIT","reason":decision["reason"],"asset":"ETH"}
    limits=await asyncio.to_thread(get_live_eth_limits,did)
    if limits["trading_paused"] is not False:
        return {"state":"WAIT","reason":"LIVE_ETH_PAUSED","asset":"ETH"}
    allowance_probe="0.000001" if decision["action"]=="BUY" else "0.000000000000000001"
    balances,allowance=await asyncio.gather(
        get_wallet_balances(did),
        inspect_router02_approval(did,side=decision["action"],
                                   stable=stable,amount=allowance_probe))
    # The inspector has read real ERC20 balances and allowance. Choose a
    # real quantity, then independently re-check the FULL amount.
    probe={**allowance,"router_allowance_sufficient":True,
           "source_funds_sufficient":True}
    amount=live_trade_quantity(
        decision=decision,live_limits=limits,
        balances=balances,allowance=probe)
    if amount is None:
        return {"state":"WAIT","reason":"NO_FUNDED_LIVE_POSITION","asset":"ETH"}
    funded=await inspect_router02_approval(
        did,side=decision["action"],stable=stable,amount=amount)
    if (funded.get("router_allowance_sufficient") is not True
            or funded.get("source_funds_sufficient") is not True
            or funded.get("conservative_native_gas_reserve_met") is not True):
        return {"state":"WAIT","reason":"OWNER_APPROVAL_OR_BALANCE_REQUIRED","asset":"ETH"}
    # Same event within a 60-second bucket never generates duplicate intents.
    event=f"ethlive_{int(now//60)}_{decision['action'].lower()}"
    signal=EthAutoSignal(strategy_run_id=run_id,signal_event_id=event,
                          side=decision["action"],stable=stable,
                          amount=amount)
    return await execute_eth_auto_signal(did,signal,client=client)
