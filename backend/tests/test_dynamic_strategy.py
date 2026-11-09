import pytest
"""Regression tests for MiniNex's deliberately non-executing BTC/ETH paper strategy."""
from backend.auto_engine import PaperAutoEngine, PaperPortfolio, PaperRules


class Market:
    def history(self, asset, limit=3600):
        return []

    def snapshot(self, now=None):
        return {"markets": {}}


def signal(*, strong=False):
    return {
        "ready": True, "window_high": 1030.0, "window_low": 990.0,
        "momentum_60s_pct": 0.50 if strong else -0.10,
        "momentum_180s_pct": 0.75 if strong else 0.10,
        "fall_from_window_high_pct": 1.0,
        "rebound_from_window_low_pct": 0.5,
    }


def verified(price):
    return {"market_data_valid": True, "price_usdt": price}


def holding_engine():
    engine = PaperAutoEngine(Market())
    engine.portfolios["BTC"] = PaperPortfolio(
        asset="BTC", settlement="USDT", coin_quantity=1.0,
        cost_basis=900.0, entry_price=900.0, peak_price=1030.0,
        last_trade_at=0.0,
    )
    return engine


def test_strong_uptrend_does_not_sell_on_shallow_pullback():
    engine = holding_engine()
    engine._signal = lambda asset, now, price: signal(strong=True)
    engine.process_asset("BTC", verified(1025.0), now=1000)
    p = engine.portfolios["BTC"]
    assert p.coin_quantity == 1.0
    assert p.trade_count == 0


def test_reversal_trails_net_profit_and_references_exit_price():
    engine = holding_engine()
    engine._signal = lambda asset, now, price: signal()
    engine.process_asset("BTC", verified(1010.0), now=1000)
    p = engine.portfolios["BTC"]
    assert p.coin_quantity == 0.0
    assert p.stable_cash > 0
    assert p.realized_profit > 0
    assert p.last_exit_price == 1010.0
    assert p.post_exit_low == 1010.0
    assert p.trade_history[-1]["reason"] == "DYNAMIC_NET_PROFIT_TRAILING"
    assert p.trade_history[-1]["real_trade"] is False


def test_post_exit_reentry_waits_for_drop_and_then_recovery():
    engine = PaperAutoEngine(Market())
    engine.portfolios["BTC"] = PaperPortfolio(
        asset="BTC", settlement="USDT", stable_cash=1000.0,
        last_exit_price=100.0, post_exit_low=100.0, last_trade_at=0.0,
    )
    engine._signal = lambda asset, now, price: signal()
    engine.process_asset("BTC", verified(98.5), now=1000)
    p = engine.portfolios["BTC"]
    assert p.coin_quantity == 0
    assert p.post_exit_low == 98.5
    engine._signal = lambda asset, now, price: {**signal(), "momentum_60s_pct": 0.30}
    engine.process_asset("BTC", verified(98.9), now=1003)
    assert p.coin_quantity > 0
    assert p.trade_history[-1]["reason"] == "REBOUND"
    assert p.trade_history[-1]["real_trade"] is False
    assert p.last_exit_price is None
    assert p.post_exit_low is None


def test_no_rebuy_before_meaningful_discount():
    engine = PaperAutoEngine(Market())
    engine.portfolios["ETH"] = PaperPortfolio(
        asset="ETH", settlement="USDC", stable_cash=1000.0,
        last_exit_price=2000.0, post_exit_low=2000.0, last_trade_at=0.0,
    )
    engine._signal = lambda asset, now, price: {**signal(), "momentum_60s_pct": 0.30}
    engine.process_asset("ETH", verified(1997.0), now=1000)
    assert engine.portfolios["ETH"].coin_quantity == 0.0
    assert engine.portfolios["ETH"].last_reason == "AWAITING_POST_EXIT_REBOUND"


def test_stale_market_cannot_execute_any_paper_trade():
    engine = holding_engine()
    engine.process_asset("BTC", {"market_data_valid": False, "price_usdt": 1000}, now=1000)
    assert engine.portfolios["BTC"].trade_count == 0
    assert engine.portfolios["BTC"].coin_quantity == 1.0


