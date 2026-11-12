"""Native BTC ↔ Ethereum stablecoin read-only cross-chain quote preflight.

The BTC engine MUST use native UTXO Bitcoin, not wrapped BTC. A route to
Ethereum USDC/USDT settles to the user's independently verified ETH wallet.
This service only asks the public THORChain Swap MCP endpoint for INDICATIVE
quotes. It intentionally omits destination, so no inbound address/memo is
provided or actionable. It never signs BTC, sends ETH or initiates settlement.
Production requires an authenticated swap aggregator, sender-side UTXO safety,
refund/finality handling, nonce/idempotency, and real source/destination
receipt reconciliation BEFORE offering automation.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
import time

import httpx
from fastapi import HTTPException

from .live_eth_quotes import STABLES, ETH_GAS_RESERVE
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import get_wallet_balances, verified_addresses

BTC_QUOTE_URL = "https://swap.thorchain.org/mcp"
BTC_GAS_RESERVE = Decimal("0.0001")
MAX_SLIPPAGE_BPS = 500  # Indicative filter only. Not an executable min-out.
MAX_INPUT_UNITS = 10**15
_AMOUNT = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_UNITS = re.compile(r"^[1-9][0-9]{0,17}$")


def _amount_to_mcp_units(value: object, decimals: int) -> tuple[Decimal, str]:
    if not isinstance(value, str) or len(value) > 48 or not _AMOUNT.fullmatch(value):
        raise HTTPException(422, "Invalid native BTC quote amount")
    try:
        amount = Decimal(value)
        token_units = amount * (Decimal(10) ** decimals)
        mcp_units = amount * Decimal(10**8)
    except (InvalidOperation, ValueError, OverflowError):
        raise HTTPException(422, "Invalid native BTC quote amount") from None
    if (not amount.is_finite() or amount <= 0
            or token_units != token_units.to_integral_value()
            or mcp_units != mcp_units.to_integral_value()
            or not 0 < mcp_units < MAX_INPUT_UNITS):
        raise HTTPException(422, "Quote amount exceeds native token precision")
    return amount, str(int(mcp_units))


def _decimal(raw: object) -> Decimal:
    if not isinstance(raw, str):
        raise ValueError("Invalid verified account balance")
    try:
        result = Decimal(raw)
    except InvalidOperation:
        raise ValueError("Invalid verified account balance") from None
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid verified account balance")
    return result


def _provider_quote(raw: object, *, from_asset: str,
                    to_asset: str, input_units: str) -> dict:
    if not isinstance(raw, dict) or raw.get("jsonrpc") != "2.0" or raw.get("id") != 1:
        raise ValueError("Incorrect cross-chain quote response")
    if raw.get("error") is not None:
        raise ValueError("Cross-chain route unavailable")
    result = raw.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        raise ValueError("Cross-chain provider returned no valid quote")
    parsed = result.get("structuredContent")
    if parsed is None:
        content = result.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise ValueError("Ambiguous cross-chain route")
        item = content[0]
        if not isinstance(item, dict) or item.get("type") != "text":
            raise ValueError("Unsupported cross-chain response")
        plain = item.get("text")
        if not isinstance(plain, str) or len(plain) > 200_000:
            raise ValueError("Cross-chain quote malformed")
        try:
            parsed = json.loads(plain)
        except ValueError:
            raise ValueError("Cross-chain quote malformed") from None
    if isinstance(parsed, dict) and isinstance(parsed.get("quote"), dict):
        parsed = parsed["quote"]
    if not isinstance(parsed, dict):
        raise ValueError("Cross-chain quote malformed")
    # A quote with an inbound address/memo can become a real funding
    # instruction. Never expose either to clients in this read-only service.
    if "inbound_address" in parsed or "memo" in parsed:
        raise ValueError("Unexpected executable cross-chain swap instructions")
    for key, expected in (("from_asset", from_asset), ("to_asset", to_asset)):
        if key in parsed and parsed[key] != expected:
            raise ValueError("Cross-chain quote asset mismatch")
    if "amount" in parsed and str(parsed["amount"]) != input_units:
        raise ValueError("Cross-chain quote input mismatch")
    output = parsed.get("expected_amount_out")
    if not isinstance(output, str) or not _UNITS.fullmatch(output):
        raise ValueError("Unverified cross-chain quote output")
    slippage = parsed.get("slippage_bps")
    if isinstance(slippage, bool) or not isinstance(slippage, (int,str)):
        raise ValueError("Cross-chain slippage unavailable")
    if not str(slippage).isascii() or not str(slippage).isdecimal():
        raise ValueError("Invalid cross-chain slippage")
    slippage = int(slippage)
    if slippage > MAX_SLIPPAGE_BPS:
        raise ValueError("Cross-chain price impact too high")
    recommended = parsed.get("recommended_min_amount_in")
    if recommended is not None:
        if not isinstance(recommended, str) or not _UNITS.fullmatch(recommended):
            raise ValueError("Unverified minimum swap quantity")
        if int(input_units) < int(recommended):
            raise ValueError("Input amount below fee-effective swap minimum")
    # An indicative quote may expire before MiniNex's 30-second UI preview.
    # Honor the provider's expiry if supplied; never accept a stale or
    # implausibly distant timestamp. No address/memo is requested here.
    raw_expiry = parsed.get("expiry")
    provider_expiry = None
    if raw_expiry is not None:
        if (type(raw_expiry) not in (int, str)
                or not re.fullmatch(r"[1-9][0-9]{9}", str(raw_expiry))):
            raise ValueError("Invalid provider quote expiry")
        provider_expiry = int(raw_expiry)
        now = int(time.time())
        if not now < provider_expiry <= now + 3600:
            raise ValueError("Expired or unbounded provider quote")
    # Fee fields are provider-specific. Omission is explicitly shown as
    # unverified, never silently treated as zero.
    return {
        "provider_expiry": provider_expiry,
        "estimated_output_units_1e8": output,
        "slippage_bps": slippage,
        "fee_breakdown_verified": False,
        "minimum_received_guaranteed": False,
    }


async def quote_native_btc(did: str, *, side: str, stable: str, amount: str) -> dict:
    if side not in ("BUY", "SELL") or stable not in ("USDC", "USDT"):
        raise HTTPException(422, "Unsupported BTC settlement")
    input_asset = stable if side == "BUY" else "BTC"
    output_asset = "BTC" if side == "BUY" else stable
    requested, units = _amount_to_mcp_units(
        amount, 6 if side == "BUY" else 8)
    user = await fetch_privy_user(did)
    try:
        wallets = verified_addresses(user, did)
    except ValueError:
        raise HTTPException(503, "Cannot verify unique BTC and ETH wallets") from None
    if not wallets.get("BTC") or not wallets.get("ETH"):
        raise HTTPException(409, "Native BTC and Ethereum settlement wallets required")
    snapshot = await get_wallet_balances(did)
    observed = snapshot.get("wallets", {})
    if not isinstance(observed, dict) or any(
        observed.get(c) != wallets.get(c) for c in ("BTC", "ETH")
    ):
        raise HTTPException(503, "BTC or ETH wallet changed during verification")
    balances=snapshot.get("balances")
    if not isinstance(balances, dict):
        raise HTTPException(503, "Unverified BTC/ETH balances")
    for asset in (input_asset, "BTC", "ETH"):
        row=balances.get(asset)
        if not isinstance(row,dict) or row.get("status")!="verified":
            raise HTTPException(503, "Unverified on-chain account balance")
    try:
        available=_decimal(balances[input_asset]["balance"])
        btc=_decimal(balances["BTC"]["balance"])
        eth=_decimal(balances["ETH"]["balance"])
    except (ValueError,TypeError,KeyError):
        raise HTTPException(503, "Malformed on-chain balance") from None
    if requested > available:
        raise HTTPException(409, "Insufficient verified input asset")
    if side=="SELL" and btc-requested<=BTC_GAS_RESERVE:
        raise HTTPException(409, "Insufficient Bitcoin fee reserve")
    if side=="BUY" and eth<=ETH_GAS_RESERVE:
        raise HTTPException(409, "Insufficient Ethereum fee reserve")
    from_asset=("BTC.BTC" if side=="SELL"
                else "ETH."+stable+"-"+STABLES[stable].upper())
    to_asset=("BTC.BTC" if side=="BUY"
              else "ETH."+stable+"-"+STABLES[stable].upper())
    body={
        "jsonrpc":"2.0","id":1,"method":"tools/call",
        "params":{"name":"get_swap_quote","arguments":{
            "from_asset":from_asset,
            "to_asset":to_asset,
            "amount":units,
            # NO destination. Prevent creation of a usable deposit address.
        }},
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9.0),follow_redirects=False) as client:
            response=await client.post(BTC_QUOTE_URL,json=body,
                headers={"Content-Type":"application/json",
                         "Accept":"application/json"})
            response.raise_for_status()
            if len(response.content)>256_000:
                raise ValueError("Oversized cross-chain quote")
            parsed=_provider_quote(response.json(),from_asset=from_asset,
                                   to_asset=to_asset,input_units=units)
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"Verified native BTC quote unavailable") from None
    return {
        "mode":"BTC_CROSSCHAIN_INDICATIVE_ONLY",
        "asset":"BTC",
        "side":side,
        "source_asset":input_asset,
        "destination_asset":output_asset,
        "destination_network":"bitcoin-mainnet" if side=="BUY" else "ethereum-mainnet",
        "input_amount":str(requested),
        "estimated_output_amount":str(
            Decimal(parsed["estimated_output_units_1e8"])/Decimal(10**8)),
        "quoted_price_impact_bps":parsed["slippage_bps"],
        "minimum_received_guaranteed":False,
        "fee_breakdown_verified":False,
        "route_provider":"THORCHAIN_PUBLIC_INDICATIVE",
        "automated_signing_enabled":False,
        "execution_enabled":False,
        "transaction_built":False,
        "deposit_address_provided":False,
        "can_submit":False,
        "quote_checked_at":int(time.time()),
        "warning":"Indicative public cross-chain quote only; no executable memo,"
                  " no minimum settlement guarantee, and no funds transferred.",
    }
