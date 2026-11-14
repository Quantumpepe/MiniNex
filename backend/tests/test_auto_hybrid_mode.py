"""AUTO = existing LONG + DCA and defensive owned-coin spot exits.

One hybrid mode across 11 coins; no leverage, real order or cross-coin capital.
"""
import pytest
from backend.auto_engine import PaperAutoEngine, PaperPortfolio
from backend.shared_coin_strategy import COINS, evaluate_coin_strategy
from backend.strategy_replay import MODES as REPLAY_MODES
from backend.trade_preferences import MODES as USER_MODES
from backend.storage import SCHEMA
from backend import app as api


class OneAssetMarket:
    def __init__(self, asset, price):
        self.asset, self.price = asset, price

    def snapshot(self, *, now=None):
        return {"markets": {self.asset: {
            "market_data_valid": True, "price_usdt": self.price,
        }}}

    def history(self, asset, *, limit=3600):
        assert asset == self.asset
        return []


def signals(*, falling=True):
    base = {
        "ready": True,
        "window_high": 206.0, "window_low": 194.0,
        "short_window_high": 202.0, "short_window_low": 195.0,
        "recent_mean_price": 202.0 if falling else 195.0,
        "local_mean": 195.0, "local_high": 199.0,
        "prior_max_drawdown_pct": 4.0,
        "fall_from_window_high_pct": 4.0,
        "rebound_from_window_low_pct": 0.0 if falling else 0.5,
        "momentum_60s_pct": -0.4 if falling else 0.2,
        "momentum_180s_pct": -0.8 if falling else 0.3,
        "momentum_600s_pct": -0.5 if falling else 0.4,
    }
    return base


def portfolio(asset, mode="AUTO", *, entry=160.0, peak=200.9):
    return PaperPortfolio(
        asset=asset, settlement="USDC", strategy_mode=mode,
        stable_cash=100.0, coin_quantity=0.5, cost_basis=entry*0.5,
        initial_equity=180.0, entry_price=entry, peak_price=peak,
    )


def test_mode_supported_in_api_replay_preferences_and_real_database_upgrade():
    assert "AUTO" in USER_MODES and "AUTO" in REPLAY_MODES
    assert len(COINS) == 11 and "ADA" not in COINS
    assert api.SharedStrategyProfileRequest(
        mode="AUTO", horizon="15m", expected_revision=0).mode == "AUTO"
    assert api.ExtraPaperStartRequest(
        budget_usd=500, mode="AUTO", stable="USDC", horizon="15m").mode == "AUTO"
    ddl = "\n".join(SCHEMA)
    for name in (
        "mininex_unified_strategy_profiles_mode_check",
        "mininex_trade_preferences_mode_check",
        "mininex_user_paper_sessions_strategy_mode_check",
        "mininex_extra_paper_sessions_strategy_mode_check",
    ):
        assert f"ADD CONSTRAINT {name}" in ddl
    assert ddl.count("CHECK (mode IN ('LONG','SPOT_SHORT','AUTO'))") >= 2
    assert ddl.count("CHECK (strategy_mode IN ('LONG','SPOT_SHORT','AUTO'))") >= 2


@pytest.mark.parametrize("asset", COINS)
def test_auto_defensive_spot_sale_only_owned_coin_after_confirmed_drop(asset, monkeypatch):
    monkeypatch.setattr(PaperAutoEngine, "_signal",
                        lambda self, name, at, price: signals())
    p = portfolio(asset)
    result = evaluate_coin_strategy(asset, p, OneAssetMarket(asset,200.0),1000.0,"15m")
    assert result.asset == asset
    assert result.action == "PAPER_SELL"
    assert result.reason == "AUTO_DOWNTREND_EXIT"
    assert result.new_paper_trade["asset"] == asset
    assert result.new_paper_trade["kind"] == "PAPER_SELL"
    assert result.new_paper_trade["real_trade"] is False
    assert result.live_order_submitted is False
    assert p.coin_quantity == 0
    assert p.last_sold_quantity == 0.5
    assert p.stable_cash > 100.0

    # Same weak market with LONG chosen does not use AUTO defensive exit.
    base = portfolio(asset, mode="LONG")
    compared = evaluate_coin_strategy(asset,base,OneAssetMarket(asset,200.0),1000.0,"15m")
    assert compared.action == "HOLD"
    assert base.coin_quantity == 0.5


@pytest.mark.parametrize("asset", COINS)
def test_auto_small_loss_is_not_automatically_dumped_on_minor_decline(asset, monkeypatch):
    monkeypatch.setattr(PaperAutoEngine, "_signal",
                        lambda self, name, at, price: signals())
    p=portfolio(asset,entry=202.0,peak=202.0)
    decision=evaluate_coin_strategy(asset,p,OneAssetMarket(asset,200.0),1000.0,"15m")
    assert decision.action == "HOLD"
    assert decision.new_paper_trade is None
    assert p.coin_quantity == 0.5
    assert p.trade_count == 0


@pytest.mark.parametrize("asset", COINS)
def test_auto_rebuys_after_discount_and_confirmed_recovery(asset, monkeypatch):
    monkeypatch.setattr(PaperAutoEngine, "_signal",
                        lambda self, name, at, price: signals(falling=True))
    p=portfolio(asset)
    first=evaluate_coin_strategy(asset,p,OneAssetMarket(asset,200.0),1000.0,"15m")
    assert first.action=="PAPER_SELL"
    p.post_exit_low=195.0
    monkeypatch.setattr(PaperAutoEngine, "_signal",
                        lambda self, name, at, price: signals(falling=False))
    second=evaluate_coin_strategy(asset,p,OneAssetMarket(asset,196.0),1200.0,"15m")
    assert second.action=="PAPER_BUY"
    assert second.reason=="REBOUND"
    assert second.new_paper_trade["asset"]==asset
    assert second.new_paper_trade["real_trade"] is False
    assert p.coin_quantity>0 and p.stable_cash>=0
    assert p.trade_count==2


def test_auto_never_uses_or_changes_a_different_coin_portfolio(monkeypatch):
    monkeypatch.setattr(PaperAutoEngine, "_signal",
                        lambda self, name, at, price: signals())
    btc=portfolio("BTC")
    eth=portfolio("ETH")
    previous=(eth.coin_quantity,eth.stable_cash,eth.trade_count)
    evaluate_coin_strategy("BTC",btc,OneAssetMarket("BTC",200.0),1000.0,"15m")
    assert (eth.coin_quantity,eth.stable_cash,eth.trade_count)==previous