def test_moderate_steady_uptrend_opens_paper_position_not_only_spikes():
    """A realistic rising BTC market does not need +0.45% in three minutes."""
    class Rising:
        def history(self,asset,limit=3600):
            return [{"timestamp":float(t),
                     "price_usdt":70000.0*(1+0.00001*(t-700))}
                    for t in range(700,1001,10)]
    engine=PaperAutoEngine(Rising(),rules=PaperRules(
        warmup_seconds=0,min_history_samples=4,price_window_seconds=900))
    engine.portfolios["BTC"]=PaperPortfolio(
        asset="BTC",settlement="USDC",stable_cash=500.0)
    price=70000.0*(1+0.00001*300)
    sig=engine._signal("BTC",1000.0,price)
    assert sig["ready"]
    assert sig["momentum_180s_pct"]<0.45
    assert sig["momentum_180s_pct"]>=0.12
    engine.process_asset("BTC",verified(price),now=1000.0)
    p=engine.portfolios["BTC"]
    assert p.last_decision=="PAPER_BUY"
    assert p.last_reason=="TREND_CONTINUATION"
    assert p.coin_quantity>0
    assert p.trade_count==1
    assert p.trade_history[-1]["real_trade"] is False


def test_falling_market_is_not_forced_into_a_purchase():
    engine=PaperAutoEngine(Market())
    engine.portfolios["BTC"]=PaperPortfolio(
        asset="BTC",settlement="USDC",stable_cash=500.0)
    engine._signal=lambda asset,now,price: {
        "ready":True,"window_high":70100.0,"window_low":68900.0,
        "momentum_60s_pct":-0.1,"momentum_180s_pct":-0.3,
        "fall_from_window_high_pct":0.2,"prior_max_drawdown_pct":1.0,
        "rebound_from_window_low_pct":0.3,"recent_mean_price":69000.0,
    }
    engine.process_asset("BTC",verified(69000),now=1000)
    assert engine.portfolios["BTC"].last_reason=="NO_CONFIRMED_ENTRY_SIGNAL"
    assert engine.portfolios["BTC"].trade_count==0


def test_completed_pullback_can_rebound_without_still_being_far_below_high():
    engine=PaperAutoEngine(Market())
    engine.portfolios["ETH"]=PaperPortfolio(
        asset="ETH",settlement="USDC",stable_cash=500.0)
    engine._signal=lambda asset,now,price: {
        "ready":True,"window_high":101.0,"window_low":99.0,
        "momentum_60s_pct":0.15,"momentum_180s_pct":0.05,
        "fall_from_window_high_pct":0.1,
        "prior_max_drawdown_pct":1.4,
        "rebound_from_window_low_pct":0.6,
        "recent_mean_price":100.5,
    }
    engine.process_asset("ETH",verified(100.9),now=1000)
    assert engine.portfolios["ETH"].last_decision=="PAPER_BUY"
    assert engine.portfolios["ETH"].last_reason=="REBOUND"


def test_live_market_data_must_still_be_verified_for_new_trend_entry():
    engine=PaperAutoEngine(Market())
    engine.portfolios["BTC"]=PaperPortfolio(
        asset="BTC",settlement="USDC",stable_cash=500.0)
    engine._signal=lambda asset,now,price:{
        "ready":True,"window_high":100.0,"window_low":99.0,
        "momentum_60s_pct":0.2,"momentum_180s_pct":0.3,
        "fall_from_window_high_pct":0.0,
        "rebound_from_window_low_pct":0.5,
        "recent_mean_price":99.9,
    }
    engine.process_asset("BTC",{"market_data_valid":False,
                                   "reason":"PRIMARY_MISSING_OR_STALE",
                                   "price_usdt":100.0},1000)
    assert engine.portfolios["BTC"].trade_count==0


