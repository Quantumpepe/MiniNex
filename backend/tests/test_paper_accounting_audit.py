"""Accounting and safety invariants for independent BTC/ETH personal PAPER ledgers.

No network, database, Privy wallet, or real settlement is involved.
"""
from __future__ import annotations

from copy import deepcopy
import math

import pytest

from backend.auto_engine import PaperAutoEngine, PaperPortfolio
from backend.user_paper import add_paper_mark_to_market, _state_of, _restore


class PaperFeed:
    def __init__(self, *, valid: bool = True, btc: float = 70_000.0,
                 eth: float = 3_000.0):
        self.valid = valid
        self.prices = {"BTC": btc, "ETH": eth}
        self.requests = 0

    def snapshot(self):
        self.requests += 1
        return {"markets": {
            asset: {"market_data_valid": self.valid, "price_usdt": price}
            for asset, price in self.prices.items()
        }}


def personal(asset: str, cash: float, coin: float, basis: float) -> dict:
    return {"asset": asset, "paper_only": True, "status": "ACTIVE",
            "real_trading_enabled": False, "wallet_funds_used": False,
            "position": {"stable_cash": str(cash),
                         "coin_quantity": str(coin), "cost_basis": str(basis)}}


@pytest.mark.parametrize("asset", ("BTC", "ETH"))
def test_cash_only_equity_survives_offline_market_without_invented_price(asset):
    feed = PaperFeed(valid=False)
    payload = personal(asset, 5_000, 0, 0)
    original = deepcopy(payload)
    valuation = add_paper_mark_to_market(payload, feed)["position"]["valuation"]
    assert valuation["quote_valid"] is True
    assert valuation["price_usdt"] is None
    assert valuation["pricing_basis"] == "PAPER_CASH_ONLY_NO_MARKET_QUOTE"
    assert valuation["estimated_exit_net_usd"] == 0
    assert valuation["estimated_total_equity_usd"] == 5_000
    assert valuation["unrealized_net_pnl_usd"] == 0
    assert valuation["estimated_sale_costs_usd"] == 0
    assert feed.requests == 0  # no price lookup is needed for uninvested cash
    assert payload == original  # read-only, no change in user paper session


@pytest.mark.parametrize("asset", ("BTC", "ETH"))
def test_open_coin_pnl_stays_unavailable_without_verified_market(asset):
    feed = PaperFeed(valid=False)
    payload = personal(asset, 2_000, 0.2, 3_000)
    mark = add_paper_mark_to_market(payload, feed)["position"]["valuation"]
    assert mark["quote_valid"] is False
    assert mark["estimated_total_equity_usd"] is None
    assert mark["unrealized_net_pnl_usd"] is None
    assert feed.requests == 1


@pytest.mark.parametrize("coin,cash,basis", [
    (0, -1, 0), (0, 5_000, 1), (-1, 1_000, 0),
    (0, float("nan"), 0), (0, 1_000, float("inf")),
])
def test_cash_only_never_fabricates_equity_from_corrupt_ledger(coin, cash, basis):
    mark = add_paper_mark_to_market(
        personal("ETH", cash, coin, basis), PaperFeed(valid=False)
    )["position"]["valuation"]
    assert mark["quote_valid"] is False
    assert mark["estimated_total_equity_usd"] is None


@pytest.mark.parametrize("asset,price", [("BTC", 70_000), ("ETH", 3_000)])
def test_multi_buy_and_full_sell_reconcile_to_realized_net_after_every_cost(asset, price):
    """The cash after full liquidation must equal baseline plus realized net P/L."""
    engine = PaperAutoEngine(PaperFeed())
    portfolio = PaperPortfolio(asset=asset, settlement="USDC",
                               stable_cash=5_000.0, initial_equity=5_000.0)
    engine.portfolios[asset] = portfolio
    allocated = [1_500, 800, 700, 450]  # first entry + three permitted adds
    cost_paid = 0.0
    for idx, amount in enumerate(allocated):
        quote = engine._buy_quote(asset, amount, price * (1 - 0.006 * idx))
        assert quote is not None
        assert quote["total_spent"] <= amount
        old_cash = portfolio.stable_cash
        old_qty = portfolio.coin_quantity
        engine._paper_buy(portfolio, quote, 1_700_000_000 + idx * 100,
                          price * (1 - 0.006 * idx),
                          "BREAKOUT" if idx == 0 else "ADD_REBOUND")
        assert portfolio.stable_cash == pytest.approx(old_cash - quote["total_spent"], abs=1e-7)
        assert portfolio.coin_quantity == pytest.approx(old_qty + quote["quantity"], abs=1e-12)
        assert portfolio.cost_basis == pytest.approx(cost_paid + quote["total_spent"], abs=1e-6)
        cost_paid += quote["total_spent"]
    assert portfolio.add_count == 3
    assert portfolio.trade_count == 4
    sale = engine._sell_quote(asset, portfolio.coin_quantity, price * 1.04)
    assert sale is not None
    engine._paper_sell(portfolio, sale, 1_700_002_000, price * 1.04, "DYNAMIC_NET_PROFIT_TRAILING")
    assert portfolio.coin_quantity == 0
    assert portfolio.cost_basis == 0
    assert portfolio.trade_count == 5
    assert portfolio.add_count == 0
    assert portfolio.stable_cash == pytest.approx(
        5_000 + portfolio.realized_profit, abs=1e-6
    )
    assert portfolio.realized_profit == pytest.approx(
        sale["net_proceeds"] - cost_paid, abs=1e-6
    )
    assert portfolio.trade_history[-1]["realized_net_profit"] == pytest.approx(
        round(portfolio.realized_profit, 8), abs=1e-6
    )
    assert all(trade["real_trade"] is False for trade in portfolio.trade_history)
    # No double deduction: an uninvested stable balance stays fully visible.
    view = add_paper_mark_to_market(
        personal(asset, portfolio.stable_cash, 0, 0), PaperFeed(valid=False)
    )["position"]["valuation"]
    assert view["estimated_total_equity_usd"] == pytest.approx(
        round(5_000 + portfolio.realized_profit, 6), abs=1e-6
    )


