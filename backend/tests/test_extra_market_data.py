"""Ten additional public coins are display-only and fail closed on bad data."""
from backend.extra_market_data import ExtraMarketData, EXTRA_ASSETS

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
    assert EXTRA_ASSETS == ("BNB", "SOL", "XRP", "TRX", "POL", "AVAX", "LINK", "DOGE", "UNI", "ADA")
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