def test_gentle_ten_minute_uptrend_can_buy_even_without_fast_spike():
    """Slow BTC appreciation should be considered, unlike the old 60s-only rule."""
    class Gradual:
        def history(self,asset,limit=3600):
            return [
                {"timestamp":float(t),
                 "price_usdt":70000*(1+0.0000027*(t-200))}
                for t in range(200,1001,10)
            ]
    engine=PaperAutoEngine(Gradual(),rules=PaperRules(
        warmup_seconds=0,min_history_samples=4,price_window_seconds=900))
    engine.portfolios["BTC"]=PaperPortfolio(
        asset="BTC",settlement="USDC",stable_cash=500.0)
    price=70000*(1+0.0000027*800)
    sig=engine._signal("BTC",1000,price)
    assert sig["ready"]
    assert sig["momentum_600s_pct"]>=0.15
    assert sig["momentum_60s_pct"]<engine.rules.min_trend_60s_pct
    engine.process_asset("BTC",verified(price),now=1000)
    assert engine.portfolios["BTC"].last_decision=="PAPER_BUY"
    assert engine.portfolios["BTC"].last_reason=="GRADUAL_TREND"


def test_negative_short_momentum_rejects_gradual_trend():
    engine=PaperAutoEngine(Market())
    engine.portfolios["BTC"]=PaperPortfolio(
        asset="BTC",settlement="USDC",stable_cash=500.0)
    engine._signal=lambda asset,now,price:{
        "ready":True,"window_high":70020,"window_low":69700,
        "momentum_60s_pct":-0.05,"momentum_180s_pct":0.01,
        "momentum_600s_pct":0.3,"local_high":70020,
        "local_mean":69950,"recent_mean_price":69980,
        "fall_from_window_high_pct":0.02,
        "rebound_from_window_low_pct":0.3,
        "prior_max_drawdown_pct":0.1,
    }
    engine.process_asset("BTC",verified(70000),now=1000)
    assert engine.portfolios["BTC"].trade_count==0
    assert engine.portfolios["BTC"].last_reason=="NO_CONFIRMED_ENTRY_SIGNAL"


def test_post_exit_waits_for_rebound_instead_of_gradual_trend():
    engine=PaperAutoEngine(Market())
    engine.portfolios["BTC"]=PaperPortfolio(
        asset="BTC",settlement="USDC",stable_cash=500.0,
        last_exit_price=69900,post_exit_low=69900,last_trade_at=0)
    engine._signal=lambda asset,now,price:{
        "ready":True,"window_high":70000,"window_low":69400,
        "momentum_60s_pct":0.03,"momentum_180s_pct":0.06,
        "momentum_600s_pct":0.3,"local_high":70000,
        "local_mean":69800,"recent_mean_price":69930,
        "fall_from_window_high_pct":0.02,
        "rebound_from_window_low_pct":0.1,
        "prior_max_drawdown_pct":0.3,
    }
    engine.process_asset("BTC",verified(69980),now=1000)
    assert engine.portfolios["BTC"].trade_count==0
    assert engine.portfolios["BTC"].last_reason=="AWAITING_POST_EXIT_REBOUND"


def test_spot_downtrend_sells_owned_coin_without_leverage():
    engine=PaperAutoEngine(Market())
    engine.portfolios["BTC"]=PaperPortfolio(
        asset="BTC",settlement="USDC",strategy_mode="SPOT_SHORT",
        coin_quantity=0.01,cost_basis=700.0,entry_price=70000,
        peak_price=70000)
    engine._signal=lambda asset,now,price:{
        **signal(),"momentum_60s_pct":-0.13,
        "momentum_180s_pct":-0.25,"recent_mean_price":69900,
    }
    engine.process_asset("BTC",verified(69850),1000)
    p=engine.portfolios["BTC"]
    assert p.coin_quantity==0.0
    assert p.last_sold_quantity==0.01
    assert p.stable_cash>0
    assert p.last_reason=="SPOT_DOWNTREND_EXIT"
    assert p.trade_count==1


