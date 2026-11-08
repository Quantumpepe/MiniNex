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
