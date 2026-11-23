"""Live liquidity quotes for native MiniNex wallet-swaps (no funds moved).

Reads the REAL THORChain public quote provider, one manual user request at a
time. A successful quote DOES NOT mean that the asset pool, native signer,
inbound vault, swap memo, refund or transfer executor are approved.
No provider-supplied deposit address/memo can reach the mobile app.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
import time

import httpx
from fastapi import HTTPException

from .live_btc_quotes import (
    BTC_QUOTE_URL, MCP_HEADERS, _amount_to_mcp_units, _provider_quote,
    _decode_mcp_quote_response, ProviderMinimumNotMet,
)
from .live_eth_quotes import STABLES
from .manual_swap_routes import ALL_SWAP_ASSETS, _asset_network, build_manual_swap_route

# Addresses, chains and decimals are fixed server-side, never user-supplied.
# Token pool availability is verified at quote time.
THOR_ASSETS = {
    "BTC": ("BTC.BTC", 8),
    "ETH": ("ETH.ETH", 18),
    "BNB": ("BSC.BNB", 18),
    "SOL": ("SOL.SOL", 9),
    "XRP": ("XRP.XRP", 6),
    "TRX": ("TRON.TRX", 6),
    "POL": ("POL.POL", 18),
    "AVAX": ("AVAX.AVAX", 18),
    "LINK": ("ETH.LINK-0X514910771AF9CA656AF840DFF83E8264ECF986CA", 18),
    "QNT": ("ETH.QNT-0X4A220E6096B25EADB88358CB44068A3248254675", 18),
    "UNI": ("ETH.UNI-0X1F9840A85D5AF5BF1D1762F925BDADDC4201F984", 18),
    "ARB": ("ARB.ARB", 18),  # Arbitrum – pool availability checked at quote time
    "USDC": ("ETH.USDC-" + STABLES["USDC"].upper(), 6),
    "USDT": ("ETH.USDT-" + STABLES["USDT"].upper(), 6),
}
assert set(THOR_ASSETS) == set(ALL_SWAP_ASSETS)
# Never let an unresponsive public provider exhaust Render's worker slots.
_QUOTE_SEMAPHORE = asyncio.Semaphore(4)
_LOG=logging.getLogger("mininex.manual_crosschain_quote")


def validate_live_quote_pair(source: object, target: object,
                             amount: object) -> tuple[str, str, str]:
    if type(source) is not str or type(target) is not str:
        raise HTTPException(422, "Invalid swap assets")
    build_manual_swap_route(source, target)
    asset_from, decimals = THOR_ASSETS[source]
    asset_to = THOR_ASSETS[target][0]
    # THORChain public API amounts are 1e8 atomic units, not token wei.
    # Never round the user's requested number to a different amount.
    _, units = _amount_to_mcp_units(amount, min(decimals, 8))
    if len(units) > 16:
        raise HTTPException(422, "Swap quote exceeds supported value")
    return asset_from, asset_to, units


async def get_native_swap_quote(source: str, target: str, amount: str) -> dict:
    """One real provider request, no wallet signer, no background polling."""
    from_asset, to_asset, units = validate_live_quote_pair(source, target, amount)
    # Do not provide 'destination' to the provider: prevents live vault
    # funding instructions, memo leakage or accidental chain transfers.
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "quote_swap",
            "arguments": {
                "from_asset": from_asset,
                "to_asset": to_asset,
                "amount": int(units),
            },
        },
    }
    acquired = False
    try:
        await asyncio.wait_for(_QUOTE_SEMAPHORE.acquire(), timeout=1.0)
        acquired = True
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9.0), follow_redirects=False,
        ) as client:
            response = await client.post(
                BTC_QUOTE_URL, json=payload, headers=MCP_HEADERS,
            )
            response.raise_for_status()
            quote = _provider_quote(
                _decode_mcp_quote_response(response), from_asset=from_asset,
                to_asset=to_asset, input_units=units,
            )
    except ProviderMinimumNotMet as exc:
        # Exact public venue minimum, measured in THORChain's 1e8 quote units.
        # Never replace it with a hard-coded application trading limit.
        minimum=format(Decimal(exc.minimum_units) / Decimal(10**8), "f")
        raise HTTPException(409, detail={
            "code": "PROVIDER_MINIMUM",
            "source_asset": source,
            "minimum_source_amount": minimum,
        }) from None
    except (httpx.HTTPError, ValueError, TypeError, OverflowError,
            asyncio.TimeoutError) as exc:
        # Public asset symbols and our own fixed parser error messages only.
        # Never log wallet addresses, upstream payloads, inbound instructions
        # or an HTTP exception's raw URL/body.
        reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
        _LOG.warning("Cross-chain quote unavailable %s -> %s reason=%s",
                     source,target,reason[:100])
        raise HTTPException(503, "Real swap venue quote unavailable") from None
    finally:
        if acquired:
            _QUOTE_SEMAPHORE.release()

    amount_out = format(
        Decimal(quote["estimated_output_units_1e8"]) / Decimal(10**8), "f"
    )
    incoming = _asset_network(source)
    outgoing = _asset_network(target)
    return {
        "mode": "NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY",
        "source_asset": source,
        "destination_asset": target,
        "source_network": incoming["network"],
        "destination_network": outgoing["network"],
        "input_amount": amount,
        "estimated_output_amount": amount_out,
        "quoted_price_impact_bps": quote["slippage_bps"],
        "quote_checked_at": int(time.time()),
        "quote_expires_at": quote["provider_expiry"],
        "quote_provider": "THORCHAIN_PUBLIC_MCP",
        "pool_route_quoted": True,
        "fee_breakdown_verified": False,
        "minimum_received_guaranteed": False,
        "deposit_address_provided": False,
        "signing_authorized": False,
        "transaction_built": False,
        "execution_enabled": False,
        "can_submit": False,
        "warning": (
            "Real live protocol price, not an executable swap. Native wallet "
            "signatures, inbound vault, expiry, finality and refunds must "
            "be independently verified before any funds can be sent."
        ),
    }
