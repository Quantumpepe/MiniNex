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


class MarketDataService:
    """Small in-memory collector; one instance/worker, no database required yet."""

    def __init__(self, *, clock: Any = time.time) -> None:
        self._clock = clock
        self.ws_url = os.getenv("MININEX_MARKET_WS_URL", DEFAULT_WS)
        self.reference_url = os.getenv("MININEX_REFERENCE_URL", DEFAULT_REFERENCE_URL)
        self.reference_period_seconds = max(15.0, float(os.getenv("MININEX_REFERENCE_INTERVAL", "25")))
        self.max_primary_age_seconds = 10.0
        self.max_reference_age_seconds = 90.0
        self.max_divergence_pct = 1.5
        self._primary: dict[str, dict[str, Any]] = {}
        self._reference: dict[str, dict[str, Any]] = {}
        self._history: dict[str, deque] = {asset: deque(maxlen=3600) for asset in ASSETS}
        self.primary_connected = False
        self.last_primary_error: str | None = None
        self.last_reference_error: str | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._primary_loop(), name="market-binance"),
            asyncio.create_task(self._reference_loop(), name="market-kraken"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.primary_connected = False

    def ingest_primary(self, payload: str | dict[str, Any], *, now: float | None = None) -> bool:
        """Ingest a validated WS packet. Public for deterministic offline tests."""
        tick = parse_binance_miniticker(payload, now=self._clock() if now is None else now)
        if tick is None:
            return False
        asset = tick["asset"]
        old = self._primary.get(asset)
        if old and tick["event_at"] < old["event_at"]:
            return False  # ignore out-of-order packets
        self._primary[asset] = tick
        history = self._history[asset]
        if not history or tick["received_at"] - history[-1]["timestamp"] >= 1.0:
            history.append({"timestamp": tick["received_at"], "price_usdt": tick["price"]})
        return True

    def ingest_reference(self, payload: dict[str, Any], *, now: float | None = None) -> bool:
        prices = parse_kraken_ticker(payload)
        if not prices:
            return False
        at = self._clock() if now is None else now
        for asset, price in prices.items():
            self._reference[asset] = {"price": price, "received_at": at}
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
            verified = bool(self.primary_connected and divergence is not None and divergence <= self.max_divergence_pct)
            if not primary_fresh:
                reason = "PRIMARY_MISSING_OR_STALE"
            elif not self.primary_connected:
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
                "primary_source": "Binance Spot WebSocket",
                "primary_at": utc_iso(primary["event_at"]) if primary else None,
                "reference_price_usd": reference["price"] if reference_fresh else None,
                "reference_source": "Kraken Spot",
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
            "markets": markets,
        }

    def history(self, asset: str, *, limit: int = 120) -> list[dict[str, Any]]:
        if asset not in ASSETS:
            raise ValueError("BTC or ETH required")
        return list(self._history[asset])[-max(1, min(limit, 3600)):]

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
                    async for packet in stream:
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), follow_redirects=False) as client:
            while True:
                try:
                    response = await client.get(self.reference_url, params={"pair": "XBTUSD,ETHUSD"})
                    response.raise_for_status()
                    if not self.ingest_reference(response.json()):
                        raise ValueError("Kraken returned no valid BTC/USD or ETH/USD bid/ask")
                    self.last_reference_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_reference_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                    LOGGER.warning("Independent Kraken price check failed: %s", self.last_reference_error)
                await asyncio.sleep(self.reference_period_seconds)