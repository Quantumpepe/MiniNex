"""Read-only public spot-market previews for six additional MiniNex coins.

This service is intentionally separate from BTC/ETH trading and PAPER engines.
Binance supplies USDT spot quotes/candles; CoinGecko USD prices independently
cross-check live display values. Never use preview quotes for order execution.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
import math
import time
from typing import Any

import httpx
import websockets

LOGGER = logging.getLogger("mininex.extra_markets")
EXTRA_ASSETS = ("BNB", "POL", "SOL", "XRP", "ADA", "TRX")
GECKO_IDS = {
    "BNB": "binancecoin",
    "POL": "polygon-ecosystem-token",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "TRX": "tron",
}
SYMBOLS = {asset + "USDT": asset for asset in EXTRA_ASSETS}
WS_URL = ("wss://data-stream.binance.vision/stream?streams=" +
          "/".join(symbol.lower() + "@miniTicker" for symbol in SYMBOLS))
REST_URL = "https://data-api.binance.vision/api/v3/ticker/price"
CANDLES_URL = "https://data-api.binance.vision/api/v3/klines"
GECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


def valid_price(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def iso(at: float | None) -> str | None:
    return datetime.fromtimestamp(at, timezone.utc).isoformat() if at is not None else None


class ExtraMarketData:
    """Public price-only collector; no wallet, PAPER or LIVE execution access."""

    def __init__(self, *, clock=time.time) -> None:
        self.clock = clock
        self.connected = False
        self._primary: dict[str, dict[str, Any]] = {}
        self._reference: dict[str, dict[str, Any]] = {}
        self._ticks = {asset: deque(maxlen=1500) for asset in EXTRA_ASSETS}
        self._historical: dict[str, list[dict[str, float]]] = {asset: [] for asset in EXTRA_ASSETS}
        self._tasks: list[asyncio.Task] = []
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._ws_loop(), name="extra-market-spot"),
            asyncio.create_task(self._rest_loop(), name="extra-market-rest"),
            asyncio.create_task(self._gecko_loop(), name="extra-market-reference"),
            asyncio.create_task(self._history_loop(), name="extra-market-history"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.connected = False

    def ingest_ws(self, packet: str | dict, *, now: float | None = None) -> bool:
        try:
            frame = json.loads(packet) if isinstance(packet, str) else packet
            event = frame.get("data", frame)
            if event.get("e") != "24hrMiniTicker":
                return False
            asset = SYMBOLS.get(event.get("s"))
            price = valid_price(event.get("c"))
            opened = valid_price(event.get("o"))
            at = self.clock() if now is None else now
            event_at = int(event["E"]) / 1000
            if (asset is None or price is None or
                event_at < at - 30 or event_at > at + 15):
                return False
            previous = self._primary.get(asset)
            if (previous and previous["source"] == "Binance Spot WebSocket"
                and previous["event_at"] > event_at):
                return False
            self._save_primary(asset, price, at, event_at,
                               "Binance Spot WebSocket",
                               (price / opened - 1) * 100 if opened else None)
            return True
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return False

    def _save_primary(self, asset: str, price: float, at: float,
                      event_at: float, source: str, change: float | None) -> None:
        self._primary[asset] = {
            "price": price, "received_at": at, "event_at": event_at,
            "source": source, "change_24h_pct": round(change, 4) if change is not None else None,
        }
        history = self._ticks[asset]
        if not history or at - history[-1]["timestamp"] >= 1:
            history.append({"timestamp": at, "price_usdt": price})

    def ingest_rest(self, payload: Any, *, now: float | None = None) -> bool:
        if not isinstance(payload, dict):
            return False
        asset = SYMBOLS.get(payload.get("symbol"))
        price = valid_price(payload.get("price"))
        at = self.clock() if now is None else now
        if asset is None or price is None or not math.isfinite(at):
            return False
        previous = self._primary.get(asset)
        if previous and 0 <= at - previous["received_at"] <= 3:
            return True
        self._save_primary(asset, price, at, at, "Binance Spot REST", None)
        return True

    def ingest_gecko(self, payload: Any, *, now: float | None = None) -> int:
        """Require genuinely fresh CoinGecko USD timestamps; no zero/fake quotes."""
        if not isinstance(payload, dict):
            return 0
        at = self.clock() if now is None else now
        count = 0
        for asset, coin_id in GECKO_IDS.items():
            data = payload.get(coin_id)
            if not isinstance(data, dict):
                continue
            price = valid_price(data.get("usd"))
            updated = data.get("last_updated_at")
            if (price is None or type(updated) not in (int, float) or
                not math.isfinite(updated) or not 0 <= at - updated <= 180):
                continue
            self._reference[asset] = {
                "price": price, "received_at": at, "source": "CoinGecko USD",
            }
            count += 1
        return count

    def snapshot(self, *, now: float | None = None) -> dict[str, dict[str, Any]]:
        at = self.clock() if now is None else now
        result = {}
        for asset in EXTRA_ASSETS:
            primary = self._primary.get(asset)
            reference = self._reference.get(asset)
            primary_ok = bool(primary and 0 <= at - primary["received_at"] <= 10)
            reference_ok = bool(reference and 0 <= at - reference["received_at"] <= 120)
            divergence = (abs(primary["price"] - reference["price"]) /
                          reference["price"] * 100) if primary_ok and reference_ok else None
            verified = bool(primary_ok and reference_ok and divergence is not None
                            and divergence <= 2.5)
            reason = ("PRIMARY_MISSING_OR_STALE" if not primary_ok else
                      "REFERENCE_MISSING_OR_STALE" if not reference_ok else
                      "PRICE_DIVERGENCE" if not verified else "OK")
            result[asset] = {
                "asset": asset, "primary_pair": asset + "/USDT",
                "price_usdt": primary["price"] if primary_ok else None,
                "change_24h_pct": primary["change_24h_pct"] if primary_ok else None,
                "primary_source": primary["source"] if primary else None,
                "primary_at": iso(primary["event_at"]) if primary else None,
                "reference_price_usd": reference["price"] if reference_ok else None,
                "reference_source": reference["source"] if reference_ok else None,
                "reference_at": iso(reference["received_at"]) if reference else None,
                "price_divergence_pct": round(divergence, 4) if divergence is not None else None,
                "market_data_valid": verified, "reason": reason,
                "market_data_only": True, "trading_enabled": False,
            }
        return result

    def history(self, asset: str, *, limit: int = 120) -> list[dict[str, float]]:
        if asset not in EXTRA_ASSETS:
            raise ValueError("Unsupported read-only market")
        at = self.clock()
        data = [point for point in self._historical[asset]
                if 0 <= at - point["timestamp"] <= 7200]
        data.extend(point for point in self._ticks[asset]
                    if 0 <= at - point["timestamp"] <= 7200)
        data.sort(key=lambda point: point["timestamp"])
        return data[-max(1, min(limit, 3600)):]

    def ingest_candles(self, asset: str, rows: Any, *,
                       now: float | None = None) -> int:
        if asset not in EXTRA_ASSETS or not isinstance(rows, list) or len(rows) < 60:
            raise ValueError("Insufficient real candles")
        at = self.clock() if now is None else now
        previous = -1
        candles = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 7:
                raise ValueError("Invalid Binance candle")
            try:
                stamp = int(row[6]) / 1000
            except (TypeError, ValueError, OverflowError):
                raise ValueError("Invalid candle timestamp") from None
            value = valid_price(row[4])
            if (value is None or not math.isfinite(stamp) or
                not previous < stamp < at or stamp < at - 7200):
                raise ValueError("Stale, future or unordered candle")
            candles.append({"timestamp": stamp, "price_usdt": value})
            previous = stamp
        if at - candles[-1]["timestamp"] > 180:
            raise ValueError("Stale candle batch")
        self._historical[asset] = candles
        return len(candles)

    async def _ws_loop(self) -> None:
        delay = 1.0
        while True:
            try:
                async with websockets.connect(
                    WS_URL, open_timeout=12, ping_interval=20, ping_timeout=15,
                    close_timeout=5, max_size=65536,
                ) as socket:
                    self.connected = True
                    delay = 1.0
                    while True:
                        message = await asyncio.wait_for(socket.recv(), timeout=25)
                        self.ingest_ws(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"WebSocket: {type(exc).__name__}: {str(exc)[:100]}"
                LOGGER.warning("Read-only coin prices: %s", self.last_error)
            finally:
                self.connected = False
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2)

    async def _rest_loop(self) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0),
                                     follow_redirects=False) as client:
            while True:
                now = self.clock()
                missing = [asset for asset in EXTRA_ASSETS
                           if asset not in self._primary or
                           now - self._primary[asset]["received_at"] > 4]
                async def fetch(asset: str) -> Any:
                    response = await client.get(REST_URL,
                                                params={"symbol": asset + "USDT"})
                    response.raise_for_status()
                    return response.json()
                if missing:
                    results = await asyncio.gather(
                        *(fetch(asset) for asset in missing), return_exceptions=True)
                    for response in results:
                        if not isinstance(response, BaseException):
                            self.ingest_rest(response)
                await asyncio.sleep(6)

    async def _gecko_loop(self) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0),
                                     follow_redirects=False) as client:
            while True:
                try:
                    response = await client.get(GECKO_URL, params={
                        "ids": ",".join(GECKO_IDS.values()),
                        "vs_currencies": "usd",
                        "include_last_updated_at": "true",
                    })
                    response.raise_for_status()
                    if not self.ingest_gecko(response.json()):
                        raise ValueError("No fresh CoinGecko reference prices")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = f"Reference: {type(exc).__name__}: {str(exc)[:100]}"
                    LOGGER.warning("Read-only coin reference: %s", self.last_error)
                await asyncio.sleep(30)

    async def _history_loop(self) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0),
                                     follow_redirects=False) as client:
            while True:
                for asset in EXTRA_ASSETS:
                    try:
                        now = self.clock()
                        end_time = (int(now) // 60 * 60 - 1) * 1000
                        response = await client.get(CANDLES_URL, params={
                            "symbol": asset + "USDT", "interval": "1m",
                            "limit": 90, "endTime": end_time,
                        })
                        response.raise_for_status()
                        self.ingest_candles(asset, response.json(), now=now)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOGGER.warning("Read-only %s chart unavailable: %s",
                                       asset, str(exc)[:100])
                await asyncio.sleep(300)
