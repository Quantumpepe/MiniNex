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