def test_spot_downtrend_does_not_sell_on_uptrend():
    engine=PaperAutoEngine(Market())
    engine.portfolios["ETH"]=PaperPortfolio(
        asset="ETH",settlement="USDC",strategy_mode="SPOT_SHORT",
        coin_quantity=0.5,cost_basis=1000.0,entry_price=2000)
    engine._signal=lambda asset,now,price:{
        **signal(strong=True),"recent_mean_price":2000,
    }
    engine.process_asset("ETH",verified(2010),1000)
    assert engine.portfolios["ETH"].coin_quantity==0.5
    assert engine.portfolios["ETH"].last_reason=="SPOT_AWAITING_DOWNTREND"


def test_spot_downtrend_rebuy_requires_more_coins_after_costs():
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="ETH",settlement="USDT",
        strategy_mode="SPOT_SHORT",stable_cash=980.0,
        last_exit_price=2000.0,post_exit_low=1980.0,
        last_sold_quantity=0.5,last_trade_at=0.0)
    engine.portfolios["ETH"]=p
    engine._signal=lambda asset,now,price:{
        **signal(),"momentum_60s_pct":0.15,
    }
    engine.process_asset("ETH",verified(1990),1000)
    assert p.coin_quantity==0
    assert p.last_reason=="AWAITING_POST_EXIT_REBOUND"
    # With a larger genuine discount the budget buys MORE coin after fees.
    p.stable_cash=1010.0
    p.post_exit_low=1970.0
    engine.process_asset("ETH",verified(1980),1100)
    assert p.last_reason=="REBOUND"
    assert p.coin_quantity>0.5
    assert p.last_sold_quantity is None


def test_early_rebound_long_uses_recent_local_low():
    engine=PaperAutoEngine(Market())
    engine.portfolios["BTC"]=PaperPortfolio(
        asset="BTC",settlement="USDC",stable_cash=500.0)
    engine._signal=lambda asset,now,price:{
        "ready":True,"window_high":71000,"window_low":69500,
        "short_window_high":70100,"short_window_low":69700,
        "momentum_60s_pct":0.06,"momentum_180s_pct":-0.01,
        "momentum_600s_pct":-0.1,"fall_from_window_high_pct":1.6,
        "prior_max_drawdown_pct":1.2,"rebound_from_window_low_pct":0.3,
        "recent_mean_price":69790,
    }
    engine.process_asset("BTC",verified(69800),1000)
    assert engine.portfolios["BTC"].last_reason=="REBOUND"


def test_spot_short_does_not_sell_on_unverified_feed():
    engine=PaperAutoEngine(Market())
    engine.portfolios["BTC"]=PaperPortfolio(
        asset="BTC",settlement="USDC",strategy_mode="SPOT_SHORT",
        coin_quantity=0.01,cost_basis=700.0,entry_price=70000)
    engine._signal=lambda asset,now,price:{
        **signal(),"momentum_60s_pct":-0.2,
        "momentum_180s_pct":-0.3,"recent_mean_price":70500,
    }
    engine.process_asset("BTC",{"market_data_valid":False,
        "price_usdt":70000,"reason":"PRIMARY_MISSING_OR_STALE"},1000)
    assert engine.portfolios["BTC"].trade_count==0

def _allocation_signal(*, strong=False, falling=False):
    return {
        "ready": True, "window_high": 103.0, "window_low": 96.0,
        "short_window_high": 102.0, "short_window_low": 97.0,
        "momentum_60s_pct": 0.60 if strong else -0.35 if falling else 0.12,
        "momentum_180s_pct": 0.85 if strong else -0.80 if falling else 0.15,
        "momentum_600s_pct": 0.35, "rebound_from_window_low_pct": 0.32,
        "fall_from_window_high_pct": 0.8, "recent_mean_price": 99.0,
    }


