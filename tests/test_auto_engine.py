"""Deterministic paper simulations. All tests run without network or actual orders."""
import math
import time
from fastapi.testclient import TestClient
from backend.auto_engine import PaperAutoEngine

T = 1762592400.0


class FakeMarket:
    def __init__(self):
        self.prices = {"BTC": 100000.0, "ETH": 3000.0}
        self.valid = {"BTC": True, "ETH": True}
        self.logs = {"BTC": [], "ETH": []}

    def set_tape(self, asset, prices, step=10, start=T):
        self.logs[asset] = [{"timestamp": start + i * step, "price_usdt": float(p)} for i, p in enumerate(prices)]
        self.prices[asset] = float(prices[-1])
        return start + (len(prices) - 1) * step

    def history(self, asset, limit=3600):
        return self.logs[asset][-limit:]

    def snapshot(self, now=None):
        return {"markets": {a: {"price_usdt": self.prices[a],
                                "market_data_valid": self.valid[a],
                                "reason": "OK" if self.valid[a] else "PRICE_DIVERGENCE"}
                            for a in ("BTC", "ETH")}}


def test_all_in_budget_includes_buy_cost_and_exit_gas():
    e = PaperAutoEngine(FakeMarket())
    q = e._buy_quote("BTC", 1000.0, 100000.0)
    assert q["total_spent"] < 1000.0
    assert 1000 - q["total_spent"] >= e.costs.network("BTC") - 1e-6
    assert math.isclose(q["total_spent"] + q["reserved_for_future_exit_network"], 1000.0, abs_tol=1e-6)
    assert q["quantity"] * q["effective_unit_price"] == q["gross"]


def test_small_all_in_budget_cannot_buy():
    e = PaperAutoEngine(FakeMarket())
    assert e._buy_quote("ETH", 5, 3000) is None
    assert e._buy_quote("BTC", 25, 100000) is None


def test_paper_buy_then_sell_fees_counted_in_actual_net_profit():
    e = PaperAutoEngine(FakeMarket())
    p = e.portfolios["BTC"]
    buy = e._buy_quote("BTC", p.stable_cash, 100000)
    e._paper_buy(p, buy, T, 100000, "REBOUND")
    assert p.coin_quantity > 0 and p.stable_cash >= 2.5 - 1e-8
    assert p.cost_basis == buy["total_spent"]
    sell = e._sell_quote("BTC", p.coin_quantity, 102000)
    expected_profit = sell["net_proceeds"] - buy["total_spent"]
    e._paper_sell(p, sell, T + 200, 102000, "NET_PROFIT_TRAILING")
    assert math.isclose(p.realized_profit, expected_profit, abs_tol=1e-8)
    assert math.isclose(p.stable_cash - 1000, p.realized_profit, abs_tol=1e-8)
    assert p.trade_history[-1]["realized_net_profit"] > 0
    assert p.trade_history[-1]["real_trade"] is False


def test_buying_and_selling_flat_price_generates_net_loss_not_fake_profit():
    e = PaperAutoEngine(FakeMarket())
    p = e.portfolios["BTC"]
    b = e._buy_quote("BTC", 1000, 100000)
    e._paper_buy(p, b, T, 100000, "BREAKOUT")
    s = e._sell_quote("BTC", p.coin_quantity, 100000)
    assert s["net_proceeds"] - p.cost_basis < 0


def test_native_eth_start_valued_on_first_verified_price_only():
    m = FakeMarket()
    e = PaperAutoEngine(m)
    p = e.portfolios["ETH"]
    assert p.initial_equity is None and p.coin_quantity == 0.25
    m.valid["ETH"] = False
    e.process_asset("ETH", m.snapshot()["markets"]["ETH"], T)
    assert p.initial_equity is None
    m.valid["ETH"] = True
    e.process_asset("ETH", m.snapshot()["markets"]["ETH"], T)
    assert p.initial_equity == 750.0 and p.cost_basis == 750.0
    assert p.last_reason == "INSUFFICIENT_HISTORY"


