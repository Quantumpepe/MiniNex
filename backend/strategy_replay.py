"""Read-only, deterministic historical MiniNex strategy replay.

No network calls, database writes, user balances, wallet, Privy signer or orders.
Replays publicly observed Binance candles/ticks with the same paper strategy.
Historical bid/ask and independent Kraken cross-checks are NOT available for
each old candle: replay's valid feed is a controlled simulation assumption,
never an authorization to trade live.
"""
from __future__ import annotations

from collections import Counter
import math
from typing import Any

from .auto_engine import PaperAutoEngine, PaperPortfolio, PaperRules, clean
from .extra_market_data import EXTRA_ASSETS

WINDOWS = {"5m":300.0,"15m":900.0,"1h":3600.0,"24h":86400.0}
MODES = ("LONG","SPOT_SHORT","AUTO")
MAX_POINTS = 3600
# ADA remains IN ARBEIT. Other assets borrow the proven BTC/ETH strategy only
# for independent, on-demand PAPER replay -- never for live/automatic orders.
EXTRA_REPLAY_ASSETS = tuple(asset for asset in EXTRA_ASSETS if asset != 'ADA')
REPLAY_ASSETS = ('BTC', 'ETH') + EXTRA_REPLAY_ASSETS
assert len(EXTRA_REPLAY_ASSETS) == 9


class _PastOnlyMarket:
    def __init__(self) -> None:
        self.seen: list[dict[str,float]] = []

    def history(self, asset: str, *, limit: int = 3600) -> list[dict[str,float]]:
        return self.seen[-limit:]