def test_first_buy_reserves_capital_on_cautious_signal_for_both_assets():
    for asset in ("BTC", "ETH"):
        engine=PaperAutoEngine(Market())
        engine.portfolios[asset]=PaperPortfolio(asset=asset,settlement="USDC",stable_cash=5000.0)
        engine._signal=lambda asset,now,price:_allocation_signal()
        engine.process_asset(asset,verified(100.0),1000)
        p=engine.portfolios[asset]
        assert p.trade_count==1
        assert 2000 < p.cost_basis < 4000
        assert 1000 < p.stable_cash < 3000
        assert p.trade_history[-1]["quantity"]==round(p.coin_quantity,12)


def test_confirmed_strong_uptrend_buys_aggressively_but_keeps_cash_reserve():
    for asset in ("BTC", "ETH"):
        engine=PaperAutoEngine(Market())
        engine.portfolios[asset]=PaperPortfolio(asset=asset,settlement="USDC",stable_cash=5000.0)
        engine._signal=lambda asset,now,price:{
            **_allocation_signal(strong=True),"window_high":100.0,"recent_mean_price":99.5}
        engine.process_asset(asset,verified(100.0),1000)
        p=engine.portfolios[asset]
        assert p.trade_count==1
        assert 3000 < p.cost_basis < 4650
        assert 350 < p.stable_cash < 2000
        assert p.cost_basis+p.stable_cash == pytest.approx(5000.0,abs=0.000001)


def test_confirmed_recovery_can_add_to_eth_below_previous_buy():
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="ETH",settlement="USDC",stable_cash=5000.0)
    engine.portfolios["ETH"]=p
    engine._signal=lambda asset,now,price:_allocation_signal()
    engine.process_asset("ETH",verified(100.0),1000)
    qty=p.coin_quantity
    basis=p.cost_basis
    remaining=p.stable_cash
    engine._signal=lambda asset,now,price:{
        **_allocation_signal(),"momentum_60s_pct":0.15,"momentum_180s_pct":-0.20,
        "window_high":100.0,"window_low":97.5,
        "rebound_from_window_low_pct":0.20,"recent_mean_price":97.8}
    engine.process_asset("ETH",verified(98.0),1100)
    assert p.trade_count==2 and p.add_count==1
    assert p.coin_quantity>qty
    assert p.cost_basis>basis and p.stable_cash<remaining
    assert 0 <= p.stable_cash
    assert p.entry_price<100
    assert p.trade_history[-1]["reason"]=="ADD_REBOUND"
    assert p.trade_history[-1]["quantity"]==round(p.coin_quantity-qty,12)
    assert p.trade_history[-1]["position_quantity"]==round(p.coin_quantity,12)


def test_small_decline_is_not_a_forced_exit_or_blind_buy():
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="BTC",settlement="USDC",stable_cash=3000.0,
        coin_quantity=20.0,cost_basis=2000.0,entry_price=100.0,
        last_buy_price=100.0,last_trade_at=0.0)
    engine.portfolios["BTC"]=p
    engine._signal=lambda asset,now,price:{
        **_allocation_signal(falling=True),"recent_mean_price":98.0,
        "rebound_from_window_low_pct":0.0}
    engine.process_asset("BTC",verified(97.5),1000)
    assert p.trade_count==0
    assert p.coin_quantity==20.0 and p.stable_cash==3000.0


def test_severe_downtrend_exits_instead_of_unbounded_averaging():
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="BTC",settlement="USDC",stable_cash=3000.0,
        coin_quantity=20.0,cost_basis=2000.0,entry_price=100.0,last_trade_at=0.0)
    engine.portfolios["BTC"]=p
    engine._signal=lambda asset,now,price:{
        **_allocation_signal(falling=True),"recent_mean_price":98.0,
        "rebound_from_window_low_pct":0.0}
    engine.process_asset("BTC",verified(96.0),1000)
    assert p.last_reason=="RISK_TREND_BREAKDOWN"
    assert p.coin_quantity==0 and p.stable_cash>3000
    assert p.trade_history[-1]["realized_net_profit"]<0


