"""Ten additional public coins are display-only and fail closed on bad data."""
from backend.extra_market_data import (
    ExtraMarketData, EXTRA_ASSETS, discover_kraken_usd_pairs,
    gecko_request_settings, gecko_backoff_seconds, retry_after_seconds,
)
import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest
from backend import extra_market_data as extra

NOW = 1_800_000_000.0


def make_ws(asset, price, at=NOW):
    return {"data": {"e": "24hrMiniTicker", "s": asset+"USDT",
                     "c": str(price), "o": str(price*0.99),
                     "E": int(at*1000)}}


def gecko_row(asset, price, at=NOW):
    from backend.extra_market_data import GECKO_IDS
    return {GECKO_IDS[asset]: {"usd": price, "last_updated_at": int(at)}}


def test_all_ten_public_prices_are_separate_and_not_tradable():
    service = ExtraMarketData(clock=lambda: NOW)
    assert EXTRA_ASSETS == ("BNB", "SOL", "XRP", "TRX", "POL", "AVAX", "LINK", "QNT", "UNI", "ARB")
    for asset, price in zip(EXTRA_ASSETS, (600, 200, 2, .3, .3, 22, 13, .15, 8, .9)):
        assert service.ingest_ws(make_ws(asset, price), now=NOW)
        assert service.ingest_gecko(gecko_row(asset, price), now=NOW) == 1
    snapshot = service.snapshot(now=NOW)
    assert set(snapshot) == set(EXTRA_ASSETS)
    for asset in EXTRA_ASSETS:
        assert snapshot[asset]["market_data_valid"]
        assert snapshot[asset]["reason"] == "OK"
        assert snapshot[asset]["market_data_only"] is True
        assert snapshot[asset]["trading_enabled"] is False
        assert snapshot[asset]["primary_pair"] == asset + "/USDT"


def test_stale_invalid_or_divergent_prices_never_display_as_verified():
    service = ExtraMarketData(clock=lambda: NOW)
    assert not service.ingest_ws(make_ws("UNKNOWN", 100), now=NOW)
    assert not service.ingest_ws(make_ws("BNB", -10), now=NOW)
    assert not service.ingest_ws(make_ws("BNB", 100, NOW-60), now=NOW)
    assert service.ingest_ws(make_ws("BNB", 600), now=NOW)
    assert service.snapshot(now=NOW)["BNB"]["reason"] == "REFERENCE_MISSING_OR_STALE"
    assert not service.ingest_gecko(gecko_row("BNB", 600, NOW-500), now=NOW)
    assert service.ingest_gecko(gecko_row("BNB", 620), now=NOW)
    assert service.snapshot(now=NOW)["BNB"]["reason"] == "PRICE_DIVERGENCE"
    assert service.ingest_gecko(gecko_row("BNB", 600), now=NOW)
    assert service.snapshot(now=NOW)["BNB"]["market_data_valid"]
    assert not service.snapshot(now=NOW+11)["BNB"]["market_data_valid"]
    assert service.snapshot(now=NOW+11)["BNB"]["reason"] == "PRIMARY_MISSING_OR_STALE"
    assert not service.ingest_ws({"data":{"e":"24hrMiniTicker",
        "s":"POLUSDT","c":"not-a-number","E":int(NOW*1000)}}, now=NOW)


def test_rest_only_restores_real_public_prices_not_orders():
    service = ExtraMarketData(clock=lambda: NOW)
    assert not service.ingest_rest({"symbol":"UNKNOWNUSDT","price":"0.5"},now=NOW)
    assert not service.ingest_rest({"symbol":"SOLUSDT","price":"NaN"},now=NOW)
    assert service.ingest_rest({"symbol":"SOLUSDT","price":"200"},now=NOW)
    assert service.ingest_gecko(gecko_row("SOL", 200),now=NOW)
    quote = service.snapshot(now=NOW)["SOL"]
    assert quote["market_data_valid"] is True
    assert quote["primary_source"] == "Binance Spot REST"
    assert quote["change_24h_pct"] is None
    assert quote["trading_enabled"] is False


def test_chart_bootstrap_accepts_only_real_closed_ordered_candles():
    service = ExtraMarketData(clock=lambda: NOW)
    rows = []
    for i in range(90):
        close = NOW - (91-i)*60 - .001
        rows.append([0, "1", "1", "1", str(1+i*.0001), "100",
                     int(close*1000)])
    assert service.ingest_candles("POL", rows, now=NOW) == 90
    assert len(service.history("POL",limit=3600)) == 90
    bad = [list(row) for row in rows]
    bad[-1][6] = int((NOW+60)*1000)
    try:
        service.ingest_candles("POL", bad, now=NOW)
    except ValueError:
        pass
    else:
        raise AssertionError("Future candle accepted")
    assert len(service.history("POL", limit=3600)) == 90


