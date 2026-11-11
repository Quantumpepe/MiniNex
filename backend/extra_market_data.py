"""Read-only public spot-market previews for ten additional MiniNex coins.

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
import os
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import websockets

LOGGER = logging.getLogger("mininex.extra_markets")
EXTRA_ASSETS = ("BNB", "SOL", "XRP", "TRX", "POL", "AVAX", "LINK", "DOGE", "UNI", "ADA")
GECKO_IDS = {
    "BNB": "binancecoin",
    "POL": "polygon-ecosystem-token",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "TRX": "tron",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "DOGE": "dogecoin",
    "UNI": "uniswap",
}
SYMBOLS = {asset + "USDT": asset for asset in EXTRA_ASSETS}
WS_URL = ("wss://data-stream.binance.vision/stream?streams=" +
          "/".join(symbol.lower() + "@miniTicker" for symbol in SYMBOLS))
REST_URL = "https://data-api.binance.vision/api/v3/ticker/price"
CANDLES_URL = "https://data-api.binance.vision/api/v3/klines"
GECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
GECKO_PRO_URL = "https://pro-api.coingecko.com/api/v3/simple/price"
KRAKEN_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
# The public CoinGecko limit can be shared with other Render clients.
# One batched request for ten coins every two minutes is conservative.
GECKO_PUBLIC_INTERVAL = 120
GECKO_AUTH_INTERVAL = 60
GECKO_BACKOFF_CAP = 1800
KRAKEN_REFERENCE_INTERVAL = 60
KRAKEN_REFERENCE_TTL = 100
GECKO_REFERENCE_TTL = 180


def gecko_request_settings(environ: dict | None = None) -> tuple[str, dict[str, str], int]:
    """Provider key is server-only; never expose in a frontend response."""
    env = os.environ if environ is None else environ
    pro = str(env.get("MININEX_COINGECKO_PRO_API_KEY", "")).strip()
    demo = str(env.get("MININEX_COINGECKO_DEMO_API_KEY", "")).strip()
    if pro:
        return GECKO_PRO_URL, {"x-cg-pro-api-key": pro}, GECKO_AUTH_INTERVAL
    if demo:
        return GECKO_URL, {"x-cg-demo-api-key": demo}, GECKO_AUTH_INTERVAL
    return GECKO_URL, {}, GECKO_PUBLIC_INTERVAL


def retry_after_seconds(raw: object, *, now: float) -> int | None:
    """Respect Retry-After seconds or HTTP-date, with a safe bounded ceiling."""
    if not isinstance(raw, str) or len(raw) > 100:
        return None
    value = raw.strip()
    try:
        if value.isascii() and value.isdecimal():
            seconds = int(value)
        else:
            stamp = parsedate_to_datetime(value)
            seconds = math.ceil(stamp.timestamp() - now)
        return min(3600, max(0, seconds))
    except (ValueError, TypeError, OverflowError, IndexError):
        return None


def gecko_backoff_seconds(failures: int, *, retry_after: object = None,
                          now: float, base_interval: int = GECKO_PUBLIC_INTERVAL) -> int:
    """First HTTP 429 waits >=5 min; repeated 429s wait up to 30 min."""
    count = max(1, min(20, int(failures)))
    exponential = min(GECKO_BACKOFF_CAP, 300 * (2 ** min(count - 1, 5)))
    provider_hint = retry_after_seconds(retry_after, now=now)
    return max(base_interval, exponential, provider_hint or 0)


def discover_kraken_usd_pairs(payload: object) -> dict[str, str]:
    """Discover exact exchange USD pair names; never infer or accept USDT pairs."""
    if not isinstance(payload, dict) or payload.get("error") != []:
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    matches: dict[str, str] = {}
    for pair_key, info in result.items():
        if not isinstance(pair_key, str) or not isinstance(info, dict):
            continue
        if info.get("status", "online") != "online":
            continue
        wsname = info.get("wsname")
        if not isinstance(wsname, str) or not wsname.endswith("/USD"):
            continue
        base = wsname[:-4]
        asset = "DOGE" if base in ("XDG", "DOGE") else base
        if asset in EXTRA_ASSETS and asset not in matches:
            matches[asset] = pair_key
    return matches



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
        self._kraken_reference: dict[str, dict[str, Any]] = {}
        self._kraken_pairs: dict[str, str] = {}
        self._kraken_discover_after = 0.0
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
            asyncio.create_task(self._kraken_loop(), name="extra-market-kraken-backup"),
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
                "price": price, "received_at": at, "source_at": updated,
                "source": "CoinGecko USD",
            }
            count += 1
        return count

    def ingest_kraken(self, payload: Any, *, now: float | None = None) -> int:
        """Read independent USD bid/ask midpoints with exact pair IDs."""
        if not isinstance(payload, dict) or payload.get("error") != []:
            return 0
        rows = payload.get("result")
        if not isinstance(rows, dict):
            return 0
        at = self.clock() if now is None else now
        count = 0
        for asset, pair in self._kraken_pairs.items():
            entry = rows.get(pair)
            if not isinstance(entry, dict):
                continue
            bids, asks = entry.get("b"), entry.get("a")
            bid = valid_price(bids[0]) if isinstance(bids, list) and bids else None
            ask = valid_price(asks[0]) if isinstance(asks, list) and asks else None
            if bid is None or ask is None or ask < bid or (ask - bid) / bid > 0.025:
                continue
            self._kraken_reference[asset] = {
                "price": (bid + ask) / 2, "received_at": at,
                "source": "Kraken Spot USD",
            }
            count += 1
        return count

    def _fresh_reference(self, asset: str, at: float) -> dict | None:
        gecko = self._reference.get(asset)
        if gecko and 0 <= at - gecko["received_at"] <= GECKO_REFERENCE_TTL and (
            0 <= at - gecko.get("source_at", gecko["received_at"]) <= 240
        ):
            return gecko
        kraken = self._kraken_reference.get(asset)
        if kraken and 0 <= at - kraken["received_at"] <= KRAKEN_REFERENCE_TTL:
            return kraken
        return None

    def snapshot(self, *, now: float | None = None) -> dict[str, dict[str, Any]]:
        at = self.clock() if now is None else now
        result = {}
        for asset in EXTRA_ASSETS:
            primary = self._primary.get(asset)
            reference = self._fresh_reference(asset, at)
            primary_ok = bool(primary and 0 <= at - primary["received_at"] <= 10)
            reference_ok = reference is not None
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
        """Single batched CoinGecko request, 429-aware backoff, optional future key."""
        url, headers, interval = gecko_request_settings()
        failures = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0),
                                     follow_redirects=False) as client:
            while True:
                delay = interval
                try:
                    response = await client.get(url, params={
                        "ids": ",".join(GECKO_IDS.values()),
                        "vs_currencies": "usd",
                        "include_last_updated_at": "true",
                    }, headers=headers)
                    if response.status_code == 429:
                        failures += 1
                        delay = gecko_backoff_seconds(
                            failures, retry_after=response.headers.get("Retry-After"),
                            now=self.clock(), base_interval=interval,
                        )
                        self.last_error = "Reference: CoinGecko HTTP 429 (cooldown)"
                        LOGGER.warning("CoinGecko rate limited; pausing %d seconds; "
                                       "Kraken independent fallback remains available", delay)
                    else:
                        response.raise_for_status()
                        if len(response.content) > 196_000:
                            raise ValueError("Oversized CoinGecko response")
                        if not self.ingest_gecko(response.json()):
                            raise ValueError("No fresh CoinGecko reference prices")
                        failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    delay = max(interval, min(GECKO_BACKOFF_CAP, 120 * 2 ** min(failures, 4)))
                    self.last_error = "Reference: CoinGecko unavailable"
                    LOGGER.warning("CoinGecko reference unavailable (%s); retry in %d s",
                                   type(exc).__name__, delay)
                await asyncio.sleep(delay)

    async def _kraken_loop(self) -> None:
        """Independent, read-only USD backup if CoinGecko is throttled.

        Kraken pair names are discovered from its market metadata instead of
        assuming that every asset has a USD market or using a wrong token.
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0),
                                     follow_redirects=False) as client:
            while True:
                now = self.clock()
                if now >= self._kraken_discover_after:
                    self._kraken_discover_after = now + 600
                    try:
                        response = await client.get(KRAKEN_PAIRS_URL)
                        response.raise_for_status()
                        if len(response.content) > 5_000_000:
                            raise ValueError("Oversized Kraken market listing")
                        pairs = discover_kraken_usd_pairs(response.json())
                        if pairs:
                            self._kraken_pairs = pairs
                            self._kraken_discover_after = now + 21600
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOGGER.warning("Kraken reference pair discovery unavailable: %s",
                                       type(exc).__name__)
                if self._kraken_pairs:
                    try:
                        response = await client.get(KRAKEN_TICKER_URL, params={
                            "pair": ",".join(self._kraken_pairs.values()),
                        })
                        response.raise_for_status()
                        if len(response.content) > 256_000:
                            raise ValueError("Oversized Kraken USD quotes")
                        self.ingest_kraken(response.json())
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOGGER.warning("Kraken backup reference unavailable: %s",
                                       type(exc).__name__)
                await asyncio.sleep(KRAKEN_REFERENCE_INTERVAL)

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
