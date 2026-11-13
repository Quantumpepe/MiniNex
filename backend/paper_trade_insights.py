"""Bounded, descriptive insights from recent PAPER fills only.

These counters describe the last 30 recorded trade events. They never change
any engine rules or suggest that a profitable sample predicts future returns.
"""
from __future__ import annotations

import math
from typing import Any

ENTRY_GROUPS=("DCA","REBOUND","BREAKOUT","TREND","OTHER")
EXIT_GROUPS=("TRAILING","RISK","SPOT","PAUSE","OTHER")
LIMIT=30


def _finite(raw: Any) -> float | None:
    if type(raw) not in (int,float,str):
        return None
    try:
        value=float(raw)
    except (OverflowError,ValueError,TypeError):
        return None
    return value if math.isfinite(value) else None


def _reason_group(kind: str,reason: Any) -> str:
    if not isinstance(reason,str):
        return "OTHER"
    if kind=="PAPER_BUY":
        if reason in ("ADD_TREND","ADD_REBOUND"):
            return "DCA"
        if reason=="REBOUND":
            return "REBOUND"
        if reason=="BREAKOUT":
            return "BREAKOUT"
        if reason in ("TREND_CONTINUATION","GRADUAL_TREND"):
            return "TREND"
        return "OTHER"
    if reason=="DYNAMIC_NET_PROFIT_TRAILING":
        return "TRAILING"
    if reason in ("RISK_EMERGENCY_EXIT","RISK_TREND_BREAKDOWN"):
        return "RISK"
    if reason=="SPOT_DOWNTREND_EXIT":
        return "SPOT"
    if reason=="USER_PAUSE_TO_STABLE":
        return "PAUSE"
    return "OTHER"


def analyze_recent_trades(asset: str,events: list[dict],lifetime_count: int
                          ) -> dict[str,Any]:
    """Never extrapolate sample costs or win rates to lifetime statistics."""
    groups_buy={name:0 for name in ENTRY_GROUPS}
    groups_sell={name:0 for name in EXIT_GROUPS}
    sells=0
    wins=losses=flats=0
    total=0.0
    largest_loss=None
    complete=True
    for event in events[:LIMIT]:
        if (not isinstance(event,dict) or event.get("asset")!=asset
                or event.get("real_trade") is not False
                or event.get("kind") not in ("PAPER_BUY","PAPER_SELL")):
            complete=False
            continue
        kind=event["kind"]
        group=_reason_group(kind,event.get("reason"))
        if kind=="PAPER_BUY":
            groups_buy[group]+=1
            continue
        groups_sell[group]+=1
        sells+=1
        profit=_finite(event.get("realized_net_profit"))
        if profit is None:
            complete=False
            continue
        total+=profit
        if profit>0:
            wins+=1
        elif profit<0:
            losses+=1
            largest_loss=profit if largest_loss is None else min(largest_loss,profit)
        else:
            flats+=1
    # A provider error or malformed saved event must not present a partial
    # win rate as complete.
    valid_sample=complete and len(events)<=LIMIT and wins+losses+flats==sells
    count_ok=type(lifetime_count) is int and lifetime_count>=0
    sample_length=min(len(events),LIMIT)
    return {
        "recent_trade_sample_count":sample_length,
        "recent_trade_sample_is_lifetime":
            bool(count_ok and lifetime_count==sample_length and valid_sample),
        "recent_entry_reason_counts":groups_buy,
        "recent_exit_reason_counts":groups_sell,
        "recent_sell_stats_complete":valid_sample,
        "recent_sell_win_rate_pct":round(wins/sells*100,1)
            if valid_sample and sells else None,
        "recent_average_sell_pnl_usd":round(total/sells,2)
            if valid_sample and sells else None,
        "recent_largest_loss_usd":round(largest_loss,2)
            if valid_sample and largest_loss is not None else None,
    }


def market_change_24h(feed: dict | None) -> float | None:
    """24h coin-market movement: distinct period from the paper session ROI."""
    if not isinstance(feed,dict) or feed.get("market_data_valid") is not True:
        return None
    value=_finite(feed.get("change_24h_pct"))
    return round(value,3) if value is not None and value>=-100 else None


def observation_status(status: str,trade_count: int, insight: dict,
                       market_is_verified: bool) -> str:
    if status=="NOT_STARTED":
        return "NOT_STARTED"
    if status=="UNAVAILABLE":
        return "UNAVAILABLE"
    if insight.get("recent_sell_stats_complete") is not True:
        return "INCOMPLETE_SAMPLE"
    sells=sum(insight.get("recent_exit_reason_counts",{}).values())
    if sells==0:
        return "NO_CLOSED_SELLS"
    if sells<5:
        return "EARLY_SAMPLE"
    if not market_is_verified:
        return "PRICE_UNAVAILABLE"
    return "OBSERVATIONS_AVAILABLE"