def test_market_data_fail_closed_even_with_positive_history():
    m = FakeMarket()
    e = PaperAutoEngine(m)
    now = m.set_tape("BTC", [100000 + i * 100 for i in range(61)])
    m.valid["BTC"] = False
    e.process_asset("BTC", m.snapshot()["markets"]["BTC"], now)
    assert e.portfolios["BTC"].trade_count == 0
    assert "MARKET_DATA_NOT_VERIFIED" in e.portfolios["BTC"].last_reason


def test_initial_warmup_prevents_early_trade():
    m = FakeMarket()
    e = PaperAutoEngine(m)
    now = m.set_tape("BTC", [100000 + i * 100 for i in range(12)])
    e.process_asset("BTC", m.snapshot()["markets"]["BTC"], now)
    assert e.portfolios["BTC"].last_reason == "INSUFFICIENT_HISTORY"


def test_confirmed_rebound_buys_only_btc():
    m = FakeMarket()
    e = PaperAutoEngine(m)
    series = [100000.0] * 20 + [99500.0] * 10 + [98500.0] * 18 + [98600, 98700, 98800, 98900, 99000]
    now = m.set_tape("BTC", series)
    e.process_asset("BTC", m.snapshot()["markets"]["BTC"], now)
    assert e.portfolios["BTC"].last_decision == "PAPER_BUY"
    assert e.portfolios["BTC"].last_reason == "REBOUND"
    assert e.portfolios["ETH"].trade_count == 0


def test_price_increase_then_pullback_sells_at_net_profit():
    m = FakeMarket()
    e = PaperAutoEngine(m)
    p = e.portfolios["BTC"]
    b = e._buy_quote("BTC", p.stable_cash, 100000)
    e._paper_buy(p, b, T, 100000, "BREAKOUT")
    baseline = [100000.] * 20 + [101000.] * 15 + [103000.] * 15
    at = m.set_tape("BTC", baseline)
    e.process_asset("BTC", m.snapshot()["markets"]["BTC"], at)
    assert p.coin_quantity > 0
    # Price still profitable but pulled back 0.5% from the intra-position peak.
    m.logs["BTC"].append({"timestamp": at + 10, "price_usdt": 102480.0})
    m.prices["BTC"] = 102480.0
    e.process_asset("BTC", m.snapshot()["markets"]["BTC"], at + 10)
    assert p.coin_quantity == 0
    assert p.last_reason == "NET_PROFIT_TRAILING"
    assert p.realized_profit > 0


def test_risk_exit_is_labelled_even_if_loss():
    m = FakeMarket()
    e = PaperAutoEngine(m)
    p = e.portfolios["BTC"]
    e._paper_buy(p, e._buy_quote("BTC", 1000, 100000), T, 100000, "BREAKOUT")
    now = m.set_tape("BTC", [100000.0] * 30 + [99500.0] * 13 + [97000.0] * 14)
    e.process_asset("BTC", m.snapshot()["markets"]["BTC"], now)
    assert p.last_reason == "RISK_EXIT_MAY_REALIZE_LOSS"
    assert p.realized_profit < 0
    assert p.trade_history[-1]["realized_net_profit"] < 0


def test_market_snapshot_never_exposes_user_funds_or_live_orders():
    e = PaperAutoEngine(FakeMarket())
    s = e.status()
    assert s["real_trading_enabled"] is False
    assert s["wallet_connected"] is False
    assert s["user_balances"] is False
    assert s["data_persistent"] is False
    assert set(s["portfolios"]) == {"BTC", "ETH"}


def test_api_read_only_routes_and_health_survive_v03():
    from backend.app import app
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").json()["trading"] == "disabled"
    assert client.get("/api/paper/status").json()["real_trading_enabled"] is False
    assert client.get("/api/paper/status/DOGE").status_code == 404
    assert client.get("/api/paper/trades/ETH").json()["trades"] == []
    assert client.post("/api/paper/status").status_code == 405
    assert client.get("/api/market/prices").status_code == 200
