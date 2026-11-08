"""Resilient Binance public spot feed: fail closed without independent Kraken."""
import pytest

from backend.market_data import MarketDataService


def rest_prices(btc="70000",eth="3500"):
    return [{"symbol":"BTCUSDT","price":btc},
            {"symbol":"ETHUSDT","price":eth}]


def reference():
    return {"error":[],"result":{
        "XXBTZUSD":{"b":["69999"],"a":["70001"]},
        "XETHZUSD":{"b":["3499"],"a":["3501"]},
    }}


def test_rest_fallback_remains_untrusted_until_kraken_verification():
    m=MarketDataService(clock=lambda:1000.0)
    assert m.ingest_primary_rest(rest_prices(),now=1000)
    snap=m.snapshot(now=1000)
    assert snap["markets"]["BTC"]["market_data_valid"] is False
    assert snap["markets"]["BTC"]["reason"]=="REFERENCE_MISSING_OR_STALE"
    assert m.ingest_reference(reference(),now=1000)
    snap=m.snapshot(now=1001)
    assert snap["rest_backup_active"] is True
    for asset in ("BTC","ETH"):
        assert snap["markets"][asset]["market_data_valid"] is True
        assert snap["markets"][asset]["primary_source"]=="Binance Spot REST"


def test_rest_backup_rejects_missing_duplicate_or_malformed_market():
    m=MarketDataService(clock=lambda:1000.0)
    for bad in ([],{},[rest_prices()[0]],rest_prices()+[rest_prices()[0]],
                [{"symbol":"BTCUSDT","price":"70000"}]*2,
                rest_prices(btc="NaN"),rest_prices(eth="-1"),
                [{"symbol":"BTCUSDT","price":"70000"},{"symbol":"SOLUSDT","price":"100"}]):
        assert m.ingest_primary_rest(bad,now=1000) is False
    assert not m._primary


def test_stale_fallback_is_never_verified_and_recovery_works():
    m=MarketDataService(clock=lambda:1000.0)
    assert m.ingest_reference(reference(),now=1000)
    assert m.ingest_primary_rest(rest_prices(),now=1000)
    assert m.snapshot(now=1011)["markets"]["BTC"]["reason"]=="PRIMARY_MISSING_OR_STALE"
    assert m.snapshot(now=1011)["markets"]["BTC"]["market_data_valid"] is False
    assert m.ingest_primary_rest(rest_prices(),now=1011)
    assert m.snapshot(now=1011)["markets"]["BTC"]["market_data_valid"] is True
    assert m.snapshot(now=1102)["markets"]["BTC"]["market_data_valid"] is False


def test_rest_never_overwrites_current_websocket():
    m=MarketDataService(clock=lambda:1000.0)
    m.primary_connected=True
    for name,price in (("BTCUSDT","70000"),("ETHUSDT","3500")):
        assert m.ingest_primary({"data":{"e":"24hrMiniTicker","s":name,"c":price,
            "o":price,"E":1000000}},now=1000)
    assert m.ingest_primary_rest(rest_prices("71000","3600"),now=1001)
    assert m.snapshot(now=1001)["markets"]["BTC"]["primary_source"]=="Binance Spot WebSocket"
    assert m._primary["BTC"]["price"]==70000


def test_websocket_reclaims_market_feed_after_rest_recovery():
    m=MarketDataService(clock=lambda:1000.0)
    assert m.ingest_primary_rest(rest_prices(),now=1000)
    m.primary_connected=True
    assert m.ingest_primary({"data":{"e":"24hrMiniTicker","s":"BTCUSDT",
        "c":"70001","o":"69900","E":1000000}},now=1001)
    assert m._primary["BTC"]["source"]=="Binance Spot WebSocket"
    assert m._primary["BTC"]["price"]==70001


def test_backup_with_kraken_price_divergence_fails_closed():
    m=MarketDataService(clock=lambda:1000.0)
    assert m.ingest_reference(reference(),now=1000)
    assert m.ingest_primary_rest(rest_prices(btc="78000"),now=1000)
    state=m.snapshot(now=1000)["markets"]
    assert state["BTC"]["reason"]=="PRICE_DIVERGENCE"
    assert state["BTC"]["market_data_valid"] is False
    assert state["ETH"]["market_data_valid"] is True


def test_binance_multi_symbol_query_contains_no_spaces():
    # Binance rejects percent-encoded JSON arrays with a whitespace separator
    # on some deployments. Keep the symbols argument canonical and compact.
    from inspect import getsource
    from backend.market_data import MarketDataService
    source=getsource(MarketDataService._rest_backup_loop)
    assert 'json.dumps(list(SYMBOL_TO_ASSET),separators=(",",":"))' in source
    import json
    value=json.dumps(["BTCUSDT","ETHUSDT"],separators=(",",":"))
    assert value=='["BTCUSDT","ETHUSDT"]'