def test_emergency_gap_sells_without_waiting_for_historical_signals():
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="ETH",settlement="USDC",stable_cash=3000.0,
        coin_quantity=20.0,cost_basis=2000.0,entry_price=100.0)
    engine.portfolios["ETH"]=p
    engine._signal=lambda asset,now,price:{"ready":False,"reason":"SIGNAL_HISTORY_STALE"}
    engine.process_asset("ETH",verified(93.0),1000)
    assert p.last_reason=="RISK_EMERGENCY_EXIT"
    assert p.coin_quantity==0


def test_rally_adds_cash_in_stages_without_draining_all_reserves():
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="BTC",settlement="USDC",stable_cash=2800.0,
        coin_quantity=22.0,cost_basis=2200.0,entry_price=100.0,
        last_buy_price=100.0,last_trade_at=0.0)
    engine.portfolios["BTC"]=p
    engine._signal=lambda asset,now,price:{
        **_allocation_signal(strong=True),"window_high":101.0,"recent_mean_price":100.0}
    engine.process_asset("BTC",verified(101.0),1000)
    assert p.last_reason=="ADD_TREND"
    assert p.add_count==1 and 100 < p.stable_cash < 2800
    assert p.coin_quantity>22


def test_post_exit_strong_breakout_can_reenter_without_dip():
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="BTC",settlement="USDC",stable_cash=5000.0,
        last_exit_price=100.0,post_exit_low=100.0,last_trade_at=0.0)
    engine.portfolios["BTC"]=p
    engine._signal=lambda asset,now,price:{
        **_allocation_signal(strong=True),"window_high":100.5,
        "recent_mean_price":100.2,"rebound_from_window_low_pct":0.0}
    engine.process_asset("BTC",verified(100.5),1000)
    assert p.last_reason=="BREAKOUT"
    assert p.coin_quantity>0


def test_flat_bounce_does_not_justify_repeated_buying():
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="ETH",settlement="USDC",stable_cash=3000.0,
        coin_quantity=20.0,cost_basis=2000.0,entry_price=100.0,
        last_buy_price=100.0,last_trade_at=0.0)
    engine.portfolios["ETH"]=p
    engine._signal=lambda asset,now,price:{
        **_allocation_signal(),"momentum_60s_pct":-0.02,
        "momentum_180s_pct":-0.7,"rebound_from_window_low_pct":0.02,
        "recent_mean_price":97.0}
    engine.process_asset("ETH",verified(97.5),1000)
    assert p.trade_count==0
    assert p.last_reason=="TREND_ACTIVE_OR_NET_EXIT_NOT_PROFITABLE"


def _clean_strong_signal(price, *, ten_minute=False, high_volatility=False):
    return {
        "ready":True,
        "window_high":price,
        "window_low":price*(0.90 if high_volatility else 0.99),
        "momentum_60s_pct":0.55,
        "momentum_180s_pct":0.80,
        "momentum_600s_pct":0.60 if ten_minute else None,
        "recent_mean_price":price*0.999,
        "rebound_from_window_low_pct":0.6,
        "fall_from_window_high_pct":0.0,
        "prior_max_drawdown_pct":0.25,
        "short_window_high":price,
        "short_window_low":price*0.99,
    }


def test_short_5m_momentum_cannot_deploy_all_cash_on_one_signal():
    for asset in ("BTC","ETH"):
        engine=PaperAutoEngine(Market())
        p=PaperPortfolio(asset=asset,settlement="USDC",stable_cash=5000)
        engine.portfolios[asset]=p
        engine._signal=lambda asset,now,price:_clean_strong_signal(price)
        engine.process_asset(asset,verified(100),1000)
        assert p.last_decision=="PAPER_BUY"
        assert 3500 < p.cost_basis < 4700
        assert p.stable_cash > 300
        assert p.initial_equity is None  # no false funded-wallet state


