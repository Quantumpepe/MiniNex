"""Central cached official FX reference rates, exclusively for display."""
import asyncio
from datetime import date
import math
import time
import httpx
from fastapi import HTTPException

CURRENCIES = ("CHF", "EUR", "GBP", "JPY")
URL = "https://api.frankfurter.dev/v2/rates"
_lock = asyncio.Lock()
_cached = None
_checked = 0.0


def parse_reference_rates(rows):
    if not isinstance(rows, list):
        raise ValueError("Invalid FX feed")
    rates, dates = {"USD": 1}, {}
    for row in rows:
        if not isinstance(row, dict) or row.get("base") != "USD":
            raise ValueError("Invalid FX base")
        code, rate = row.get("quote"), row.get("rate")
        if (code not in CURRENCIES or code in dates or type(rate) not in (int, float)
                or not math.isfinite(rate) or rate <= 0):
            raise ValueError("Invalid FX rate")
        published = date.fromisoformat(row["date"])
        if not 0 <= (date.today() - published).days <= 7:
            raise ValueError("FX reference date unavailable")
        dates[code], rates[code] = published.isoformat(), rate
    if set(dates) != set(CURRENCIES):
        raise ValueError("Incomplete FX feed")
    return {"base": "USD", "rates": rates, "dates": dates,
            "source": "ECB via Frankfurter", "reference_only": True}


async def get_display_currency_rates():
    global _cached, _checked
    async with _lock:
        if _cached is not None and time.monotonic() - _checked < 3600:
            return _cached
        try:
            async with httpx.AsyncClient(timeout=7, follow_redirects=False) as client:
                response = await client.get(URL, params={
                    "base": "USD", "quotes": ",".join(CURRENCIES), "providers": "ecb"})
                response.raise_for_status()
                result = parse_reference_rates(response.json())
            _cached, _checked = result, time.monotonic()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            if _cached is None or any(
                    (date.today() - date.fromisoformat(d)).days > 7
                    for d in _cached["dates"].values()):
                raise HTTPException(503, "FX reference rates unavailable") from None
            _checked = time.monotonic() - 3300
        return _cached
