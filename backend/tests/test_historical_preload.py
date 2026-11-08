"""Real Binance candle bootstrap: no fake history or session timer."""
import pytest

from backend.market_data import MarketDataService
from backend.auto_engine import PaperAutoEngine, PaperRules

NOW=1_800_000_000.0


def candles(interval, count, *, now=NOW, base=70000.0):
    # Strictly closed bars; last close is around 1-2 minutes old.
    end=int((now-65)//interval)*interval
    start=end-count*interval
    return [
        [
            (start+i*interval)*1000,
            str(base+float(i)*0.025), str(base+float(i)*0.025+10),
            str(base+float(i)*0.025-10),str(base+float(i)*0.025),
            "10",
            (start+(i+1)*interval)*1000-1,
            "100",50,"4","40","0",
        ] for i in range(count)
    ]


def test_real_candles_bootstrap_every_horizon_without_wait():
    market=MarketDataService(clock=lambda: NOW)
    assert market.ingest_historical_candles(
        "BTC",candles(60,240),candles(300,300),now=NOW)>400
    assert market.ingest_historical_candles(
        "ETH",candles(60,240,base=3500),candles(300,300,base=3500),now=NOW)>400
    assert market.history_loaded_at["BTC"]==NOW
    assert market.history_loaded_at["ETH"]==NOW
    assert market.history("BTC",limit=3600)[-1]["timestamp"]<=NOW
    assert market.ingest_primary_rest([
        {"symbol":"BTCUSDT","price":"70006.0"},
        {"symbol":"ETHUSDT","price":"3506.0"}
    ],now=NOW)
    assert market.ingest_reference({"error":[],"result":{
        "XXBTZUSD":{"b":["70005"],"a":["70007"]},
        "XETHZUSD":{"b":["3505"],"a":["3507"]},
    }},now=NOW)
    engine=PaperAutoEngine(market,rules=PaperRules(
        warmup_seconds=0,min_history_samples=4))
    for seconds in (300,900,3600,86400):
        engine.rules=PaperRules(
            warmup_seconds=0,min_history_samples=4,
            price_window_seconds=seconds)
        signals=engine._signal("BTC",NOW,70006.0)
        assert signals["ready"],(seconds,signals)


def test_rejects_partial_latest_candle_and_leaves_last_good_history():
    market=MarketDataService(clock=lambda: NOW)
    one=candles(60,240)
    five=candles(300,300)
    original=market.ingest_historical_candles("BTC",one,five,now=NOW)
    invalid=[list(row) for row in one]
    invalid[-1][6]=int((NOW+60)*1000)
    with pytest.raises(ValueError):
        market.ingest_historical_candles("BTC",invalid,five,now=NOW)
    assert len(market._historical["BTC"])==original


def test_rejects_fabricated_stale_or_short_history():
    market=MarketDataService(clock=lambda: NOW)
    one=candles(60,240)
    five=candles(300,300)
    cases=[
        (one[:2],five),
        (one,five[:20]),
        (one,[*five[:-1],five[-2]]),
        (one,[]),
        (one[:-1]+[one[-1][:4]],five),
    ]
    for a,b in cases:
        with pytest.raises(ValueError):
            market.ingest_historical_candles("BTC",a,b,now=NOW)
    assert market._historical["BTC"]==[]


def test_no_unsafe_buy_when_historical_feed_lacks_current_verified_price():
    market=MarketDataService(clock=lambda: NOW)
    market.ingest_historical_candles("BTC",candles(60,240),candles(300,300))
    paper=PaperAutoEngine(market,rules=PaperRules(
        warmup_seconds=0,min_history_samples=4,
        price_window_seconds=900))
    paper.process_asset("BTC",market.snapshot(now=NOW)["markets"]["BTC"],NOW)
    assert paper.portfolios["BTC"].trade_count==0
    assert paper.portfolios["BTC"].last_reason.startswith("MARKET_DATA_NOT_VERIFIED")
