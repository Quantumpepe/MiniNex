"""Evidence for nine-coin PAPER decision diagnostics, never auto-tuning."""
from __future__ import annotations

from decimal import Decimal
import math

import pytest

from backend import extra_paper_report as report
from backend.paper_trade_insights import (
    ENTRY_GROUPS, EXIT_GROUPS, analyze_recent_trades,
    market_change_24h, observation_status,
)
from backend.auto_engine import PaperPortfolio
from backend.user_paper import _state_of

ASSET="SOL"


def fill(kind,reason,pnl=None):
    event={"asset":ASSET,"kind":kind,"reason":reason,"real_trade":False}
    if kind=="PAPER_SELL":
        event["realized_net_profit"]=pnl
    event["estimated_costs"]={
        "venue_fee":0.20,"network_fee":0.10,
        "spread_and_slippage_cost":0.15}
    return event


def observed():
    return [
        fill("PAPER_BUY","ADD_REBOUND"),
        fill("PAPER_BUY","ADD_TREND"),
        fill("PAPER_BUY","REBOUND"),
        fill("PAPER_BUY","BREAKOUT"),
        fill("PAPER_SELL","DYNAMIC_NET_PROFIT_TRAILING",4.0),
        fill("PAPER_SELL","RISK_EMERGENCY_EXIT",-7.0),
        fill("PAPER_SELL","USER_PAUSE_TO_STABLE",-2.0),
        fill("PAPER_SELL","SPOT_DOWNTREND_EXIT",1.0),
    ]


def session():
    portfolio=PaperPortfolio(
        asset="SOL",settlement="USDT",strategy_mode="LONG",
        stable_cash=400.0,coin_quantity=1.0,cost_basis=100.0,
        initial_equity=500.0,realized_profit=-4.0,trade_count=8,
    )
    state=_state_of(portfolio)
    state["last_decision"]="HOLD"
    state["last_reason"]="TREND_ACTIVE_OR_NET_EXIT_NOT_PROFITABLE"
    state["last_signal_summary"]="1m +0.125% | 3m +0.325%"
    return {
        "asset":"SOL","status":"ACTIVE","settlement":"USDT",
        "strategy_mode":"LONG","horizon":"15m","revision":3,
        "initial_demo_cash":Decimal(500),
        "state_json":state,
    }


def test_all_known_trade_reasons_are_counted_without_tuning_thresholds():
    result=analyze_recent_trades(ASSET,observed(),8)
    assert set(result["recent_entry_reason_counts"])==set(ENTRY_GROUPS)
    assert set(result["recent_exit_reason_counts"])==set(EXIT_GROUPS)
    assert result["recent_entry_reason_counts"]["DCA"]==2
    assert result["recent_entry_reason_counts"]["REBOUND"]==1
    assert result["recent_entry_reason_counts"]["BREAKOUT"]==1
    assert result["recent_exit_reason_counts"]["TRAILING"]==1
    assert result["recent_exit_reason_counts"]["RISK"]==1
    assert result["recent_exit_reason_counts"]["PAUSE"]==1
    assert result["recent_exit_reason_counts"]["SPOT"]==1
    assert result["recent_sell_stats_complete"] is True
    assert result["recent_sell_win_rate_pct"]==50.0
    assert result["recent_average_sell_pnl_usd"]==-1.0
    assert result["recent_largest_loss_usd"]==-7.0
    assert result["recent_trade_sample_count"]==8
    assert result["recent_trade_sample_is_lifetime"] is True
    assert analyze_recent_trades(ASSET,observed(),12)[
        "recent_trade_sample_is_lifetime"] is False


def test_partial_events_and_nonfinite_sell_pnl_do_not_fake_win_rates():
    for changed in (
        [fill("PAPER_SELL","RISK_EMERGENCY_EXIT",math.nan)],
        [{"asset":ASSET,"real_trade":True,"kind":"PAPER_SELL",
          "reason":"DYNAMIC_NET_PROFIT_TRAILING","realized_net_profit":10}],
        [fill("PAPER_SELL","DYNAMIC_NET_PROFIT_TRAILING",None)],
        [None],
    ):
        row=analyze_recent_trades(ASSET,changed,1)
        assert row["recent_sell_stats_complete"] is False
        assert row["recent_sell_win_rate_pct"] is None
        assert row["recent_average_sell_pnl_usd"] is None
        assert row["recent_largest_loss_usd"] is None
    row=analyze_recent_trades(ASSET,[fill("PAPER_BUY","UNKNOWN_COIN_REASON")],1)
    assert row["recent_entry_reason_counts"]["OTHER"]==1
    assert row["recent_sell_win_rate_pct"] is None
    assert observation_status("ACTIVE",1,row,True)=="NO_CLOSED_SELLS"


