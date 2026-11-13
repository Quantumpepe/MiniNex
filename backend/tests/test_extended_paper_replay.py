"""Nine independent extension-coin backtests reusing the BTC/ETH spot logic.

No user DB, live order, key, native token approval or transfer is involved.
"""
import math

import pytest
from fastapi.testclient import TestClient

from backend import app as api
from backend.extended_paper_replay import compact_replay_observations
from backend.strategy_replay import (
    EXTRA_REPLAY_ASSETS, REPLAY_ASSETS, replay_strategy,
)
from backend.extra_market_data import EXTRA_ASSETS


def observations(count=115, step=60.0, start=100.0):
    # 1-minute historical candles with a trend, pullback and recovery.
    return [
        {"timestamp": 1_800_000_000.0 + i*step,
         "price_usdt": start * (1 + (0.00045*i if i < 55 else
                                    0.02475 - 0.00065*(i-55) if i < 85 else
                                    0.00525 + 0.00055*(i-85)))}
        for i in range(count)
    ]


def test_replay_coin_roster_keeps_ada_in_progress_and_btc_eth_original():
    assert REPLAY_ASSETS[:2] == ("BTC", "ETH")
    assert EXTRA_REPLAY_ASSETS == tuple(a for a in EXTRA_ASSETS if a != "ADA")
    assert len(EXTRA_REPLAY_ASSETS) == 9
    assert "ADA" not in REPLAY_ASSETS
    assert len(set(REPLAY_ASSETS)) == 11


@pytest.mark.parametrize("coin", EXTRA_REPLAY_ASSETS)
def test_all_nine_share_independent_backtest_and_real_orders_stay_zero(coin):
    result = replay_strategy(coin, "LONG", "15m", observations(),
                             initial_usd=500.0)
    assert result["status"] == "SIMULATED"
    assert result["asset"] == coin
    assert result["paper_only"] is True
    assert result["hypothetical"] is True
    assert result["real_trading_enabled"] is False
    assert result["wallet_funds_used"] is False
    assert result["budget_isolated"] is True
    assert result["orders_executed"] == 0
    assert result["adaptive_trailing_from_past_volatility"] is True
    assert result["cost_model"] == "GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES"
    assert result["sample_count"] == len(observations())
    assert result["estimated_net_return_pct"] <= 500
    assert result["estimated_net_return_pct"] >= -100
    assert "estimated_buy_and_hold_return_pct" in result
    assert "estimated_strategy_minus_hold_pp" in result
    assert all(t["asset"] == coin and t["real_trade"] is False
               for t in result["trades"])


def test_asset_isolation_and_budget_changes_do_not_accumulate_or_rotate():
    rows = observations()
    sol = replay_strategy("SOL", "LONG", "15m", rows, initial_usd=250)
    bnb = replay_strategy("BNB", "LONG", "15m", rows, initial_usd=900)
    again = replay_strategy("SOL", "LONG", "15m", rows, initial_usd=250)
    assert sol == again
    assert sol["initial_usd"] == 250
    assert bnb["initial_usd"] == 900
    assert sol["asset"] == "SOL" and bnb["asset"] == "BNB"
    assert sol["orders_executed"] == bnb["orders_executed"] == 0


def test_spot_downtrend_supported_without_leverage():
    prices = [{"timestamp": 1_800_000_000.0+i*30,
               "price_usdt": 2.0 if i < 22 else 2.0-(i-21)*.005}
              for i in range(65)]
    result = replay_strategy("XRP", "SPOT_SHORT", "15m", prices)
    assert result["status"] == "SIMULATED"
    assert result["orders_executed"] == 0
    assert result["paper_only"] is True
    assert all(t["real_trade"] is False for t in result["trades"])


def test_24h_window_cannot_be_claimed_from_two_hours_of_history():
    result = replay_strategy("AVAX", "LONG", "24h", observations())
    assert result["status"] == "INSUFFICIENT_WINDOW"
    assert result["required_minutes"] == 1440
    assert result["sample_count"] == 115
    assert result["orders_executed"] == 0
    assert result["trades"] == []
    assert result["budget_isolated"] is True


def test_empty_invalid_and_nonmonotonic_extra_candles_fail_closed():
    assert replay_strategy("UNI", "LONG", "15m", [])["status"] == "INSUFFICIENT_SAMPLES"
    with pytest.raises(ValueError):
        replay_strategy("ADA", "LONG", "15m", observations())
    with pytest.raises(ValueError):
        replay_strategy("DOGE", "LONG", "15m", list(reversed(observations())))
    with pytest.raises(ValueError):
        replay_strategy("SOL", "LONG", "15m", observations(), initial_usd=-1)
    with pytest.raises(ValueError):
        replay_strategy("TRX", "LONG", "15m", observations(), initial_usd=float("nan"))


def test_compaction_is_past_only_order_preserving_and_bounded():
    rows = observations(count=70, step=15.0)
    reduced = compact_replay_observations(rows)
    assert len(reduced) == 35
    assert reduced[0] == rows[1] and reduced[-1] == rows[-1]
    assert [p["timestamp"] for p in reduced] == sorted(
        p["timestamp"] for p in reduced)
    prefix = compact_replay_observations(rows[:42])
    assert prefix[:-1] == reduced[:len(prefix)-1]
    assert compact_replay_observations(rows[:2])[-1] == rows[1]
    assert compact_replay_observations([rows[1], rows[0]]) == []
    assert compact_replay_observations([{"timestamp": 1, "price_usdt": math.nan}]) == []
    assert compact_replay_observations([{"timestamp": -1, "price_usdt": 10}]) == []
    assert compact_replay_observations(rows, bucket_seconds=0) == []
    assert compact_replay_observations(rows*100) == []


def test_nine_public_replay_endpoints_use_local_histories_without_order_calls(monkeypatch):
    calls=[]
    source = observations()
    def historical(asset, *, limit=3600):
        calls.append((asset,limit))
        return source
    monkeypatch.setattr(api.extra_markets, "history", historical)
    client = TestClient(api.app)
    for symbol in EXTRA_REPLAY_ASSETS:
        response = client.get(f"/api/strategy/replay/{symbol}",
                              params={"initial_usd": 1234,"mode":"LONG",
                                      "horizon":"15m"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body=response.json()
        assert body["asset"] == symbol
        assert body["initial_usd"] == 1234
        assert body["real_trading_enabled"] is False
        assert body["wallet_funds_used"] is False
        assert body["orders_executed"] == 0
    assert len(calls) == 9
    assert all(limit == 3600 for _,limit in calls)


def test_invalid_replay_requests_do_not_touch_the_market(monkeypatch):
    calls=[]
    def historical(asset, *, limit=3600):
        calls.append(asset)
        return observations()
    monkeypatch.setattr(api.extra_markets, "history", historical)
    client=TestClient(api.app)
    for suffix in (
        "/api/strategy/replay/ADA",
        "/api/strategy/replay/JAM",
        "/api/strategy/replay/SOL?initial_usd=0",
        "/api/strategy/replay/SOL?initial_usd=5001",
        "/api/strategy/replay/SOL?initial_usd=nan",
        "/api/strategy/replay/SOL?mode=LEVERAGED_SHORT",
        "/api/strategy/replay/SOL?horizon=2d",
    ):
        response=client.get(suffix)
        assert response.status_code == 422
    assert not calls
