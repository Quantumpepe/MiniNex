"""One shared paper strategy used by BTC, ETH and every other active coin."""
from collections import deque
import math

import pytest

from backend import shared_coin_strategy as shared
from backend import extra_paper, user_paper
from backend.auto_engine import PaperPortfolio, PaperAutoEngine


class Market:
    def __init__(self, coin, price=200.0, *, valid=True):
        self.coin = coin
        self.price = price
        self.valid = valid

    def snapshot(self, *, now=None):
        return {"markets": {
            self.coin: {
                "market_data_valid": self.valid,
                "price_usdt": self.price if self.valid else None,
                "reason": "TEST_PRICE" if self.valid else "SOURCE_OFFLINE",
            }
        }}

    def history(self, asset, *, limit=3600):
        assert asset == self.coin
        return []


def holding(asset, *, coin_qty=0.0, cash=500.0):
    return PaperPortfolio(asset=asset, settlement="USDC", strategy_mode="LONG",
                          stable_cash=cash, coin_quantity=coin_qty,
                          cost_basis=coin_qty * 200,
                          entry_price=200.0 if coin_qty else None,
                          peak_price=200.0 if coin_qty else None,
                          initial_equity=500.0,
                          trade_history=deque(maxlen=120))


@pytest.mark.parametrize("asset", shared.COINS)
def test_every_coin_has_same_strategy_decision_route(asset):
    p = holding(asset)
    decision = shared.evaluate_coin_strategy(asset,p,Market(asset),1000.0,"15m")
    assert decision.asset == asset
    assert decision.action in ("WAIT","HOLD","PAPER_BUY","PAPER_SELL")
    assert decision.paper_only is True
    assert decision.live_order_submitted is False
    assert decision.new_paper_trade is None
    assert p.last_evaluated_at == 1000.0
    assert p.coin_quantity == 0
    assert p.stable_cash == 500.0


@pytest.mark.parametrize("asset", shared.COINS)
def test_all_coins_pause_to_their_own_stablecoin(asset):
    p = holding(asset,coin_qty=1.0,cash=100.0)
    market = Market(asset)
    assert shared.exit_coin_to_stable(asset,p,market,1000.0) is True
    assert p.coin_quantity == 0.0
    assert p.stable_cash > 100.0
    assert p.trade_count == 1
    assert p.last_reason == "USER_PAUSE_TO_STABLE"
    assert p.trade_history[-1]["asset"] == asset
    assert p.trade_history[-1]["real_trade"] is False
    assert p.trade_history[-1]["settlement"] == "USDC"


@pytest.mark.parametrize("asset", shared.COINS)
def test_bad_market_never_fabricates_a_sale(asset):
    p = holding(asset,coin_qty=1.0,cash=100.0)
    assert shared.exit_coin_to_stable(asset,p,Market(asset,valid=False),1000.0) is False
    assert p.coin_quantity == 1.0
    assert p.stable_cash == 100.0
    assert p.trade_count == 0


@pytest.mark.parametrize("asset", ["ADA","btc","",None])
def test_unsupported_coin_does_not_access_another_budget(asset):
    with pytest.raises(ValueError):
        shared.evaluate_coin_strategy(asset,holding("BTC"),Market("BTC"),1000.0,"15m")


def test_shared_engine_is_actually_used_by_both_persistence_paths():
    import inspect
    core = inspect.getsource(user_paper.step_user_paper)
    extra = inspect.getsource(extra_paper.tick)
    assert "evaluate_coin_strategy(" in core
    assert "evaluate_coin_strategy(" in extra
    assert "engine.process_asset(" not in core
    assert "engine.process_asset(" not in extra
    assert "exit_coin_to_stable(" in inspect.getsource(user_paper._settle_pending_pause)
    assert "exit_coin_to_stable(" in inspect.getsource(extra_paper._settle_pause)


def test_shared_evaluator_uses_exactly_one_coin_portfolio(monkeypatch):
    p = holding("SOL")
    seen = {}
    def evaluate(self, asset, feed, now):
        seen["assets"] = tuple(self.portfolios)
        seen["asset"] = asset
        seen["price"] = feed["price_usdt"]
        self.portfolios[asset].last_decision = "WAIT"
        self.portfolios[asset].last_reason = "NO_SIGNAL"
    monkeypatch.setattr(PaperAutoEngine,"process_asset",evaluate)
    d=shared.evaluate_coin_strategy("SOL",p,Market("SOL",110.0),10_000.0,"1h")
    assert seen == {"assets":("SOL",),"asset":"SOL","price":110.0}
    assert d.action == "WAIT" and d.reason == "NO_SIGNAL"


def test_invalid_horizon_or_account_mismatch_rejected_before_market_access():
    for asset,p,horizon,clock in [
        ("ETH",holding("BTC"),"15m",1000.0),
        ("BTC",holding("BTC"),"2h",1000.0),
        ("BTC",holding("BTC"),"15m",float("nan")),
    ]:
        with pytest.raises(ValueError):
            shared.evaluate_coin_strategy(asset,p,object(),clock,horizon)