def test_high_volatility_reserves_more_even_with_strong_fast_momentum():
    engine=PaperAutoEngine(Market())
    calm=_clean_strong_signal(100)
    turbulent=_clean_strong_signal(100,high_volatility=True)
    calm_target=engine._deployment_target(calm,100,"BREAKOUT")
    turbulent_target=engine._deployment_target(turbulent,100,"BREAKOUT")
    assert 0.70 < turbulent_target < calm_target < 1.0
    assert calm_target-turbulent_target > 0.08


def test_a_second_strong_signal_without_real_price_progress_is_not_an_add():
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="ETH",settlement="USDC",stable_cash=5000)
    engine.portfolios["ETH"]=p
    engine._signal=lambda asset,now,price:_clean_strong_signal(price)
    engine.process_asset("ETH",verified(100),1000)
    initial_qty=p.coin_quantity
    initial_cash=p.stable_cash
    engine.process_asset("ETH",verified(100),1200)
    assert p.trade_count==1
    assert p.add_count==0
    assert p.coin_quantity==initial_qty
    assert p.stable_cash==initial_cash
    assert p.last_reason=="TREND_ACTIVE_OR_NET_EXIT_NOT_PROFITABLE"


def test_progressive_real_rally_uses_three_adds_and_never_overdraws_cash():
    for asset in ("BTC","ETH"):
        engine=PaperAutoEngine(Market())
        p=PaperPortfolio(asset=asset,settlement="USDC",stable_cash=5000)
        engine.portfolios[asset]=p
        engine._signal=lambda asset,now,price:_clean_strong_signal(price,ten_minute=True)
        cash=[]
        for i,price in enumerate((100.0,100.8,101.6,102.4)):
            engine.process_asset(asset,verified(price),1000+i*120)
            cash.append(p.stable_cash)
        assert p.trade_count==4
        assert p.add_count==3
        assert all(cash[i]>cash[i+1] for i in range(3))
        assert cash[-1]>0
        assert p.stable_cash+p.cost_basis > 0
        assert all(t["kind"]=="PAPER_BUY" for t in p.trade_history)
        assert sum(t["total_spent"] for t in p.trade_history)==pytest.approx(5000-cash[-1],abs=0.00001)
        engine.process_asset(asset,verified(103.2),1600)
        assert p.trade_count==4


def test_rebased_500_to_5000_budget_preserves_add_cash_not_100_pct_drain():
    from decimal import Decimal
    from backend.user_paper import _reconcile_virtual_budget
    engine=PaperAutoEngine(Market())
    p=PaperPortfolio(asset="BTC",settlement="USDC",stable_cash=500)
    engine.portfolios["BTC"]=p
    first=engine._buy_quote("BTC",490,100)
    engine._paper_buy(p,first,1000,100,"BREAKOUT")
    assert _reconcile_virtual_budget(p,Decimal("500"),Decimal("5000"))
    assert p.initial_equity==5000
    engine._signal=lambda asset,now,price:_clean_strong_signal(price)
    engine.process_asset("BTC",verified(101),1200)
    assert p.trade_count==2
    assert p.last_reason=="ADD_TREND"
    assert p.stable_cash>200
    assert 0 < p.coin_quantity
    assert p.cost_basis+p.stable_cash==pytest.approx(5000,abs=0.000001)


def test_spot_short_all_in_rebuy_keeps_coin_quantity_comparison():
    engine=PaperAutoEngine(Market())
    # Preserve spot-downtrend semantics: native BTC/ETH -> stable -> more native,
    # rather than applying LONG capital reserve to a full spot-short rebuy.
    p=PaperPortfolio(asset="BTC",settlement="USDC",strategy_mode="SPOT_SHORT",
        stable_cash=5000,last_exit_price=110,post_exit_low=100,
        last_sold_quantity=40.0,last_trade_at=0)
    engine.portfolios["BTC"]=p
    engine._signal=lambda asset,now,price:{
        **_clean_strong_signal(price),"momentum_60s_pct":0.15}
    engine.process_asset("BTC",verified(101),1000)
    assert p.last_decision=="PAPER_BUY"
    assert p.coin_quantity>40
    assert 0 <= p.stable_cash < 5
