"""Independent Bitstamp fallback prevents a Kraken 503 from blanking PAPER quotes.

No simulated trade occurs without two independently sourced, fresh, closely
matching spot prices. Fallback never impersonates a Binance primary.
"""
import asyncio

import httpx
import pytest

from backend import market_data as md

NOW = 1_800_000_000.0


def ticker(price, at=NOW):
    return {
        "bid":str(price-0.5),
        "ask":str(price+0.5),
        "timestamp":str(int(at)),
        "last":"not used",
        "market_type":"SPOT",
    }


@pytest.mark.parametrize("bad", [
    {}, [], {"bid":"NaN","ask":"1","timestamp":str(int(NOW))},
    {"bid":"0","ask":"1","timestamp":str(int(NOW))},
    {"bid":"101","ask":"100","timestamp":str(int(NOW))},
    {"bid":"10","ask":"20","timestamp":str(int(NOW))},
    ticker(70000,NOW-92), ticker(70000,NOW+7),
    {"bid":"10","ask":"11","timestamp":"not a time"},
    {"bid":"10","ask":"11","timestamp":True},
])
def test_reject_stale_invalid_crossed_or_absurd_bitstamp_reference(bad):
    assert md.parse_bitstamp_ticker(bad,now=NOW) is None


def test_independent_bitstamp_fallback_verifies_fresh_binance_for_both_assets():
    market=md.MarketDataService(clock=lambda:NOW)
    assert market.ingest_primary_rest_asset({"symbol":"BTCUSDT","price":"70000"},now=NOW)
    assert market.ingest_primary_rest_asset({"symbol":"ETHUSDT","price":"3500"},now=NOW)
    assert market.snapshot(now=NOW)["markets"]["BTC"]["reason"]=="REFERENCE_MISSING_OR_STALE"
    assert market.ingest_bitstamp_reference("BTC",ticker(70000))
    assert market.ingest_bitstamp_reference("ETH",ticker(3500))
    snap=market.snapshot(now=NOW)
    assert snap["markets"]["BTC"]["reference_source"]=="Bitstamp Spot"
    assert snap["markets"]["ETH"]["reference_source"]=="Bitstamp Spot"
    assert snap["markets"]["BTC"]["market_data_valid"] is True
    assert snap["markets"]["ETH"]["market_data_valid"] is True
    assert snap["markets"]["BTC"]["reason"]==snap["markets"]["ETH"]["reason"]=="OK"
    assert snap["trading_enabled"] is False
    assert snap["execution_enabled"] is False


def test_kraken_recovers_and_reclaims_primary_independent_reference():
    market=md.MarketDataService(clock=lambda:NOW)
    assert market.ingest_bitstamp_reference("BTC",ticker(70000,at=NOW),now=NOW)
    assert market.ingest_reference({"error":[],"result":{
        "XXBTZUSD":{"b":["70000"],"a":["70002"]}
    }},now=NOW+15)
    assert market.snapshot(now=NOW+15)["markets"]["BTC"]["reference_source"]=="Kraken Spot"


def test_fallback_quote_divergence_or_stale_age_remains_fail_closed():
    market=md.MarketDataService(clock=lambda:NOW)
    market.ingest_primary_rest_asset({"symbol":"BTCUSDT","price":"70000"},now=NOW)
    assert market.ingest_bitstamp_reference("BTC",ticker(74000),now=NOW)
    assert market.snapshot(now=NOW)["markets"]["BTC"]["reason"]=="PRICE_DIVERGENCE"
    assert not market.snapshot(now=NOW)["markets"]["BTC"]["market_data_valid"]
    # A valid match still expires after reference deadline; don't keep
    # trading on stale quotes merely because the fallback used to work.
    assert market.ingest_bitstamp_reference("BTC",ticker(70000),now=NOW+10)
    market.ingest_primary_rest_asset({"symbol":"BTCUSDT","price":"70000"},now=NOW+100)
    assert market.snapshot(now=NOW+101)["markets"]["BTC"]["reason"]=="REFERENCE_MISSING_OR_STALE"


