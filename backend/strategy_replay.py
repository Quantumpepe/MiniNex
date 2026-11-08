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

WINDOWS = {"5m":300.0,"15m":900.0,"1h":3600.0,"24h":86400.0}
MODES = ("LONG","SPOT_SHORT")
MAX_POINTS = 3600


class _PastOnlyMarket:
    def __init__(self) -> None:
        self.seen: list[dict[str,float]] = []

    def history(self, asset: str, *, limit: int = 3600) -> list[dict[str,float]]:
        return self.seen[-limit:]


def replay_strategy(
    asset: str, mode: str, horizon: str,
    observations: list[dict[str, Any]], *, initial_usd: float = 500.0,
) -> dict[str, Any]:
    if asset not in ("BTC","ETH") or mode not in MODES or horizon not in WINDOWS:
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

    if len(points)<5:
        return {"asset":asset,"mode":mode,"horizon":horizon,
                "status":"INSUFFICIENT_SAMPLES","sample_count":len(points),
                "paper_only":True,"hypothetical":True,
                "trades":[],"orders_executed":0}

    past=_PastOnlyMarket()
    engine=PaperAutoEngine(past,rules=PaperRules(
        warmup_seconds=0.0,min_history_samples=4,
        price_window_seconds=WINDOWS[horizon],
    ))
    start=points[0]["price_usdt"]
    if mode=="LONG":
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
    return {
        "asset":asset,"mode":mode,"horizon":horizon,"status":"SIMULATED",
        "paper_only":True,"hypothetical":True,"orders_executed":0,
        "historical_quotes_not_independently_verified":True,
        "warning":"Historical strategy replay, not a real fill or profitability forecast. "
                  "Does not recreate historical executable bid/ask, routing or liquidity.",
        "sample_count":len(points),
        "from_timestamp":points[0]["timestamp"],
        "to_timestamp":points[-1]["timestamp"],
        "initial_usd":clean(initial_usd),
        "estimated_final_liquidation_usd":clean(final_equity),
        "estimated_net_return_pct":clean((final_equity/initial_usd-1)*100,4),
        "estimated_max_drawdown_pct":clean(worst_drawdown,4),
        "realized_paper_pnl_usd":clean(holding.realized_profit),
        "trade_count":holding.trade_count,
        "buy_count":sum(t["kind"]=="PAPER_BUY" for t in holding.trade_history),
        "sell_count":sum(t["kind"]=="PAPER_SELL" for t in holding.trade_history),
        "last_reason":holding.last_reason,
        "decision_counts":dict(decisions),
        "trades":list(holding.trade_history)[-30:],
    }
