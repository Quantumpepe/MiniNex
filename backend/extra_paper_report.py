"""One read-only, per-Privy-user comparison of nine persistent PAPER coins.

The report deliberately does not start, stop, rebalance, tune or sign for a
session. It distinguishes lifetime account equity from a bounded (30-event)
sample of recent trade reasons, costs and closed sell outcomes.
"""
from __future__ import annotations

from collections import defaultdict
import logging
import math

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .extra_paper import _present, mark_to_market
from .paper_trade_insights import (
    analyze_recent_trades, market_change_24h, observation_status,
)
from .strategy_replay import EXTRA_REPLAY_ASSETS
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG=logging.getLogger("mininex.extra_paper_report")
RECENT_LIMIT=30
COST_KEYS=("venue_fee","network_fee","spread_and_slippage_cost")


class _OneSnapshot:
    """A single bounded read of the existing public verified market marks."""
    def __init__(self, feeds: dict):
        self.feeds=feeds

    def snapshot(self):
        return self.feeds


def _finite(raw: object, *, nonnegative: bool = False) -> float | None:
    if type(raw) not in (int,float,str):
        return None
    try:
        value=float(raw)
    except (TypeError,ValueError,OverflowError):
        return None
    if not math.isfinite(value) or (nonnegative and value<0):
        return None
    return value


def summarize(asset: str,row: dict | None, events: list[dict],
              feeds: dict | None) -> dict:
    """Pure and non-mutating for one coin; missing quotes never imply zero PnL."""
    if asset not in EXTRA_REPLAY_ASSETS:
        raise ValueError("Unexpected extra-coin symbol")
    base={
        "asset":asset,"status":"NOT_STARTED",
        "paper_only":True,"simulation_only":True,
        "wallet_funds_used":False,"real_trading_enabled":False,
        "budget_isolated":True,"parameters_unchanged":True,
        "recent_window_limit":RECENT_LIMIT,
        "cost_model":"GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES",
        "budget_usd":None,"estimated_equity_usd":None,
        "estimated_total_pnl_usd":None,"estimated_net_return_pct":None,
        "quote_valid":False,"cumulative_realized_pnl_usd":None,
        "trade_count":0,"recent_buys":0,"recent_closed_sells":0,
        "recent_winning_sells":0,"recent_losing_sells":0,
        "recent_flat_sells":0,"recent_realized_pnl_usd":None,
        "recent_estimated_costs_usd":None,"recent_costs_complete":False,
        "runtime_active_seconds":None,"pause_exit_pending":False,
        "last_decision":None,"last_reason":None,
        "last_signal_summary":None,
        "recent_trade_sample_count":0,
        "recent_trade_sample_is_lifetime":False,
        "recent_entry_reason_counts":{
            "DCA":0,"REBOUND":0,"BREAKOUT":0,"TREND":0,"OTHER":0},
        "recent_exit_reason_counts":{
            "TRAILING":0,"RISK":0,"SPOT":0,"PAUSE":0,"OTHER":0},
        "recent_sell_stats_complete":False,
        "recent_sell_win_rate_pct":None,
        "recent_average_sell_pnl_usd":None,
        "recent_largest_loss_usd":None,
        "market_change_24h_pct":None,
        "analysis_status":"NOT_STARTED",
    }
    if row is None:
        return base
    session=_present(asset,row,[])
    state=session.get("position") or {}
    budget=_finite(session.get("budget_usd"),nonnegative=True)
    if budget is None or budget<=0:
        return {**base,"status":"UNAVAILABLE"}
    status=session.get("status")
    if status not in ("ACTIVE","PAUSED"):
        return {**base,"status":"UNAVAILABLE"}
    view=mark_to_market(session,_OneSnapshot(feeds or {}))
    valuation=view.get("position",{}).get("valuation",{})
    valid=valuation.get("quote_valid") is True
    equity=_finite(valuation.get("estimated_total_equity_usd"),nonnegative=True) if valid else None
    realized=_finite(state.get("realized_net_profit"))
    count=state.get("trade_count")
    if type(count) is not int or count<0:
        return {**base,"status":"UNAVAILABLE"}
    costs=0.0
    recent=0.0
    complete=True
    buys=sells=wins=losses=flats=0
    # The query returns at most 30 events for each coin. Do not imply these
    # limited trades represent all historical wins or all lifetime fees.
    for event in events[:RECENT_LIMIT]:
        if not isinstance(event,dict) or event.get("real_trade") is not False or event.get("asset")!=asset:
            complete=False
            continue
        kind=event.get("kind")
        if kind not in ("PAPER_BUY","PAPER_SELL"):
            complete=False
            continue
        if kind=="PAPER_BUY":
            buys+=1
        else:
            sells+=1
            pnl=_finite(event.get("realized_net_profit"))
            if pnl is None:
                complete=False
            else:
                recent+=pnl
                if pnl>0:wins+=1
                elif pnl<0:losses+=1
                else:flats+=1
        parts=event.get("estimated_costs")
        if not isinstance(parts,dict):
            complete=False
            continue
        vals=[_finite(parts.get(key),nonnegative=True) for key in COST_KEYS]
        if any(v is None for v in vals):
            complete=False
        else:
            costs+=sum(vals)
    if len(events)>RECENT_LIMIT:
        complete=False
    runtime=session.get("runtime_active_seconds")
    insight=analyze_recent_trades(asset,events,count)
    market_row=feeds.get(asset) if isinstance(feeds,dict) else None
    market_pct=market_change_24h(market_row)
    usable_market=market_pct is not None
    summary=state.get("last_signal_summary")
    if not isinstance(summary,str) or not summary or len(summary)>160:
        summary=None
    return {**base,
            "status":status,
            "budget_usd":round(budget,2),
            "estimated_equity_usd":round(equity,2) if equity is not None else None,
            "estimated_total_pnl_usd":round(equity-budget,2) if equity is not None else None,
            "estimated_net_return_pct":round((equity/budget-1)*100,3) if equity is not None else None,
            "quote_valid":equity is not None,
            "cumulative_realized_pnl_usd":round(realized,2) if realized is not None else None,
            "trade_count":count,
            "recent_buys":buys,
            "recent_closed_sells":sells,
            "recent_winning_sells":wins,
            "recent_losing_sells":losses,
            "recent_flat_sells":flats,
            "recent_realized_pnl_usd":round(recent,2) if complete else None,
            "recent_estimated_costs_usd":round(costs,2) if complete else None,
            "recent_costs_complete":complete,
            "runtime_active_seconds":runtime,
            "pause_exit_pending":session.get("pause_exit_pending") is True,
            "last_decision":state.get("last_decision"),
            "last_reason":state.get("last_reason"),
            "last_signal_summary":summary,
            **insight,
            "market_change_24h_pct":market_pct,
            "analysis_status":observation_status(
                status,count,insight,usable_market),
            }