def test_coingecko_public_is_one_batch_every_two_minutes_and_keys_stay_server_side():
    url, headers, interval = gecko_request_settings({})
    assert url == "https://api.coingecko.com/api/v3/simple/price"
    assert headers == {}
    assert interval == 120
    url, headers, interval = gecko_request_settings({
        "MININEX_COINGECKO_DEMO_API_KEY": "test-demo-only",
    })
    assert url == "https://api.coingecko.com/api/v3/simple/price"
    assert headers == {"x-cg-demo-api-key": "test-demo-only"}
    assert interval == 60
    url, headers, interval = gecko_request_settings({
        "MININEX_COINGECKO_DEMO_API_KEY": "test-demo-only",
        "MININEX_COINGECKO_PRO_API_KEY": "test-paid-only",
    })
    assert url == "https://pro-api.coingecko.com/api/v3/simple/price"
    assert headers == {"x-cg-pro-api-key": "test-paid-only"}
    assert "test-paid-only" not in url
    assert interval == 60


def test_retry_after_and_exponential_rate_limit_backoff_never_burst():
    now = NOW
    date = format_datetime(datetime.fromtimestamp(now + 720, timezone.utc),
                           usegmt=True)
    assert retry_after_seconds("180", now=now) == 180
    assert retry_after_seconds(date, now=now) == 720
    assert retry_after_seconds("invalid", now=now) is None
    assert retry_after_seconds(None, now=now) is None
    assert retry_after_seconds("999999", now=now) == 3600
    assert gecko_backoff_seconds(1, now=now) == 300
    assert gecko_backoff_seconds(2, now=now) == 600
    assert gecko_backoff_seconds(3, now=now) == 1200
    assert gecko_backoff_seconds(12, now=now) == 1800
    assert gecko_backoff_seconds(1, retry_after="720", now=now) == 720
    assert gecko_backoff_seconds(1, retry_after=date, now=now) == 720


def test_kraken_pairs_require_exact_market_and_us_dollar_quote():
    data = {"error": [], "result": {
        "BNBUSD": {"wsname": "BNB/USD", "status": "online"},
        "XDGZUSD": {"wsname": "XDG/USD", "status": "online"},
        "QNTUSD": {"wsname": "QNT/USD", "status": "online"},
        "SOLUSDT": {"wsname": "SOL/USDT", "status": "online"},
        "ARBUSD": {"wsname": "ARB/USD", "status": "online"},
        "DODGY": {"wsname": "DOGE/USD", "status": "cancel_only"},
        "LINKUSD": {"wsname": "LINK/USD", "status": "offline"},
        "UNKNOWNUSD": {"wsname": "UNKNOWN/USD", "status": "online"},
    }}
    assert discover_kraken_usd_pairs(data) == {
        "BNB": "BNBUSD", "QNT": "QNTUSD", "ARB": "ARBUSD",
    }
    assert discover_kraken_usd_pairs({"error": ["EQuery:Unknown asset pair"],
                                      "result": data["result"]}) == {}
    assert discover_kraken_usd_pairs({"error": [], "result": []}) == {}


def test_kraken_secondary_reference_replaces_expired_gecko_not_live_capability():
    svc = ExtraMarketData(clock=lambda: NOW)
    svc._kraken_pairs = {"SOL": "SOLUSD"}
    assert svc.ingest_ws(make_ws("SOL", 200), now=NOW)
    assert svc.ingest_kraken({"error": [], "result": {
        "SOLUSD": {"b": ["199.5"], "a": ["200.5"]},
    }}, now=NOW) == 1
    snap = svc.snapshot(now=NOW)["SOL"]
    assert snap["market_data_valid"] is True
    assert snap["reference_source"] == "Kraken Spot USD"
    assert snap["trading_enabled"] is False
    assert snap["market_data_only"] is True
    svc.ingest_gecko(gecko_row("SOL", 199, NOW), now=NOW)
    assert svc.snapshot(now=NOW)["SOL"]["reference_source"] == "CoinGecko USD"
    # Source-stamped CoinGecko may not be prolonged artificially by cached data.
    svc.ingest_ws(make_ws("SOL", 200, NOW+180), now=NOW+180)
    svc.ingest_kraken({"error": [], "result": {
        "SOLUSD": {"b": ["199.5"], "a": ["200.5"]},
    }}, now=NOW+179)
    snap = svc.snapshot(now=NOW+181)["SOL"]
    assert snap["reference_source"] == "Kraken Spot USD"
    assert snap["market_data_valid"] is True
    assert svc.snapshot(now=NOW+300)["SOL"]["market_data_valid"] is False


