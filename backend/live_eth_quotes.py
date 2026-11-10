"""Read-only Ethereum MAINNET swap quote preflight for MiniNex LIVE.

No signer, transaction calldata, approval, order placement or balance mutation.
User-provided addresses, chain IDs, recipients and routers are never accepted.
Even a valid quote is NOT permission to trade. Every future execution must
independently reverify wallet ownership, funds, spend limits and a fresh quote.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import os
import re
import time

import httpx
from fastapi import HTTPException

from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import get_wallet_balances, verified_addresses

ETH_NATIVE = "0x0000000000000000000000000000000000000000"
STABLES = {
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
}
UNISWAP_QUOTE_URL = "https://trade-api.gateway.uniswap.org/v1/quote"
ETH_GAS_RESERVE = Decimal("0.005")
SLIPPAGE_PCT = 0.50
_QUOTE_TTL_SECONDS = 15
_AMOUNT = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_HEX_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _quantity(raw: object, decimals: int) -> tuple[Decimal, str]:
    """Parse exact positive amounts; never silently round user spending."""
    if not isinstance(raw, str) or len(raw) > 60 or not _AMOUNT.fullmatch(raw):
        raise HTTPException(422, "Invalid decimal amount")
    try:
        amount = Decimal(raw)
        units = amount * (10**decimals)
    except (InvalidOperation, ValueError, OverflowError):
        raise HTTPException(422, "Invalid decimal amount") from None
    if amount <= 0 or units != units.to_integral_value() or units >= 10**45:
        raise HTTPException(422, "Amount must be positive and exact for token decimals")
    return amount, str(int(units))


def _valid_units(raw: object) -> int:
    if (not isinstance(raw, str) or len(raw) > 50
            or not raw.isascii() or not raw.isdecimal()):
        raise ValueError("Invalid quote units")
    result = int(raw)
    if result <= 0 or result >= 10**45:
        raise ValueError("Invalid quote units")
    return result


def _read_quote(payload: object, *, token_in: str, token_out: str,
                amount_units: str, wallet: str) -> tuple[str, str]:
    """Accept CLASSIC same-chain ETH swaps only; reject provider surprises."""
    if not isinstance(payload, dict) or payload.get("routing") != "CLASSIC":
        raise ValueError("Unsafe or unsupported route")
    quote = payload.get("quote")
    if not isinstance(quote, dict) or payload.get("txFailureReason"):
        raise ValueError("Invalid quoted route")
    source, destination = quote.get("input"), quote.get("output")
    if not isinstance(source, dict) or not isinstance(destination, dict):
        raise ValueError("Missing quoted amounts")
    if (str(source.get("token", "")).lower() != token_in.lower()
            or str(destination.get("token", "")).lower() != token_out.lower()):
        raise ValueError("Wrong quoted tokens")
    if str(destination.get("recipient", wallet)).lower() != wallet.lower():
        raise ValueError("Wrong quote recipient")
    if str(quote.get("swapper", wallet)).lower() != wallet.lower():
        raise ValueError("Wrong quote swapper")
    if _valid_units(source.get("amount")) != int(amount_units):
        raise ValueError("Quoted input differs from requested input")
    output = _valid_units(destination.get("amount"))
    minimum = _valid_units(destination.get("minimumAmount"))
    # A provider cannot relax the configured 0.50% minimum received.
    # Maximum quote slippage is 0.50%: min >= floor(199/200 * expected) - 1 base unit.
    if minimum > output or (minimum + 1) * 200 < output * 199:
        raise ValueError("Invalid minimum output")
    if quote.get("portionBips") not in (None, 0, "0"):
        raise ValueError("Unexpected additional route fee")
    return str(output), str(minimum)


def quote_readiness() -> dict:
    return {
        "asset": "ETH",
        "chain_id": 1,
        "supported_settlement_tokens": ["USDC", "USDT"],
        "quote_provider_configured": bool(os.getenv("MININEX_UNISWAP_API_KEY", "").strip()),
        "privy_wallet_verification_configured": bool(os.getenv("MININEX_PRIVY_APP_SECRET", "").strip()),
        "quote_only": True,
        "execution_enabled": False,
        "automated_signing_enabled": False,
        "orders_enabled": False,
    }


async def quote_eth_mainnet(did: str, *, side: str, stable: str, amount: str) -> dict:
    """Get a real Uniswap quote for a verified user's Ethereum wallet.

    This is strictly a read-only preparation step, not an order.
    Only an independent wallet/balance read and a static Uniswap endpoint
    are allowed to influence the response.
    """
    if side not in ("BUY", "SELL") or stable not in STABLES:
        raise HTTPException(422, "Unsupported swap")
    input_symbol = stable if side == "BUY" else "ETH"
    output_symbol = "ETH" if side == "BUY" else stable
    value, atomic = _quantity(amount, 6 if side == "BUY" else 18)
    api_key = os.getenv("MININEX_UNISWAP_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "Uniswap quote provider not configured")

    user = await fetch_privy_user(did)
    try:
        address = verified_addresses(user, did)["ETH"]
    except ValueError:
        raise HTTPException(503, "Unambiguous verified Ethereum wallet required") from None
    if not address or not _HEX_ADDR.fullmatch(address):
        raise HTTPException(409, "Verified Ethereum wallet missing")

    # Read actual blockchain balances. Unavailable data is NOT a zero balance.
    verified_balance_snapshot = await get_wallet_balances(did)
    snapshot_wallet = verified_balance_snapshot.get("wallets", {}).get("ETH")
    if not isinstance(snapshot_wallet, str) or snapshot_wallet.lower() != address.lower():
        raise HTTPException(503, "Wallet identity changed during balance verification")
    balances = verified_balance_snapshot.get("balances", {})
    spending = balances.get(input_symbol, {})
    gas = balances.get("ETH", {})
    if spending.get("status") != "verified" or gas.get("status") != "verified":
        raise HTTPException(503, "Verified mainnet balance unavailable")
    try:
        available = Decimal(spending["balance"])
        eth_available = Decimal(gas["balance"])
    except (InvalidOperation, ValueError, TypeError, KeyError):
        raise HTTPException(503, "Invalid verified balance") from None
    if not available.is_finite() or not eth_available.is_finite() or available < 0 or eth_available < 0:
        raise HTTPException(503, "Invalid verified balance")
    if value > available:
        raise HTTPException(409, "Insufficient verified input balance")
    if eth_available - (value if side == "SELL" else 0) <= ETH_GAS_RESERVE:
        raise HTTPException(409, "Ethereum balance insufficient for gas reserve")

    token_in = STABLES[stable] if side == "BUY" else ETH_NATIVE
    token_out = ETH_NATIVE if side == "BUY" else STABLES[stable]
    payload = {
        "type": "EXACT_INPUT",
        "amount": atomic,
        "tokenInChainId": 1,
        "tokenOutChainId": 1,
        "tokenIn": token_in,
        "tokenOut": token_out,
        "swapper": address,
        "recipient": address,
        "protocols": ["V3"],
        "routingPreference": "BEST_PRICE",
        "slippageTolerance": SLIPPAGE_PCT,
        "permitAmount": "EXACT",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(9.0), follow_redirects=False) as client:
            response = await client.post(
                UNISWAP_QUOTE_URL,
                json=payload,
                headers={"x-api-key": api_key, "Accept": "application/json",
                         "Content-Type": "application/json"},
            )
            response.raise_for_status()
            if len(response.content) > 192_000:
                raise ValueError("Oversized quote")
            raw = response.json()
        out_atomic, min_atomic = _read_quote(
            raw, token_in=token_in, token_out=token_out,
            amount_units=atomic, wallet=address,
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise HTTPException(502, "Verified same-chain Uniswap quote unavailable") from None

    output_decimals = 18 if side == "BUY" else 6
    divisor = Decimal(10)**output_decimals
    return {
        "mode": "LIVE_QUOTE_ONLY",
        "asset": "ETH",
        "chain_id": 1,
        "side": side,
        "input_asset": input_symbol,
        "output_asset": output_symbol,
        "input_amount": str(value),
        "expected_output_amount": str(Decimal(out_atomic) / divisor),
        "minimum_output_amount": str(Decimal(min_atomic) / divisor),
        "slippage_tolerance_pct": SLIPPAGE_PCT,
        "quote_expires_at": int(time.time()) + _QUOTE_TTL_SECONDS,
        "route": "UNISWAP_V3_CLASSIC",
        "execution_enabled": False,
        "signing_authorized": False,
        "transaction_submitted": False,
        "warning": "Read-only live quote. No order or trading permission. Requote before any future execution.",
    }