def replay_strategy(
    asset: str, mode: str, horizon: str,
    observations: list[dict[str, Any]], *, initial_usd: float = 500.0,
) -> dict[str, Any]:
    if asset not in REPLAY_ASSETS or mode not in MODES or horizon not in WINDOWS:
        raise ValueError("Invalid replay selection")
    if not math.isfinite(initial_usd) or not 30 <= initial_usd <= 100000:
        raise ValueError("Invalid virtual replay budget")
    if not isinstance(observations,list) or len(observations)>MAX_POINTS:
        raise ValueError("Invalid replay sample count")

    points: list[dict[str,float]] = []
    last_ts = -math.inf
    # Dedup same-timestamp samples with latest value, never sort data from
    # the future into an earlier evaluation.
    for item in observations:
        if not isinstance(item,dict):
            raise ValueError("Invalid replay sample")
        try:
            t = float(item["timestamp"])
            price = float(item["price_usdt"])
        except (TypeError,ValueError,KeyError,OverflowError):
            raise ValueError("Invalid replay sample") from None
        if not math.isfinite(t) or not math.isfinite(price) or t<=0 or price<=0 or t<last_ts:
            raise ValueError("Non-monotonic or invalid historical price")
        point={"timestamp":t,"price_usdt":price}
        if t==last_ts:
            points[-1]=point
        else:
            points.append(point)
            last_ts=t

    span = points[-1]["timestamp"]-points[0]["timestamp"] if points else 0
    required_span = WINDOWS[horizon]
    extra = asset in EXTRA_REPLAY_ASSETS
    # Extra coins have 1-minute historical candles, not reliable 24h ticks.
    # Do not show a hypothetical full-horizon trade when history is too short.
    if len(points)<(16 if extra else 5) or (extra and span < required_span):
        return {"asset":asset,"mode":mode,"horizon":horizon,
                "status":"INSUFFICIENT_WINDOW" if extra and len(points)>=16
                          else "INSUFFICIENT_SAMPLES",
                "sample_count":len(points),"observed_minutes":clean(span/60,2),
                "required_minutes":clean(required_span/60,2),
                "paper_only":True,"hypothetical":True,
                "real_trading_enabled":False,"wallet_funds_used":False,
                "budget_isolated":True,"trades":[],"orders_executed":0}

    past=_PastOnlyMarket()
    engine=PaperAutoEngine(past,rules=PaperRules(
        warmup_seconds=0.0,min_history_samples=4,
        price_window_seconds=WINDOWS[horizon],
    ))
    start=points[0]["price_usdt"]
    if mode in ("LONG", "AUTO"):
        holding=PaperPortfolio(asset=asset,settlement="USDC",
                               strategy_mode=mode,stable_cash=initial_usd,
                               initial_equity=initial_usd)
    else:
        holding=PaperPortfolio(asset=asset,settlement="USDC",
                               strategy_mode=mode,coin_quantity=initial_usd/start,
                               cost_basis=initial_usd,entry_price=start,
                               peak_price=start,initial_equity=initial_usd)
    engine.portfolios={asset:holding}
    peak_equity=initial_usd
    worst_drawdown=0.0
    decisions=Counter()
    # All feeds are HYPOTHETICAL replay marks, NOT independently verified
    # transaction quotes. No lookahead: the strategy sees only prior samples.
    for point in points:
        past.seen.append(point)
        price=point["price_usdt"]
        engine.process_asset(asset,{"market_data_valid":True,
            "price_usdt":price,"reason":"HISTORICAL_REPLAY_ONLY"},
            point["timestamp"])
        decisions[holding.last_decision]+=1
        estimated_exit=engine._sell_quote(asset,holding.coin_quantity,price)
        equity=holding.stable_cash+(estimated_exit["net_proceeds"] if estimated_exit else 0.0)
        peak_equity=max(peak_equity,equity)
        if peak_equity>0:
            worst_drawdown=max(worst_drawdown,(peak_equity-equity)/peak_equity*100)

    final_price=points[-1]["price_usdt"]
    final_exit=engine._sell_quote(asset,holding.coin_quantity,final_price)
    final_equity=holding.stable_cash+(final_exit["net_proceeds"] if final_exit else 0.0)
    # Same hypothetical costs for a single buy-and-hold baseline, rather
    # than claiming a strategy gain solely because the market rallied.
    hold_buy=engine._buy_quote(asset,initial_usd,start)
    hold_sell=(engine._sell_quote(asset,hold_buy["quantity"],final_price)
               if hold_buy else None)
    hold_equity=(initial_usd-hold_buy["total_spent"]+hold_sell["net_proceeds"]
                 if hold_buy and hold_sell else None)
    hold_return=((hold_equity/initial_usd-1)*100
                 if hold_equity is not None else None)
    strategy_return=(final_equity/initial_usd-1)*100
    return {
        "asset":asset,"mode":mode,"horizon":horizon,"status":"SIMULATED",
        "paper_only":True,"hypothetical":True,"orders_executed":0,
        "real_trading_enabled":False,"wallet_funds_used":False,
        "budget_isolated":True,"simulation_only":True,
        "adaptive_trailing_from_past_volatility":True,
        "historical_quotes_not_independently_verified":True,
        "warning":"Historical strategy replay, not a real fill or profitability forecast. "
                  "Does not recreate historical executable bid/ask, routing or liquidity.",
        "sample_count":len(points),
        "observed_minutes":clean(span/60,2),
        "required_minutes":clean(required_span/60,2),
        "estimated_network_fee_usd_per_trade":clean(engine.costs.network(asset),2),
        "cost_model":"GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES",
        "from_timestamp":points[0]["timestamp"],
        "to_timestamp":points[-1]["timestamp"],
        "initial_usd":clean(initial_usd),
        "estimated_final_liquidation_usd":clean(final_equity),
        "estimated_net_return_pct":clean(strategy_return,4),
        "estimated_buy_and_hold_return_pct":clean(hold_return,4) if hold_return is not None else None,
        "estimated_strategy_minus_hold_pp":clean(strategy_return-hold_return,4)
                                           if hold_return is not None else None,
        "estimated_max_drawdown_pct":clean(worst_drawdown,4),
        "realized_paper_pnl_usd":clean(holding.realized_profit),
        "trade_count":holding.trade_count,
        "buy_count":sum(t["kind"]=="PAPER_BUY" for t in holding.trade_history),
        "sell_count":sum(t["kind"]=="PAPER_SELL" for t in holding.trade_history),
        "last_reason":holding.last_reason,
        "decision_counts":dict(decisions),
        "trades":list(holding.trade_history)[-30:],
    }
