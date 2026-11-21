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
    if pro:
        return PRO_BASE + path, {"x-cg-pro-api-key": pro}, AUTH_INTERVAL
    if demo:
        return PUBLIC_BASE + path, {"x-cg-demo-api-key": demo}, AUTH_INTERVAL
    return PUBLIC_BASE + path, {}, PUBLIC_INTERVAL