def test_kraken_mismatched_pair_spread_and_invalid_prices_never_verify():
    svc = ExtraMarketData(clock=lambda: NOW)
    svc._kraken_pairs = {"QNT": "QNTUSD"}
    svc.ingest_ws(make_ws("QNT", .2), now=NOW)
    assert svc.ingest_kraken({"error": [], "result": {
        "WRONG": {"b": [".19"], "a": [".21"]}}}, now=NOW) == 0
    assert svc.ingest_kraken({"error": [], "result": {
        "QNTUSD": {"b": [".1"], "a": [".2"]}}}, now=NOW) == 0
    assert svc.ingest_kraken({"error": [], "result": {
        "QNTUSD": {"b": ["NaN"], "a": [".2"]}}}, now=NOW) == 0
    assert svc.snapshot(now=NOW)["QNT"]["market_data_valid"] is False
    assert svc.ingest_kraken({"error": [], "result": {
        "QNTUSD": {"b": [".2"], "a": [".2001"]}}}, now=NOW) == 1
    assert svc.snapshot(now=NOW)["QNT"]["market_data_valid"] is True


def test_gecko_429_waits_without_immediate_retries(monkeypatch):
    calls = []
    delays = []
    class Response:
        status_code = 429
        headers = {"Retry-After": "601"}
    class FakeHttp:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return False
        async def get(self, *_args, **kwargs):
            calls.append(kwargs["params"]["ids"])
            return Response()
    monkeypatch.setattr(extra.httpx, "AsyncClient", lambda *args, **kwargs: FakeHttp())
    async def stop_on_sleep(seconds):
        delays.append(seconds)
        raise asyncio.CancelledError()
    monkeypatch.setattr(extra.asyncio, "sleep", stop_on_sleep)
    svc = ExtraMarketData(clock=lambda: NOW)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc._gecko_loop())
    assert len(calls) == 1
    assert calls[0].count(",") == 11  # all twelve market assets in one call
    assert delays == [601]
    assert not svc._reference
    status=svc.diagnostics(now=NOW)
    assert status['gecko_cooldown_active'] is True
    assert status['gecko_next_attempt_in_seconds'] == 601
    assert status['trading_enabled'] is False


def test_ten_coin_diagnostics_never_claim_unverified_prices_or_trading():
    svc = ExtraMarketData(clock=lambda: NOW)
    report = svc.diagnostics(now=NOW)
    assert report["mode"] == "EXTRA_COINS_READ_ONLY"
    assert report["coin_count"] == 10
    assert report["verified_coin_count"] == 0
    assert report["trading_enabled"] is False
    assert report["gecko_cooldown_active"] is False
    assert report["gecko_next_attempt_in_seconds"] is None
    assert set(report["market_data_valid"]) == set(EXTRA_ASSETS)
    assert set(report["market_reasons"]) == set(EXTRA_ASSETS)
    assert not any(report["market_data_valid"].values())
    assert not any(report["reference_sources"].values())

    assert svc.ingest_ws(make_ws("BNB", 600))
    assert svc.ingest_gecko(gecko_row("BNB", 600)) == 1
    report = svc.diagnostics(now=NOW)
    assert report["verified_coin_count"] == 1
    assert report["market_data_valid"]["BNB"] is True
    assert report["reference_sources"]["BNB"] == "CoinGecko USD"
    assert report["market_reasons"]["BNB"] == "OK"
    assert all(not value for key,value in report["market_data_valid"].items() if key!="BNB")
    assert report["trading_enabled"] is False


def test_rate_limit_diagnostics_expires_and_contains_no_secrets():
    svc = ExtraMarketData(clock=lambda: NOW)
    svc._gecko_rate_limited = True
    svc._gecko_next_attempt_at = NOW + 300
    first=svc.diagnostics(now=NOW + 2)
    assert first["gecko_cooldown_active"] is True
    assert first["gecko_next_attempt_in_seconds"] == 298
    expired=svc.diagnostics(now=NOW + 301)
    assert expired["gecko_cooldown_active"] is False
    assert expired["gecko_next_attempt_in_seconds"] == 0
    serialized=str(first)
    assert "api_key" not in serialized.lower()
    assert "http" not in serialized.lower()
    assert "wallet" not in serialized.lower()


def test_public_market_status_contains_bounded_readonly_extra_coin_report():
    from backend import app as api
    report = api.market_status()["extra_coin_markets"]
    assert report["coin_count"] == 10
    assert report["mode"] == "EXTRA_COINS_READ_ONLY"
    assert report["trading_enabled"] is False
    assert set(report["market_data_valid"]) == set(EXTRA_ASSETS)