def test_bitstamp_failure_is_independent_per_asset():
    market=md.MarketDataService(clock=lambda:NOW)
    market.ingest_primary_rest_asset({"symbol":"BTCUSDT","price":"70000"},now=NOW)
    market.ingest_primary_rest_asset({"symbol":"ETHUSDT","price":"3500"},now=NOW)
    assert not market.ingest_bitstamp_reference("BTC",ticker(70000,NOW-100),now=NOW)
    assert market.ingest_bitstamp_reference("ETH",ticker(3500),now=NOW)
    s=market.snapshot(now=NOW)["markets"]
    assert s["BTC"]["market_data_valid"] is False
    assert s["BTC"]["reason"]=="REFERENCE_MISSING_OR_STALE"
    assert s["ETH"]["market_data_valid"] is True
    assert market.ingest_bitstamp_reference("BTC",ticker(70000),now=NOW)
    assert market.snapshot(now=NOW)["markets"]["BTC"]["market_data_valid"] is True


def test_reference_loop_survives_kraken_503_with_real_independent_tickers(monkeypatch):
    market=md.MarketDataService(clock=lambda:NOW)
    market.ingest_primary_rest_asset({"symbol":"BTCUSDT","price":"70000"},now=NOW)
    market.ingest_primary_rest_asset({"symbol":"ETHUSDT","price":"3500"},now=NOW)
    visited=[]
    class Response:
        def __init__(self,body,fail=False):self.body=body;self.fail=fail
        def raise_for_status(self):
            if self.fail:
                raise httpx.HTTPStatusError("503 Service Unavailable",
                    request=httpx.Request("GET",md.DEFAULT_REFERENCE_URL),
                    response=httpx.Response(503))
        def json(self):return self.body
    class Client:
        def __init__(self,**kwargs):
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kwargs):
            visited.append(url)
            if url==md.DEFAULT_REFERENCE_URL:
                return Response({},True)
            if url==md.DEFAULT_BITSTAMP_TICKER_URL+"/btcusd/":
                return Response(ticker(70000))
            if url==md.DEFAULT_BITSTAMP_TICKER_URL+"/ethusd/":
                return Response(ticker(3500))
            raise AssertionError("Unexpected reference source: "+url)
    async def terminate_after_one_iteration(_):
        raise asyncio.CancelledError()
    monkeypatch.setattr(md.httpx,"AsyncClient",Client)
    monkeypatch.setattr(md.asyncio,"sleep",terminate_after_one_iteration)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(market._reference_loop())
    assert len(visited)==3
    assert market.last_reference_error is None
    assert all(v["market_data_valid"] for v in market.snapshot(now=NOW)["markets"].values())
    assert all(v["reference_source"]=="Bitstamp Spot"
               for v in market.snapshot(now=NOW)["markets"].values())


def test_double_outage_does_not_fabricate_valid_market_data(monkeypatch):
    market=md.MarketDataService(clock=lambda:NOW)
    market.ingest_primary_rest_asset({"symbol":"BTCUSDT","price":"70000"},now=NOW)
    market.ingest_primary_rest_asset({"symbol":"ETHUSDT","price":"3500"},now=NOW)
    class Response:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("503",
                request=httpx.Request("GET",md.DEFAULT_REFERENCE_URL),
                response=httpx.Response(503))
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kwargs):return Response()
    async def one(_):raise asyncio.CancelledError()
    monkeypatch.setattr(md.httpx,"AsyncClient",Client)
    monkeypatch.setattr(md.asyncio,"sleep",one)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(market._reference_loop())
    assert market.last_reference_error and "Bitstamp fallback" in market.last_reference_error
    assert not any(x["market_data_valid"] for x in market.snapshot(now=NOW)["markets"].values())
