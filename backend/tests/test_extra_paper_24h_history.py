"""Extended paper-only 24h price windows without sacrificing fast 1m signals."""
from __future__ import annotations

import asyncio
import math

import pytest

from backend import extra_market_data as extra
from backend.extra_market_data import ExtraMarketData, EXTRA_ASSETS
from backend.extra_paper import ExtraPaperMarket, _history_coverage
from backend.auto_engine import PaperAutoEngine, PaperRules
from backend.strategy_replay import EXTRA_REPLAY_ASSETS

NOW=1_800_000_000.0


def candles(interval_seconds=900,count=96,first_price=100.0):
    rows=[]
    for i in range(count):
        close=NOW-(count-i)*interval_seconds-.001
        price=first_price if i==0 else (100 + .01*i)
        rows.append([0,"1","1","1",str(price),"100",
                     int(close*1000)])
    return rows


def make_tick(asset="SOL",price=102):
    return {"data":{"e":"24hrMiniTicker","s":asset+"USDT",
                    "c":str(price),"o":"100","E":int(NOW*1000)}}


def test_one_public_15m_series_covers_the_24h_mode_without_fake_candles():
    svc=ExtraMarketData(clock=lambda:NOW)
    assert svc.ingest_long_candles("SOL",candles(),now=NOW)==96
    assert svc.ingest_candles("SOL",candles(60,90),now=NOW)==90
    assert svc.ingest_ws(make_tick(),now=NOW)
    history=svc.history("SOL",limit=3600)
    assert len(history)==187  # 96 long, 90 minute closes, 1 live tick
    assert history==sorted(history,key=lambda x:x["timestamp"])
    assert all(0 <= NOW-p["timestamp"] <= 86400 for p in history)
    assert any(NOW-p["timestamp"]>3600 for p in history)
    assert any(NOW-p["timestamp"]<=60 for p in history)
    assert svc._long_historical["SOL"][-1]["timestamp"]<NOW
    assert svc.snapshot(now=NOW)["SOL"]["trading_enabled"] is False
    assert "ADA" not in svc._long_historical
    coverage=_history_coverage("SOL","24h",svc)
    assert coverage["status"]=="READY"
    assert coverage["complete"] is True
    assert 1400 < coverage["available_minutes"] <= 1440
    assert coverage["required_minutes"]==1440
    assert coverage["recent_age_seconds"]<=1


def test_dynamic_signal_keeps_short_windows_but_can_inspect_24h_long_history():
    svc=ExtraMarketData(clock=lambda:NOW)
    long=candles(first_price=230)
    svc.ingest_long_candles("SOL",long,now=NOW)
    svc.ingest_candles("SOL",candles(60,90),now=NOW)
    assert svc.ingest_ws(make_tick(),now=NOW)
    wrapper=ExtraPaperMarket(svc)
    long_engine=PaperAutoEngine(wrapper,rules=PaperRules(
        warmup_seconds=0,min_history_samples=4,price_window_seconds=86400))
    short_engine=PaperAutoEngine(wrapper,rules=PaperRules(
        warmup_seconds=0,min_history_samples=4,price_window_seconds=900))
    long_signal=long_engine._signal("SOL",NOW,102)
    short_signal=short_engine._signal("SOL",NOW,102)
    assert long_signal["ready"] is True
    assert short_signal["ready"] is True
    assert long_signal["window_high"]>=230
    assert short_signal["window_high"]<230
    assert math.isfinite(long_signal["momentum_60s_pct"])
    assert math.isfinite(long_signal["momentum_180s_pct"])
    assert math.isfinite(short_signal["momentum_60s_pct"])


def test_missing_long_24h_candles_shows_partial_history_without_blocking_session():
    svc=ExtraMarketData(clock=lambda:NOW)
    svc.ingest_candles("BNB",candles(60,90),now=NOW)
    svc.ingest_ws(make_tick("BNB"),now=NOW)
    report=_history_coverage("BNB","24h",svc)
    assert report["status"]=="PARTIAL"
    assert report["complete"] is False
    assert report["available_minutes"]<100
    assert report["required_minutes"]==1440
    assert _history_coverage("BNB","1h",svc)["status"]=="READY"
    assert _history_coverage("BNB","15m",svc)["status"]=="READY"
    assert _history_coverage("BNB","5m",svc)["status"]=="READY"