def test_arb_replaces_ada_with_real_usdt_and_independent_usd_reference():
    from backend.extra_market_data import GECKO_IDS,SYMBOLS,WS_URL,EXTRA_ASSETS
    assert "ARB" in EXTRA_ASSETS and "ADA" not in EXTRA_ASSETS
    assert GECKO_IDS["ARB"] == "arbitrum"
    assert "cardano" not in GECKO_IDS.values()
    assert SYMBOLS["ARBUSDT"] == "ARB"
    assert "arbusdt@miniTicker" in WS_URL
    service=ExtraMarketData(clock=lambda: NOW)
    assert "ARB" in service._long_historical
    assert service.ingest_ws(make_ws("ARB",1.2),now=NOW)
    assert service.snapshot(now=NOW)["ARB"]["reason"]=="REFERENCE_MISSING_OR_STALE"
    assert service.ingest_gecko(gecko_row("ARB",1.2),now=NOW)==1
    quote=service.snapshot(now=NOW)["ARB"]
    assert quote["price_usdt"]==1.2
    assert quote["market_data_valid"] is True
    assert quote["market_data_only"] is True
    assert quote["trading_enabled"] is False
    assert service.ingest_ws(make_ws("ADA",0.5),now=NOW) is False
    assert len(service.history("ARB"))==1
    assert service.history("ARB")[0]["price_usdt"]==1.2
    with pytest.raises(ValueError):
        service.history("ADA")


def test_arb_history_and_price_snapshot_http_endpoints_no_longer_return_404():
    """Regression for the Android update: /api/market/history/ARB was 404."""
    from fastapi.testclient import TestClient
    from backend.app import app
    client=TestClient(app)
    chart=client.get("/api/market/history/ARB?limit=3600")
    assert chart.status_code==200
    assert chart.json()["asset"]=="ARB"
    assert chart.json()["currency"]=="USDT"
    assert isinstance(chart.json()["points"],list)
    prices=client.get("/api/market/prices")
    assert prices.status_code==200
    assert "ARB" in prices.json()["markets"]
    assert "ADA" not in prices.json()["markets"]
    assert client.get("/api/market/history/ADA").status_code==404


def test_shared_coingecko_provider_supports_all_twelve_assets_and_paid_key():
    from backend.coingecko_provider import COIN_IDS, coingecko_request_settings
    assert set(COIN_IDS) >= {"BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "POL", "AVAX", "LINK", "QNT", "UNI", "ARB"}
    pro = {"MININEX_COINGECKO_PRO_API_KEY": "server-secret"}
    url, headers, interval = coingecko_request_settings("/simple/price", pro)
    assert url == "https://pro-api.coingecko.com/api/v3/simple/price"
    assert headers == {"x-cg-pro-api-key": "server-secret"}
    assert "server-secret" not in url
    assert interval == 60
    url, headers, _ = coingecko_request_settings("/coins/ethereum/market_chart/range", pro)
    assert url == "https://pro-api.coingecko.com/api/v3/coins/ethereum/market_chart/range"
    assert headers == {"x-cg-pro-api-key": "server-secret"}
    with pytest.raises(ValueError):
        coingecko_request_settings("/coins/unknown-token/market_chart/range", pro)
    with pytest.raises(ValueError):
        coingecko_request_settings("/users/private", pro)

def test_coingecko_basic_and_future_analyst_use_identical_paid_transport():
    from backend.coingecko_provider import coingecko_request_settings, MARKET_ASSETS, COIN_IDS
    assert len(MARKET_ASSETS) == 12
    assert len({COIN_IDS[asset] for asset in MARKET_ASSETS}) == 12
    env = {"MININEX_COINGECKO_PRO_API_KEY": "existing-nexus-plan-key",
           "MININEX_COINGECKO_REFERENCE_POLL_SECONDS": "120"}
    url, headers, interval = coingecko_request_settings("/simple/price", env)
    assert url == "https://pro-api.coingecko.com/api/v3/simple/price"
    assert headers == {"x-cg-pro-api-key": "existing-nexus-plan-key"}
    assert interval == 120
    assert "existing-nexus-plan-key" not in url
    # Invalid overrides cannot accidentally create an aggressive poll loop.
    env["MININEX_COINGECKO_REFERENCE_POLL_SECONDS"] = "-5"
    assert coingecko_request_settings("/simple/price", env)[2] == 60
    env["MININEX_COINGECKO_REFERENCE_POLL_SECONDS"] = "9999"
    assert coingecko_request_settings("/simple/price", env)[2] == 60
    public = {"MININEX_COINGECKO_REFERENCE_POLL_SECONDS": "60"}
    assert coingecko_request_settings("/simple/price", public)[2] == 120
