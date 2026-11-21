"""One server-only CoinGecko transport configuration for Demo and paid API plans.

Do not put API credentials in the APK, frontend, URL query or public endpoints.
Adding MININEX_COINGECKO_PRO_API_KEY to the Render backend switches all callers
of this adapter to CoinGecko's paid API. No trading permission is involved.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping

PUBLIC_BASE = "https://api.coingecko.com/api/v3"
PRO_BASE = "https://pro-api.coingecko.com/api/v3"
PUBLIC_INTERVAL = 120
AUTH_INTERVAL = 60

# Covers the current twelve in one batched call; settlement stablecoins are separate.
MARKET_ASSETS = ("BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "POL", "AVAX", "LINK", "DOGE", "UNI", "ARB")

# Canonical CoinGecko IDs; never derive them from an untrusted ticker.
COIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "XRP": "ripple", "TRX": "tron",
    "POL": "polygon-ecosystem-token", "AVAX": "avalanche-2",
    "LINK": "chainlink", "DOGE": "dogecoin",
    "UNI": "uniswap", "ARB": "arbitrum",
    "USDC": "usd-coin", "USDT": "tether",
}


def coingecko_request_settings(path: str, environ: Mapping[str, str] | None = None
                               ) -> tuple[str, dict[str, str], int]:
    """Return a vetted request URL, secret HTTP header and conservative poll period.

    One route for both free and Pro endpoints. The allowlist prevents callers
    turning this credential-bearing backend transport into an arbitrary proxy.
    """
    historical = re.fullmatch(r"/coins/([a-z0-9-]+)/market_chart/range", path)
    if path != "/simple/price" and (
        historical is None or historical.group(1) not in COIN_IDS.values()
    ):
        raise ValueError("Unsupported CoinGecko API path")

    env = os.environ if environ is None else environ
    pro = str(env.get("MININEX_COINGECKO_PRO_API_KEY", "")).strip()
    demo = str(env.get("MININEX_COINGECKO_DEMO_API_KEY", "")).strip()
    # Basic, Analyst, Lite and Pro all use the SAME paid endpoint/header. No
    # plan-specific code, keys or permissions are needed when the plan changes.
    if pro:
        base, headers, interval = PRO_BASE, {"x-cg-pro-api-key": pro}, AUTH_INTERVAL
    elif demo:
        base, headers, interval = PUBLIC_BASE, {"x-cg-demo-api-key": demo}, AUTH_INTERVAL
    else:
        base, headers, interval = PUBLIC_BASE, {}, PUBLIC_INTERVAL

    # A shared Basic subscription can be used by Nexus and MiniNex; operators
    # may lower *reference-data polling* to leave credits for both projects.
    # This does NOT change user trading budgets, signals, or order frequency.
    setting = str(env.get("MININEX_COINGECKO_REFERENCE_POLL_SECONDS", "")).strip()
    if setting.isascii() and setting.isdecimal() and len(setting) <= 4:
        seconds = int(setting)
        if 60 <= seconds <= 150:
            interval = max(interval, seconds)
    return base + path, headers, interval