def test_invalid_long_candles_do_not_replace_last_valid_history():
    svc=ExtraMarketData(clock=lambda:NOW)
    original=candles()
    svc.ingest_long_candles("DOGE",original,now=NOW)
    before=svc.history("DOGE",limit=3600)
    def broken(mutator):
        bad=[row[:] for row in original]
        mutator(bad)
        with pytest.raises(ValueError):
            svc.ingest_long_candles("DOGE",bad,now=NOW)
        assert svc.history("DOGE",limit=3600)==before
    broken(lambda r:r[-1].__setitem__(6,int((NOW+10)*1000)))
    broken(lambda r:r[-1].__setitem__(4,"nan"))
    broken(lambda r:r[30].__setitem__(6,r[29][6]))
    broken(lambda r:r[20].__setitem__(6,r[18][6]))
    with pytest.raises(ValueError):
        svc.ingest_long_candles("ADA",original,now=NOW)
    with pytest.raises(ValueError):
        svc.ingest_long_candles("BTC",original,now=NOW)
    with pytest.raises(ValueError):
        svc.ingest_long_candles("SOL",original[:7],now=NOW)
    assert len(EXTRA_REPLAY_ASSETS)==9


def test_stale_long_candles_age_out_and_never_pretend_live_price_is_valid():
    svc=ExtraMarketData(clock=lambda:NOW)
    svc.ingest_long_candles("AVAX",candles(),now=NOW)
    assert len(svc.history("AVAX",limit=3600))>0
    # The cached prices can remain for a while, but never qualify as
    # a verified current quote, new transfer or freshly completed candle.
    assert svc.snapshot(now=NOW)["AVAX"]["market_data_valid"] is False
    assert _history_coverage("AVAX","24h",svc)["status"]=="PARTIAL"
    assert all(row["timestamp"]<=NOW for row in svc.history("AVAX",limit=3600))
    svc.clock=lambda:NOW+90000
    assert svc.history("AVAX",limit=3600)==[]
    assert _history_coverage("AVAX","24h",svc)["status"]=="INSUFFICIENT"


def test_paper_fetch_does_not_query_ada_15m_or_authorize_wallet(monkeypatch):
    calls=[]
    class FakeResponse:
        def __init__(self,data):self.data=data
        def raise_for_status(self):return None
        def json(self):return self.data
    class Client:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def get(self,url,*,params):
            assert url==extra.CANDLES_URL
            calls.append((params["symbol"],params["interval"],params["limit"]))
            if params["interval"]=="1m":
                return FakeResponse(candles(60,90))
            return FakeResponse(candles(900,96))
    async def stop_after_one_cycle(seconds):
        assert seconds==300
        raise asyncio.CancelledError()
    monkeypatch.setattr(extra.httpx,"AsyncClient",lambda *a,**k:Client())
    monkeypatch.setattr(extra.asyncio,"sleep",stop_after_one_cycle)
    svc=ExtraMarketData(clock=lambda:NOW)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc._history_loop())
    assert len(calls)==len(EXTRA_ASSETS)+len(EXTRA_REPLAY_ASSETS)
    assert ("ADAUSDT","1m",90) in calls
    assert not any(symbol=="ADAUSDT" and interval=="15m"
                   for symbol,interval,_ in calls)
    assert {symbol for symbol,interval,_ in calls if interval=="15m"}=={
        asset+"USDT" for asset in EXTRA_REPLAY_ASSETS}
    assert all(limit==97 for _,interval,limit in calls if interval=="15m")
    assert svc.snapshot(now=NOW)["SOL"]["market_data_valid"] is False


def test_coverage_outage_reports_unknown_without_invented_history():
    class Down:
        def history(self,asset,*,limit):raise ConnectionError("offline")
    c=_history_coverage("UNI","24h",Down())
    assert c["status"]=="UNAVAILABLE"
    assert c["available_minutes"] is None
    assert c["complete"] is False
