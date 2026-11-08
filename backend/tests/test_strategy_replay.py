"""Historical strategy replay never affects user accounts, wallets or execution."""
import pytest
from fastapi.testclient import TestClient

from backend.strategy_replay import replay_strategy
from backend.app import app


def history(prices,step=15.0):
    return [{"timestamp":100000.0+i*step,"price_usdt":float(p)}
            for i,p in enumerate(prices)]


def test_replay_trends_are_hypothetical_and_never_real_orders():
    # Rise, then a clear reversal: a trade may occur but is not forced.
    prices=[70000*(1+i*0.00014) for i in range(75)]
    prices.extend(prices[-1]*(1-i*0.0004) for i in range(1,26))
    result=replay_strategy("BTC","LONG","15m",history(prices))
    assert result["status"]=="SIMULATED"
    assert result["paper_only"] is True
    assert result["hypothetical"] is True
    assert result["orders_executed"]==0
    assert result["trade_count"]>=1
    assert any(e["kind"]=="PAPER_BUY" for e in result["trades"])
    assert all(e["real_trade"] is False for e in result["trades"])
    assert result["estimated_final_liquidation_usd"]>=0


def test_spot_downtrend_history_can_sell_owned_coins():
    prices=[100.0]*22+[100.0-i*.07 for i in range(1,21)]
    result=replay_strategy("ETH","SPOT_SHORT","5m",history(prices))
    assert result["status"]=="SIMULATED"
    assert result["orders_executed"]==0
    assert result["sell_count"]>=1
    assert result["trades"][0]["reason"]=="SPOT_DOWNTREND_EXIT"


def test_replay_looks_only_at_past_samples_not_future():
    prices=[100.0]*22+[100+i*.03 for i in range(1,31)]
    points=history(prices)
    a=replay_strategy("BTC","LONG","5m",points[:30])
    b=replay_strategy("BTC","LONG","5m",points)
    assert a["sample_count"]==30
    assert b["sample_count"]==len(points)
    assert a["to_timestamp"]<b["to_timestamp"]
    # The longer replay must reproduce every prior trade identically.
    # Decisions at earlier timestamps cannot depend on future samples.
    prefix_events = [
        e for e in b["trades"]
        if __import__("datetime").datetime.fromisoformat(e["at"]).timestamp()
        <= a["to_timestamp"]
    ]
    assert prefix_events == a["trades"]


def test_invalid_replay_data_rejected():
    with pytest.raises(ValueError):
        replay_strategy("SOL","LONG","15m",[])
    with pytest.raises(ValueError):
        replay_strategy("BTC","LEVERAGED_SHORT","15m",[])
    with pytest.raises(ValueError):
        replay_strategy("BTC","LONG","36h",[])
    with pytest.raises(ValueError):
        replay_strategy("BTC","LONG","15m",history([100,0,120,150,140]))
    with pytest.raises(ValueError):
        replay_strategy("BTC","LONG","15m",list(reversed(history([100,101,102,103,104]))))
    with pytest.raises(ValueError):
        replay_strategy("BTC","LONG","15m",history([100]*3601))


def test_insufficient_history_does_not_claim_trades():
    r=replay_strategy("ETH","LONG","5m",history([100,101]))
    assert r["status"]=="INSUFFICIENT_SAMPLES"
    assert r["orders_executed"]==0
    assert r["trades"]==[]


def test_replay_endpoint_rejects_invalid_query_without_mutating():
    client=TestClient(app)
    r=client.get("/api/strategy/replay/BTC?mode=LEVERAGED_SHORT")
    assert r.status_code==422
    r=client.get("/api/strategy/replay/SOL")
    assert r.status_code==422
    r=client.get("/api/strategy/replay/BTC")
    assert r.status_code==200
    data=r.json()
    assert data["paper_only"] is True
    assert data["orders_executed"]==0