@pytest.mark.parametrize("asset,price", [("BTC", 65_000), ("ETH", 2_400)])
def test_flat_trade_at_same_market_price_pays_costs_not_fictional_profit(asset, price):
    engine = PaperAutoEngine(PaperFeed())
    portfolio = PaperPortfolio(asset=asset, settlement="USDT",
                               stable_cash=5_000, initial_equity=5_000)
    q = engine._buy_quote(asset, 2_500, price)
    assert q is not None
    engine._paper_buy(portfolio, q, 1_700_000_000, price, "BREAKOUT")
    sale = engine._sell_quote(asset, portfolio.coin_quantity, price)
    assert sale is not None
    engine._paper_sell(portfolio, sale, 1_700_000_200, price, "RISK_EMERGENCY_EXIT")
    assert portfolio.realized_profit < 0
    assert portfolio.stable_cash < 5_000
    assert portfolio.stable_cash == pytest.approx(5_000 + portfolio.realized_profit, abs=1e-7)


def test_separate_assets_survive_state_roundtrips_without_cross_ledger_leaks():
    engine = PaperAutoEngine(PaperFeed())
    engine.portfolios = {
        "BTC": PaperPortfolio(asset="BTC", settlement="USDC",
                              stable_cash=5_000, initial_equity=5_000),
        "ETH": PaperPortfolio(asset="ETH", settlement="USDT",
                              stable_cash=5_000, initial_equity=5_000),
    }
    for asset, price in (("BTC", 70_000), ("ETH", 3_000)):
        p = engine.portfolios[asset]
        q = engine._buy_quote(asset, 1_250, price)
        engine._paper_buy(p, q, 1_700_000_000, price, "BREAKOUT")
    btc = _restore("BTC", "USDC", _state_of(engine.portfolios["BTC"]), 5_000)
    eth = _restore("ETH", "USDT", _state_of(engine.portfolios["ETH"]), 5_000)
    assert btc.asset == "BTC" and eth.asset == "ETH"
    assert btc.settlement == "USDC" and eth.settlement == "USDT"
    assert btc.trade_count == eth.trade_count == 1
    assert btc.coin_quantity < eth.coin_quantity
    btc.stable_cash -= 100
    assert eth.stable_cash == pytest.approx(engine.portfolios["ETH"].stable_cash)


def test_all_eleven_coins_keep_trade_costs_and_histories_independent():
    """Shared decision engine, independent holdings, all-in costs and PnL."""
    from backend.strategy_replay import REPLAY_ASSETS

    assert len(REPLAY_ASSETS) == 11
    engine = PaperAutoEngine(PaperFeed())
    engine.portfolios = {}
    opening = {}

    for index, asset in enumerate(REPLAY_ASSETS):
        capital = float(500 + 100 * index)
        price = float(100 + 75 * index)
        portfolio = PaperPortfolio(
            asset=asset, settlement="USDC" if index % 2 == 0 else "USDT",
            stable_cash=capital, initial_equity=capital,
        )
        engine.portfolios[asset] = portfolio
        quote = engine._buy_quote(asset, 250 + index * 30, price)
        assert quote is not None
        assert 0 < quote["total_spent"] <= capital
        engine._paper_buy(portfolio, quote, 1_700_000_000, price, "BREAKOUT")
        assert portfolio.stable_cash == pytest.approx(
            capital - quote["total_spent"], abs=1e-7,
        )
        assert portfolio.cost_basis == pytest.approx(
            quote["total_spent"], abs=1e-7,
        )
        opening[asset] = deepcopy(_state_of(portfolio))

    # Only alternating coins sell. Every other coin must be bit-for-bit
    # unchanged in its funds, position, profit and persisted state.
    for index, asset in enumerate(REPLAY_ASSETS):
        portfolio = engine.portfolios[asset]
        if index % 2 == 0:
            price = float(100 + 75 * index) * 1.025
            quote = engine._sell_quote(asset, portfolio.coin_quantity, price)
            assert quote is not None
            engine._paper_sell(
                portfolio, quote, 1_700_000_200, price, "DYNAMIC_NET_PROFIT_TRAILING"
            )
            assert portfolio.stable_cash == pytest.approx(
                portfolio.initial_equity + portfolio.realized_profit,
                abs=1e-6,
            )
            assert portfolio.coin_quantity == 0.0
            assert portfolio.cost_basis == 0.0
            assert portfolio.trade_count == 2
        else:
            assert _state_of(portfolio) == opening[asset]
            assert portfolio.trade_count == 1
        assert all(event["asset"] == asset and event["real_trade"] is False
                   for event in portfolio.trade_history)
        assert all(event["settlement"] == portfolio.settlement
                   for event in portfolio.trade_history)