def test_verifiable_24h_market_change_not_a_session_return():
    assert market_change_24h({
        "market_data_valid":True,"change_24h_pct":-9.8765})==-9.877
    assert market_change_24h({
        "market_data_valid":True,"change_24h_pct":0.0})==0.0
    for invalid in (
        {"market_data_valid":False,"change_24h_pct":25.0},
        {"market_data_valid":True,"change_24h_pct":math.nan},
        {"market_data_valid":True,"change_24h_pct":None},
        {"market_data_valid":True,"change_24h_pct":-101.0},
        {},
        None,
    ):
        assert market_change_24h(invalid) is None


def test_integration_keeps_market_conditions_and_trade_results_distinct():
    feed={"SOL":{"market_data_valid":True,"price_usdt":115.0,
                 "change_24h_pct":-3.75}}
    row=session()
    before=repr(row)
    insight=report.summarize("SOL",row,observed(),feed)
    assert insight["status"]=="ACTIVE"
    assert insight["quote_valid"] is True
    assert insight["estimated_net_return_pct"] is not None
    assert insight["market_change_24h_pct"]==-3.75
    assert insight["recent_sell_win_rate_pct"]==50
    assert insight["recent_average_sell_pnl_usd"]==-1
    assert insight["recent_largest_loss_usd"]==-7
    assert insight["analysis_status"]=="EARLY_SAMPLE"
    assert insight["last_signal_summary"].startswith("1m +0.125%")
    assert row.__repr__()==before
    assert insight["real_trading_enabled"] is False
    assert insight["parameters_unchanged"] is True
    assert insight["wallet_funds_used"] is False
    assert insight["recent_estimated_costs_usd"]==round(8*0.45,2)


def test_five_closed_trades_are_observations_not_trading_recommendations():
    events=observed()+[fill("PAPER_SELL","RISK_TREND_BREAKDOWN",-1)]
    insight=report.summarize("SOL",session(),events,{
        "SOL":{"market_data_valid":True,"price_usdt":104.0}})
    assert insight["analysis_status"]=="OBSERVATIONS_AVAILABLE"
    assert insight["market_change_24h_pct"] is None
    assert insight["recent_exit_reason_counts"]["RISK"]==2
    assert insight["recent_sell_win_rate_pct"]==40
    assert insight["recent_average_sell_pnl_usd"]==-1


def test_missing_market_quote_keeps_recent_closed_stats_but_no_roi():
    events=observed()+[fill("PAPER_SELL","RISK_TREND_BREAKDOWN",-1)]
    insight=report.summarize("SOL",session(),events,{})
    assert insight["quote_valid"] is False
    assert insight["estimated_net_return_pct"] is None
    assert insight["market_change_24h_pct"] is None
    assert insight["recent_sell_win_rate_pct"]==40
    assert insight["analysis_status"]=="PRICE_UNAVAILABLE"


def test_no_closed_sells_is_not_presented_as_100_percent_win_rate():
    event=fill("PAPER_BUY","GRADUAL_TREND")
    insight=report.summarize("SOL",session(),[event],{})
    assert insight["recent_sell_win_rate_pct"] is None
    assert insight["recent_entry_reason_counts"]["TREND"]==1
    assert insight["recent_trade_sample_is_lifetime"] is False
    assert insight["analysis_status"]=="NO_CLOSED_SELLS"


def test_unstarted_and_unavailable_accounts_never_claim_complete_samples():
    row=report.summarize("SOL",None,[],{})
    assert row["analysis_status"]=="NOT_STARTED"
    assert row["recent_trade_sample_count"]==0
    assert row["recent_sell_stats_complete"] is False
    assert row["market_change_24h_pct"] is None