def compare(did: str,market: object) -> dict:
    """Two DB queries, one market snapshot, nine independent account rows."""
    did=_safe_did(did)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                cur.execute("""SELECT * FROM mininex_extra_paper_sessions
                    WHERE privy_did=%s AND asset=ANY(%s)""",
                    (did,list(EXTRA_REPLAY_ASSETS)))
                rows={r["asset"]:r for r in cur.fetchall()
                      if r.get("asset") in EXTRA_REPLAY_ASSETS}
                cur.execute("""SELECT asset,event_json FROM (
                    SELECT asset,event_json,
                        ROW_NUMBER() OVER (PARTITION BY asset ORDER BY event_id DESC) AS rn
                    FROM mininex_extra_paper_trades
                    WHERE privy_did=%s AND asset=ANY(%s)
                    ) recent WHERE rn<=%s
                    ORDER BY asset,rn DESC""",
                    (did,list(EXTRA_REPLAY_ASSETS),RECENT_LIMIT))
                histories=defaultdict(list)
                for r in cur.fetchall():
                    if r.get("asset") in EXTRA_REPLAY_ASSETS:
                        histories[r["asset"]].append(r.get("event_json"))
    except psycopg.Error:
        LOG.warning("Extra-coin PAPER comparison DB unavailable")
        raise HTTPException(503,"Personal PAPER comparison unavailable") from None
    # A temporary market-data outage must not erase the stored results.
    try:
        feeds=market.snapshot()
        if not isinstance(feeds,dict):
            feeds={}
    except Exception:
        feeds={}
    return {"paper_only":True,"simulation_only":True,
            "real_trading_enabled":False,"wallet_funds_used":False,
            "parameters_unchanged":True,"recent_window_limit":RECENT_LIMIT,
            "assets":[summarize(asset,rows.get(asset),histories[asset],feeds)
                      for asset in EXTRA_REPLAY_ASSETS]}
