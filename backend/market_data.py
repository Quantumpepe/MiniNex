"""MiniNex V0.2 -- public BTC/ETH market data. NO order-execution code.

Primary: Binance SPOT BTCUSDT/ETHUSDT miniTicker (roughly 1-second updates).
Reference: Kraken SPOT BTCUSD/ETHUSD ticker (periodic independent check).

Prices are for monitoring only, NOT executable quotes. Actual live trading needs
venue-specific bid/ask, route quotes, fee/gas estimates, and strict execution guards.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
import math
import os
import time
from typing import Any

import httpx
import websockets

LOGGER = logging.getLogger("mininex.market")
ASSETS = ("BTC", "ETH")
SYMBOL_TO_ASSET = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
DEFAULT_WS = (
    "wss://data-stream.binance.vision/stream?streams="
    "btcusdt@miniTicker/ethusdt@miniTicker"
)
DEFAULT_REFERENCE_URL = "https://api.kraken.com/0/public/Ticker"
# Fixed, independent spot-exchange reference when Kraken returns 503.
# Bitstamp market ticker: https://www.bitstamp.net/api/#tag/Tickers
DEFAULT_BITSTAMP_TICKER_URL = "https://www.bitstamp.net/api/v2/ticker"
BITSTAMP_SYMBOLS = {"BTC": "btcusd", "ETH": "ethusd"}
DEFAULT_PRIMARY_REST_URL = "https://data-api.binance.vision/api/v3/ticker/price"
DEFAULT_CANDLES_URL = "https://data-api.binance.vision/api/v3/klines"


def utc_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def positive_price(raw: Any) -> float | None:
    """Accept only finite, positive public market prices."""
    try:
        price = float(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def parse_binance_miniticker(message: str | dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
    """Parse a *combined* miniTicker event, with strict timestamp checks."""
    try:
        packet = json.loads(message) if isinstance(message, str) else message
        event = packet.get("data", packet)
        if not isinstance(event, dict) or event.get("e") != "24hrMiniTicker":
            return None
        symbol = str(event.get("s", "")).upper()
        asset = SYMBOL_TO_ASSET.get(symbol)
        price = positive_price(event.get("c"))
        open_price = positive_price(event.get("o"))
        event_time_ms = int(event["E"])
        received_at = time.time() if now is None else now
        event_at = event_time_ms / 1000.0
        # Prevent delayed/replayed packets and implausible future timestamps.
        if not asset or price is None or event_at < received_at - 30 or event_at > received_at + 15:
            return None
        change_pct = (price / open_price - 1.0) * 100 if open_price else None
        return {
            "asset": asset,
            "price": price,
            "change_24h_pct": round(change_pct, 4) if change_pct is not None else None,
            "event_at": event_at,
            "received_at": received_at,
        }
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return None


def parse_kraken_ticker(payload: dict[str, Any]) -> dict[str, float]:
    """Extract independent USD reference midpoint prices for BTC and ETH."""
    if not isinstance(payload, dict) or payload.get("error"):
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    prices: dict[str, float] = {}
    for market, ticker in result.items():
        if not isinstance(ticker, dict):
            continue
        symbol = str(market).upper()
        if not symbol.endswith("USD"):
            continue  # reject USDT, EUR and unexpected alternative quote currencies
        asset = "BTC" if "XBT" in symbol or "BTC" in symbol else "ETH" if "ETH" in symbol else None
        if asset is None:
            continue
        bid_values, ask_values = ticker.get("b"), ticker.get("a")
        bid = positive_price(bid_values[0]) if isinstance(bid_values, list) and bid_values else None
        ask = positive_price(ask_values[0]) if isinstance(ask_values, list) and ask_values else None
        if bid is not None and ask is not None and ask >= bid:
            prices[asset] = (bid + ask) / 2.0
    return prices


def parse_bitstamp_ticker(payload: Any, *, now: float) -> float | None:
    """Validate an independent, recent BTC/USD or ETH/USD spot bid/ask.

    Bitstamp reports exchange-time Unix seconds; reject stale, future,
    crossed, invalid or absurdly wide books. No `last` fallback and no
    fabricated quote if an endpoint is unavailable.
    """
    if not isinstance(payload, dict) or not math.isfinite(now):
        return None
    bid = positive_price(payload.get("bid"))
    ask = positive_price(payload.get("ask"))
    try:
        raw = payload.get("timestamp")
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            return None
        raw = str(raw)
        if not raw.isdigit() or len(raw) > 12:
            return None
        timestamp = int(raw)
    except (ValueError, OverflowError):
        return None
    if (bid is None or ask is None or ask < bid
        or not -5 <= now - timestamp <= 90
        or (ask / bid - 1) * 100 > 1.5):
        return None
    return (bid + ask) / 2.0


class MarketDataService:
    """Small in-memory collector; one instance/worker, no database required yet."""

    def __init__(self, *, clock: Any = time.time) -> None:
        self._clock = clock
        self.ws_url = os.getenv("MININEX_MARKET_WS_URL", DEFAULT_WS)
        self.reference_url = os.getenv("MININEX_REFERENCE_URL", DEFAULT_REFERENCE_URL)
        self.primary_rest_url = DEFAULT_PRIMARY_REST_URL
        self.reference_period_seconds = max(15.0, float(os.getenv("MININEX_REFERENCE_INTERVAL", "25")))
        self.max_primary_age_seconds = 10.0
        self.max_reference_age_seconds = 90.0
        self.max_divergence_pct = 1.5
        self._primary: dict[str, dict[str, Any]] = {}
        self._reference: dict[str, dict[str, Any]] = {}
        self._history: dict[str, deque] = {asset: deque(maxlen=1500) for asset in ASSETS}
        self._historical: dict[str, list[dict]] = {asset: [] for asset in ASSETS}
        self.history_loaded_at: dict[str,float|None] = {asset: None for asset in ASSETS}
        self.last_history_error: str | None = None
        self.history_errors: dict[str,str|None] = {asset: None for asset in ASSETS}
        self.primary_connected = False
        self.last_primary_error: str | None = None
        self.last_rest_error: str | None = None
        self.rest_backup_active = False
        self.last_reference_error: str | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._primary_loop(), name="market-binance"),
            asyncio.create_task(self._reference_loop(), name="market-kraken"),
            asyncio.create_task(self._rest_backup_loop(), name="market-binance-rest-backup"),
            asyncio.create_task(self._history_loop(), name="market-history-preload"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.primary_connected = False
        self.rest_backup_active = False

    def ingest_primary(self, payload: str | dict[str, Any], *, now: float | None = None) -> bool:
        """Ingest a validated WS packet. Public for deterministic offline tests."""
        tick = parse_binance_miniticker(payload, now=self._clock() if now is None else now)
        if tick is None:
            return False
        asset = tick["asset"]
        old = self._primary.get(asset)
        if old and old.get("source")=="Binance Spot WebSocket" and tick["event_at"] < old["event_at"]:
            return False  # ignore out-of-order WebSocket packets
        tick["source"] = "Binance Spot WebSocket"
        self._primary[asset] = tick
        history = self._history[asset]
        if not history or tick["received_at"] - history[-1]["timestamp"] >= 1.0:
            history.append({"timestamp": tick["received_at"], "price_usdt": tick["price"]})
        return True

    def ingest_primary_rest_asset(self, packet: Any, *, now: float | None = None) -> bool:
        """Independently recover one primary asset; never fabricate a market tick.

        A broken ETH endpoint must not prevent BTC recovery, or vice versa.
        Kraken still independently confirms both quotes in snapshot().
        """
        if not isinstance(packet, dict):
            return False
        asset = SYMBOL_TO_ASSET.get(packet.get("symbol"))
        price = positive_price(packet.get("price"))
        if asset is None or price is None:
            return False
        at = self._clock() if now is None else now
        if not math.isfinite(at) or at <= 0:
            return False
        existing = self._primary.get(asset)
        # WebSocket is the preferred low-latency feed if recently active.
        if (self.primary_connected and existing
            and existing.get("source") == "Binance Spot WebSocket"
            and 0 <= at - existing["received_at"] <= 3.0):
            return True
        if existing and existing["received_at"] > at:
            return True
        self._primary[asset] = {
            "asset": asset, "price": price, "change_24h_pct": None,
            "event_at": at, "received_at": at,
            "source": "Binance Spot REST",
        }
        history = self._history[asset]
        if not history or at - history[-1]["timestamp"] >= 1.0:
            history.append({"timestamp": at, "price_usdt": price})
        self.rest_backup_active = True
        return True

    def ingest_primary_rest(self, payload: Any, *, now: float | None = None) -> bool:
        """Legacy batch helper: reject invalid/incomplete batches atomically."""
        if not isinstance(payload, list) or len(payload) != len(ASSETS):
            return False
        symbols = [item.get("symbol") if isinstance(item, dict) else None
                   for item in payload]
        if set(symbols) != set(SYMBOL_TO_ASSET) or len(set(symbols)) != len(ASSETS):
            return False
        if any(positive_price(item.get("price")) is None for item in payload):
            return False
        at = self._clock() if now is None else now
        return all(self.ingest_primary_rest_asset(item, now=at) for item in payload)

    async def _rest_backup_loop(self) -> None:
        """Prompt per-asset REST recovery without sequential BTC/ETH delays.

        Check every three seconds; request both symbols concurrently with a
        bounded timeout. Missing data still fails closed after freshness expiry.
        """
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.5), follow_redirects=False
        ) as client:
            while True:
                try:
                    at = self._clock()
                    needed = [
                        symbol for symbol, asset in SYMBOL_TO_ASSET.items()
                        if not self.primary_connected
                        or not (sample := self._primary.get(asset))
                        or not 0 <= at - sample["received_at"] <= 3.0
                    ]
                    if needed:
                        async def fetch_symbol(symbol: str) -> dict:
                            response = await client.get(
                                self.primary_rest_url, params={"symbol":symbol}
                            )
                            response.raise_for_status()
                            packet = response.json()
                            if (not isinstance(packet, dict)
                                or packet.get("symbol") != symbol
                                or positive_price(packet.get("price")) is None):
                                raise ValueError("Invalid Binance spot REST "+symbol)
                            return packet

                        results = await asyncio.gather(
                            *(fetch_symbol(symbol) for symbol in needed),
                            return_exceptions=True,
                        )
                        errors = []
                        for symbol, result in zip(needed, results):
                            if isinstance(result, BaseException):
                                errors.append(f"{symbol}: {type(result).__name__}: {str(result)[:90]}")
                            elif not self.ingest_primary_rest_asset(result):
                                errors.append(f"{symbol}: Invalid fallback data")
                        self.last_rest_error = "; ".join(errors)[:250] if errors else None
                        if errors:
                            LOGGER.warning("Binance REST backup: %s", self.last_rest_error)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_rest_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                    LOGGER.warning("Binance REST market backup unavailable: %s",
                                   self.last_rest_error)
                await asyncio.sleep(3.0)

    @staticmethod
    def _parse_closed_candles(payload: Any, now: float, interval_seconds: int) -> list[dict]:
        """Strictly validated, already-closed spot candles; never fabricated."""
        if not isinstance(payload,list) or not payload or len(payload)>500:
            raise ValueError("Invalid historical candle batch")
        parsed=[]
        last_open=-1.0
        for item in payload:
            if not isinstance(item,list) or len(item)<7:
                raise ValueError("Invalid historical candle")
            try:
                opened=float(item[0])/1000
                closed=float(item[6])/1000
            except (TypeError,ValueError,OverflowError):
                raise ValueError("Invalid candle timestamp") from None
            price=positive_price(item[4])
            if (price is None or not math.isfinite(opened) or not math.isfinite(closed)
                or opened<=last_open or closed-opened<interval_seconds-1.1
                or closed-opened>interval_seconds+1.1 or closed>=now
                or opened<now-93000 or opened>now):
                raise ValueError("Invalid, future or incomplete candle")
            parsed.append({"timestamp":closed,"price_usdt":price})
            last_open=opened
        return parsed

    def ingest_historical_candles(
        self, asset: str, one_minute: Any, five_minute: Any,
        *, now: float | None = None,
    ) -> int:
        """Atomically replace an asset's historical cache with verified timestamps.

        1m candles give 60/180s momentum, 5m candles give real 24h context.
        Public Binance history is NEVER independently executable prices:
        the most recent live quote still requires a fresh Kraken cross-check.
        """
        if asset not in ASSETS:
            raise ValueError("Unsupported historical asset")
        at=self._clock() if now is None else now
        fine=self._parse_closed_candles(one_minute,at,60)
        coarse=self._parse_closed_candles(five_minute,at,300)
        if (len(fine)<4 or len(coarse)<200
            or at-fine[-1]["timestamp"]>180
            or at-coarse[-1]["timestamp"]>900
            or coarse[0]["timestamp"]>at-84000):
            raise ValueError("Historical coverage incomplete or stale")
        cutoff=fine[0]["timestamp"]
        merged=[p for p in coarse if p["timestamp"]<cutoff]+fine
        # Never append old snapshots on top of fresh WebSocket data.
        self._historical[asset]=merged
        self.history_loaded_at[asset]=at
        return len(merged)

    async def _history_loop(self) -> None:
        """Fetch actual closed Binance candles on boot and renew every 5m."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0),
                                     follow_redirects=False) as client:
            while True:
                for asset in ASSETS:
                    try:
                        symbol=f"{asset}USDT"
                        # Request only CLOSED candles. Binance otherwise
                        # includes the currently forming, untrustworthy bar.
                        clock_now=self._clock()
                        one_end=(int(clock_now)//60*60-1)*1000
                        five_end=(int(clock_now)//300*300-1)*1000
                        one=await client.get(DEFAULT_CANDLES_URL,params={
                            "symbol":symbol,"interval":"1m","limit":240,
                            "endTime":one_end,
                        })
                        one.raise_for_status()
                        five=await client.get(DEFAULT_CANDLES_URL,params={
                            "symbol":symbol,"interval":"5m","limit":300,
                            "endTime":five_end,
                        })
                        five.raise_for_status()
                        count=self.ingest_historical_candles(
                            asset,one.json(),five.json()
                        )
                        self.last_history_error=None
                        self.history_errors[asset]=None
                        LOGGER.info("Loaded %d real historical %s spot samples",count,asset)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.last_history_error=f"{type(exc).__name__}: {str(exc)[:160]}"
                        self.history_errors[asset]=self.last_history_error
                        LOGGER.warning("Spot history preload unavailable for %s: %s",
                                       asset,self.last_history_error)
                await asyncio.sleep(300.0)

    def ingest_reference(self, payload: dict[str, Any], *, now: float | None = None) -> bool:
        prices = parse_kraken_ticker(payload)
        if not prices:
            return False
        at = self._clock() if now is None else now
        for asset, price in prices.items():
            self._reference[asset] = {
                "price": price, "received_at": at, "source": "Kraken Spot",
            }
        return True

    def ingest_bitstamp_reference(
        self, asset: str, payload: Any, *, now: float | None = None
    ) -> bool:
        """Recover only one independently verified asset; never affect the other."""
        if asset not in ASSETS:
            return False
        at = self._clock() if now is None else now
        price = parse_bitstamp_ticker(payload, now=at)
        if price is None:
            return False
        current = self._reference.get(asset)
        if current and current["received_at"] > at:
            return False
        self._reference[asset] = {
            "price": price, "received_at": at, "source": "Bitstamp Spot",
        }
        return True

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        at = self._clock() if now is None else now
        markets: dict[str, dict[str, Any]] = {}
        for asset in ASSETS:
            primary = self._primary.get(asset)
            reference = self._reference.get(asset)
            primary_fresh = bool(primary and 0 <= at - primary["received_at"] <= self.max_primary_age_seconds)
            reference_fresh = bool(reference and 0 <= at - reference["received_at"] <= self.max_reference_age_seconds)
            divergence = None
            if primary_fresh and reference_fresh:
                divergence = abs(primary["price"] - reference["price"]) / reference["price"] * 100
            source=primary.get("source","Binance Spot WebSocket") if primary else None
            primary_healthy=self.primary_connected if source=="Binance Spot WebSocket" else source=="Binance Spot REST"
            verified = bool(primary_fresh and primary_healthy and divergence is not None and divergence <= self.max_divergence_pct)
            if not primary_fresh:
                reason = "PRIMARY_MISSING_OR_STALE"
            elif not primary_healthy:
                reason = "PRIMARY_DISCONNECTED"
            elif not reference_fresh:
                reason = "REFERENCE_MISSING_OR_STALE"
            elif not verified:
                reason = "PRICE_DIVERGENCE"
            else:
                reason = "OK"
            markets[asset] = {
                "asset": asset,
                "primary_pair": f"{asset}/USDT",
                "price_usdt": primary["price"] if primary_fresh else None,
                "change_24h_pct": primary["change_24h_pct"] if primary_fresh else None,
                "primary_source": source,
                "primary_at": utc_iso(primary["event_at"]) if primary else None,
                "reference_price_usd": reference["price"] if reference_fresh else None,
                "reference_source": reference.get("source", "Kraken Spot") if reference_fresh else None,
                "reference_at": utc_iso(reference["received_at"]) if reference else None,
                "price_divergence_pct": round(divergence, 4) if divergence is not None else None,
                "market_data_valid": verified,
                "reason": reason,
            }
        return {
            "app": "MiniNex",
            "version": "0.2.0",
            "timestamp": utc_iso(at),
            "trading_enabled": False,
            "execution_enabled": False,
            "primary_ws_connected": self.primary_connected,
            "rest_backup_active": any(v.get("primary_source")=="Binance Spot REST" and v.get("market_data_valid") for v in markets.values()),
            "historical_samples":{a:len(self._historical[a]) for a in ASSETS},
            "history_loaded_at":{a:utc_iso(self.history_loaded_at[a]) for a in ASSETS},
            "history_errors":dict(self.history_errors),
            "markets": markets,
        }

    def history(self, asset: str, *, limit: int = 120) -> list[dict[str, Any]]:
        if asset not in ASSETS:
            raise ValueError("BTC or ETH required")
        now=self._clock()
        # Raw ticks take precedence over any historical candle with the
        # same timestamp; all samples remain actual public exchange data.
        points=[p for p in self._historical[asset] if 0<=now-p["timestamp"]<=90000]
        points.extend(p for p in self._history[asset] if 0<=now-p["timestamp"]<=90000)
        points.sort(key=lambda p:p["timestamp"])
        return points[-max(1,min(limit,3600)):]

    async def _primary_loop(self) -> None:
        retry_seconds = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.ws_url,
                    open_timeout=12,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=5,
                    max_size=65536,
                ) as stream:
                    self.primary_connected = True
                    self.last_primary_error = None
                    retry_seconds = 1.0
                    LOGGER.info("Binance public market stream connected")
                    while True:
                        try:
                            packet=await asyncio.wait_for(stream.recv(),timeout=18.0)
                        except asyncio.TimeoutError:
                            raise TimeoutError("No primary market ticks for 18 seconds") from None
                        self.ingest_primary(packet)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_primary_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                LOGGER.warning("Primary market WebSocket disconnected: %s", self.last_primary_error)
            finally:
                self.primary_connected = False
            await asyncio.sleep(retry_seconds)
            retry_seconds = min(30.0, retry_seconds * 2)

    async def _reference_loop(self) -> None:
        """Continue independently checking both assets if Kraken goes offline.

        The reference must remain an actual public *other exchange* bid/ask,
        with independent timestamps and the usual 1.5% Binance divergence
        guard. Fail closed if BOTH providers have no fresh quote.
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), follow_redirects=False) as client:
            while True:
                try:
                    verified_from_kraken: set[str] = set()
                    kraken_error = None
                    try:
                        response = await client.get(
                            self.reference_url, params={"pair": "XBTUSD,ETHUSD"}
                        )
                        response.raise_for_status()
                        packet = response.json()
                        prices = parse_kraken_ticker(packet)
                        if not prices:
                            raise ValueError("Kraken returned no valid BTC/USD or ETH/USD bid/ask")
                        self.ingest_reference(packet)
                        verified_from_kraken = set(prices)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        kraken_error = f"{type(exc).__name__}: {str(exc)[:135]}"
                        LOGGER.warning("Independent Kraken price check failed: %s", kraken_error)
                    missing = [asset for asset in ASSETS if asset not in verified_from_kraken]
                    failures = []
                    if missing:
                        async def fetch_bitstamp(asset: str) -> bool:
                            market = BITSTAMP_SYMBOLS[asset]
                            response = await client.get(
                                f"{DEFAULT_BITSTAMP_TICKER_URL}/{market}/"
                            )
                            response.raise_for_status()
                            return self.ingest_bitstamp_reference(asset, response.json())

                        results = await asyncio.gather(
                            *(fetch_bitstamp(asset) for asset in missing),
                            return_exceptions=True,
                        )
                        for asset, result in zip(missing, results):
                            if isinstance(result, BaseException) or result is not True:
                                failures.append(
                                    asset + ": " + (
                                        f"{type(result).__name__}: {str(result)[:60]}"
                                        if isinstance(result, BaseException) else "invalid Bitstamp ticker"
                                    )
                                )
                    if failures:
                        self.last_reference_error = (
                            (kraken_error + "; " if kraken_error else "")
                            + "Bitstamp fallback: " + "; ".join(failures)
                        )[:300]
                        LOGGER.warning("Independent reference unavailable: %s", self.last_reference_error)
                    else:
                        # Kraken is allowed to be offline if independent
                        # Bitstamp spot quotes for BOTH assets are confirmed.
                        self.last_reference_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_reference_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                    LOGGER.warning("Independent reference loop failed: %s", self.last_reference_error)
                await asyncio.sleep(self.reference_period_seconds)