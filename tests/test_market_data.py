import time
from backend.market_data import MarketDataService, parse_binance_miniticker, parse_kraken_ticker

NOW = 1762592400.0


def mini(symbol="BTCUSDT", price="100000", event_time=NOW):
    return {"stream": "btcusdt@miniTicker", "data": {"e": "24hrMiniTicker", "E": int(event_time * 1000), "s": symbol, "c": price, "o": "99000"}}


def kraken(btc_bid="99990", btc_ask="100010", eth_bid="2998", eth_ask="3002"):
    return {"error": [], "result": {"XXBTZUSD": {"b": [btc_bid], "a": [btc_ask]}, "XETHZUSD": {"b": [eth_bid], "a": [eth_ask]}}}


def test_binance_parser_and_24h_change():
    parsed = parse_binance_miniticker(mini(), now=NOW)
    assert parsed["asset"] == "BTC"
    assert parsed["price"] == 100000
    assert parsed["change_24h_pct"] > 1


def test_invalid_spoofed_delayed_and_future_ticks_rejected():
    assert parse_binance_miniticker(mini("DOGEUSDT"), now=NOW) is None
    assert parse_binance_miniticker(mini(price="-1"), now=NOW) is None
    assert parse_binance_miniticker(mini(price="NaN"), now=NOW) is None
    assert parse_binance_miniticker(mini(event_time=NOW-31), now=NOW) is None
    assert parse_binance_miniticker(mini(event_time=NOW+16), now=NOW) is None


def test_kraken_independent_midpoint_btc_eth():
    assert parse_kraken_ticker(kraken()) == {"BTC": 100000.0, "ETH": 3000.0}
    assert parse_kraken_ticker({"error": ["rate limited"], "result": {}}) == {}


def test_verification_valid_when_fresh_and_close():
    service = MarketDataService(clock=lambda: NOW)
    assert service.ingest_primary(mini(), now=NOW)
    assert service.ingest_reference(kraken(), now=NOW)
    service.primary_connected = True
    state = service.snapshot(now=NOW)
    assert state["markets"]["BTC"]["market_data_valid"] is True
    assert state["markets"]["BTC"]["reason"] == "OK"
    assert state["trading_enabled"] is False
    assert state["execution_enabled"] is False


def test_divergence_disables_market_validity():
    service = MarketDataService(clock=lambda: NOW)
    service.ingest_primary(mini(price="108000"), now=NOW)
    service.ingest_reference(kraken(), now=NOW)
    service.primary_connected = True
    state = service.snapshot(now=NOW)["markets"]["BTC"]
    assert state["market_data_valid"] is False
    assert state["reason"] == "PRICE_DIVERGENCE"


def test_stale_primary_invalid_even_if_websocket_connected():
    service = MarketDataService(clock=lambda: NOW)
    service.ingest_primary(mini(), now=NOW)
    service.ingest_reference(kraken(), now=NOW)
    service.primary_connected = True
    state = service.snapshot(now=NOW+12)["markets"]["BTC"]
    assert state["market_data_valid"] is False
    assert state["price_usdt"] is None
    assert state["reason"] == "PRIMARY_MISSING_OR_STALE"


def test_stale_reference_invalid():
    service = MarketDataService(clock=lambda: NOW)
    service.ingest_reference(kraken(), now=NOW-100)
    service.ingest_primary(mini(event_time=NOW+100), now=NOW+100)
    service.primary_connected = True
    state = service.snapshot(now=NOW+100)["markets"]["BTC"]
    assert state["market_data_valid"] is False
    assert state["reason"] == "REFERENCE_MISSING_OR_STALE"


def test_old_out_of_order_event_cannot_replace_new_tick():
    service = MarketDataService(clock=lambda: NOW)
    assert service.ingest_primary(mini(price="100000"), now=NOW)
    assert not service.ingest_primary(mini(price="90000", event_time=NOW-1), now=NOW)
    assert service.snapshot(now=NOW)["markets"]["BTC"]["price_usdt"] == 100000


def test_history_rate_limited_to_one_per_second_and_bounded():
    service = MarketDataService(clock=lambda: NOW)
    service.ingest_primary(mini(), now=NOW)
    service.ingest_primary(mini(price="100001"), now=NOW+0.2)
    assert len(service.history("BTC")) == 1
    assert service.snapshot(now=NOW+0.2)["markets"]["BTC"]["price_usdt"] == 100001


def test_empty_response_is_not_an_error():
    state = MarketDataService(clock=lambda: NOW).snapshot()
    assert state["markets"]["ETH"]["reason"] == "PRIMARY_MISSING_OR_STALE"
    assert state["markets"]["ETH"]["market_data_valid"] is False


def test_disconnected_stream_invalidates_previously_valid_price():
    service = MarketDataService(clock=lambda: NOW)
    service.ingest_primary(mini(), now=NOW)
    service.ingest_reference(kraken(), now=NOW)
    service.primary_connected = True
    assert service.snapshot(now=NOW)["markets"]["BTC"]["market_data_valid"] is True
    service.primary_connected = False
    state = service.snapshot(now=NOW)["markets"]["BTC"]
    assert state["market_data_valid"] is False
    assert state["reason"] == "PRIMARY_DISCONNECTED"


def test_fastapi_initial_read_only_endpoints():
    from fastapi.testclient import TestClient
    from backend.app import app

    # No context manager: do not start any external market collection in this test.
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").json()["trading"] == "disabled"
    prices = client.get("/api/market/prices")
    assert prices.status_code == 200
    assert prices.headers["cache-control"] == "no-store"
    assert set(prices.json()["markets"]) == {"BTC", "ETH"}
    assert prices.json()["execution_enabled"] is False
    assert client.get("/api/market/history/DOGE").status_code == 404
