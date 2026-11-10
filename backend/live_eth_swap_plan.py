"""Read-only ETH swap transaction assembly through the real Uniswap Trading API.

This is the next step beyond price quoting: check stablecoin approvals, request
a fresh Classic AMM quote, then ask Uniswap to simulate and build an unsigned
Ethereum swap transaction. Validate the resulting outer transaction.

SECURITY: The universal-router's nested commands are not yet decoded or
verified against the quoted tokens/minOut; therefore NO returned calldata,
approval instruction, wallet signature, or broadcast is made available to the
client. The real order executor MUST independently decode and approve every
nested command, reserve funds atomically, verify Privy grants and simulate via
a trusted Ethereum RPC immediately before signing. Nothing in here sends funds.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import os
import re
import time

import httpx
from fastapi import HTTPException

from .live_eth_quotes import (
    _quantity, _read_quote, ETH_NATIVE, STABLES, ETH_GAS_RESERVE,
    SLIPPAGE_PCT, UNISWAP_QUOTE_URL,
)
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import get_wallet_balances, verified_addresses

_API_URL = "https://trade-api.gateway.uniswap.org/v1"
_APPROVAL_URL = _API_URL + "/check_approval"
_SWAP_URL = _API_URL + "/swap"
# Verified Uniswap Universal Router V2.1.2, Ethereum mainnet.
# https://github.com/Uniswap/universal-router/blob/main/deploy-addresses/mainnet.json
_ROUTER_VERSION = "2.1.2"
_ROUTER = "0x23617e59A5925b2A4Bf75d73ff6711cD0b29De85"
_EXECUTE_WITH_DEADLINE = "3593564c"  # execute(bytes,bytes[],uint256)
_ROUTER_SELECTOR = "0x" + _EXECUTE_WITH_DEADLINE
_HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CALL_DATA = re.compile(r"^0x[0-9a-fA-F]{8,131072}$")
_INTEGER = re.compile(r"^(?:0|[1-9][0-9]{0,76})$")
_MAX_GAS_LIMIT = 2_000_000
_MAX_FEE_PER_GAS_WEI = 300_000_000_000
_MAX_GAS_WEI = 4_000_000_000_000_000
_TX_DEADLINE_SECONDS = 120


def _unsigned(raw: object, *, allow_zero: bool = False) -> int:
    if (type(raw) is not str or not _INTEGER.fullmatch(raw)):
        raise ValueError("Invalid unsigned gas or transaction amount")
    value = int(raw)
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError("Invalid zero transaction amount")
    return value


def _nonnegative_eth(raw: object) -> Decimal:
    if not isinstance(raw, str):
        raise ValueError("Invalid mainnet balance")
    try:
        qty = Decimal(raw)
    except InvalidOperation:
        raise ValueError("Invalid mainnet balance") from None
    if not qty.is_finite() or qty < 0:
        raise ValueError("Invalid mainnet balance")
    return qty


def inspect_uniswap_swap_tx(tx: object, *, wallet: str, side: str,
                            input_atomic: str, eth_balance: str) -> dict:
    """Validate outer transaction; nested Universal Router calls stay untrusted."""
    if not isinstance(tx, dict) or not _HEX_ADDRESS.fullmatch(wallet):
        raise ValueError("Invalid swap transaction")
    from_address = tx.get("from")
    to_address = tx.get("to")
    if not isinstance(from_address, str) or from_address.lower() != wallet.lower():
        raise ValueError("Wrong swap transaction owner")
    if not isinstance(to_address, str) or to_address.lower() != _ROUTER.lower():
        raise ValueError("Unrecognized swap execution contract")
    if type(tx.get("chainId")) is not int or tx["chainId"] != 1:
        raise ValueError("Swap must execute only on Ethereum mainnet")
    data = tx.get("data")
    if (not isinstance(data, str) or len(data) % 2
            or not _CALL_DATA.fullmatch(data) or not data.lower().startswith(_ROUTER_SELECTOR)):
        raise ValueError("Unexpected or unbounded router calldata")
    if len(data) < 10 + 64 * 3:
        raise ValueError("Truncated universal router function")
    try:
        value = _unsigned(tx.get("value"), allow_zero=True)
        gas_limit = _unsigned(tx.get("gasLimit"))
        gas_max_fee = _unsigned(tx.get("maxFeePerGas"))
        priority_fee = _unsigned(tx.get("maxPriorityFeePerGas"), allow_zero=True)
        eth_available = _nonnegative_eth(eth_balance)
        spend = _unsigned(input_atomic)
    except (ValueError, OverflowError):
        raise ValueError("Unverifiable swap amount or gas") from None
    if gas_limit > _MAX_GAS_LIMIT:
        raise ValueError("Excessive gas limit")
    if gas_max_fee > _MAX_FEE_PER_GAS_WEI or priority_fee > gas_max_fee:
        raise ValueError("Excessive priority or max gas fee")
    worst_gas_wei = gas_limit * gas_max_fee
    if worst_gas_wei > _MAX_GAS_WEI:
        raise ValueError("Excessive worst-case gas spending")
    if side == "BUY":
        if value != 0:
            raise ValueError("USDC/USDT purchase cannot forward native ETH")
    elif side == "SELL":
        if value != spend:
            raise ValueError("Native ETH sale must use exact quoted ETH input")
    else:
        raise ValueError("Invalid swap side")
    # A fixed additional ETH balance cushion remains after max gas and spend.
    wallet_liability = Decimal(value + worst_gas_wei) / Decimal(10**18)
    if eth_available - wallet_liability <= ETH_GAS_RESERVE:
        raise ValueError("Insufficient ETH to cover swap value, maximum gas and reserve")
    return {
        "router": _ROUTER,
        "router_version": _ROUTER_VERSION,
        "max_gas_limit": gas_limit,
        "max_fee_per_gas_gwei": str(Decimal(gas_max_fee) / Decimal(10**9)),
        "max_possible_gas_eth": str(Decimal(worst_gas_wei) / Decimal(10**18)),
        "tx_outer_fields_verified": True,
        "router_commands_decoded": False,
        "calldata_security_review_complete": False,
        "safe_to_sign": False,
    }


async def _provider_post(client: httpx.AsyncClient, path: str, body: dict,
                         api_key: str) -> dict:
    resp = await client.post(
        _API_URL + path,
        headers={
            "x-api-key": api_key,
            "x-universal-router-version": _ROUTER_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
    )
    resp.raise_for_status()
    if len(resp.content) > 256_000:
        raise ValueError("Oversized Uniswap provider response")
    result = resp.json()
    if not isinstance(result, dict):
        raise ValueError("Unrecognized Uniswap provider response")
    return result


async def make_eth_swap_plan(did: str, *, side: str, stable: str, amount: str) -> dict:
    """Get actual quote and simulated unsigned tx WITHOUT any signer access."""
    if side not in ("BUY", "SELL") or stable not in STABLES:
        raise HTTPException(422, "Unsupported ETH swap")
    _, input_atomic = _quantity(amount, 18 if side == "SELL" else 6)
    key = os.environ.get("MININEX_UNISWAP_API_KEY", "").strip()
    if not key:
        raise HTTPException(503, "Uniswap Trading API not configured")
    user = await fetch_privy_user(did)
    try:
        wallet = verified_addresses(user, did)["ETH"]
    except ValueError:
        raise HTTPException(503, "Unique Privy ETH wallet required") from None
    if not isinstance(wallet, str) or not _HEX_ADDRESS.fullmatch(wallet):
        raise HTTPException(409, "Verified Ethereum mainnet wallet missing")
    balance_response = await get_wallet_balances(did)
    record = balance_response.get("wallets", {})
    if not isinstance(record, dict) or str(record.get("ETH", "")).lower() != wallet.lower():
        raise HTTPException(503, "Balance and Privy wallet identity do not match")
    balances = balance_response.get("balances")
    token_in = stable if side == "BUY" else "ETH"
    if not isinstance(balances, dict):
        raise HTTPException(503, "Verified mainnet balances missing")
    for asset in (token_in, "ETH"):
        if (not isinstance(balances.get(asset), dict)
                or balances[asset].get("status") != "verified"):
            raise HTTPException(503, "Verified mainnet balance unavailable")
    try:
        available = _nonnegative_eth(balances[token_in]["balance"])
        eth_balance = _nonnegative_eth(balances["ETH"]["balance"])
    except (KeyError, ValueError):
        raise HTTPException(503, "Malformed verified mainnet balance") from None
    spending_decimals = 18 if side == "SELL" else 6
    input_amount = Decimal(input_atomic) / Decimal(10**spending_decimals)
    if available < input_amount:
        raise HTTPException(409, "Insufficient verified input funds")
    if eth_balance - (input_amount if side == "SELL" else Decimal(0)) <= ETH_GAS_RESERVE:
        raise HTTPException(409, "Insufficient Ethereum gas reserve")

    addr_in = STABLES[stable] if side == "BUY" else ETH_NATIVE
    addr_out = ETH_NATIVE if side == "BUY" else STABLES[stable]
    quote_request = {
        "type": "EXACT_INPUT", "amount": input_atomic,
        "tokenInChainId": 1, "tokenOutChainId": 1,
        "tokenIn": addr_in, "tokenOut": addr_out,
        "swapper": wallet, "recipient": wallet,
        "protocols": ["V3"],
        "routingPreference": "BEST_PRICE",
        "slippageTolerance": SLIPPAGE_PCT,
        "permitAmount": "EXACT",
    }
    approval_required = False
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0), follow_redirects=False) as client:
            if side == "BUY":
                approval = await _provider_post(client, "/check_approval", {
                    "walletAddress": wallet,
                    "token": addr_in, "tokenOut": addr_out,
                    "amount": input_atomic,
                    "chainId": 1, "tokenOutChainId": 1,
                }, key)
                # Never trust an approval transaction to transfer money.
                approval_required = (approval.get("approval") is not None
                                     or approval.get("cancel") is not None)
            quote = await _provider_post(client, "/quote", quote_request, key)
            if (quote.get("routing") != "CLASSIC" or quote.get("txFailureReasons")
                    or quote.get("txFailureReason")):
                raise ValueError("Unsupported or unsuccessful swap route")
            expected, minimum = _read_quote(
                quote, token_in=addr_in, token_out=addr_out,
                amount_units=input_atomic, wallet=wallet,
            )
            if approval_required or quote.get("permitData") is not None:
                return {
                    "mode": "LIVE_ETH_SWAP_PLAN_ONLY",
                    "asset": "ETH", "chain_id": 1,
                    "side": side, "input_asset": token_in,
                    "output_asset": "ETH" if side == "BUY" else stable,
                    "input_amount": amount,
                    "expected_output_amount": str(
                        Decimal(expected) / Decimal(10**(18 if side == "BUY" else 6))),
                    "minimum_output_amount": str(
                        Decimal(minimum) / Decimal(10**(18 if side == "BUY" else 6))),
                    "state": "APPROVAL_OR_PERMIT_REQUIRED",
                    "erc20_approval_required": approval_required,
                    "permit_signature_required": quote.get("permitData") is not None,
                    "transaction_built": False,
                    "execution_enabled": False,
                    "automatic_signing_enabled": False,
                    "can_submit": False,
                    "warning": "An additional on-chain approval or typed-data permit may be required.",
                }
            swap = await _provider_post(client, "/swap", {
                "quote": quote["quote"],
                "simulateTransaction": True,
                "refreshGasPrice": True,
                "safetyMode": "SAFE",
                "deadline": int(time.time()) + _TX_DEADLINE_SECONDS,
                "swapSource": "WALLET",
            }, key)
            safe = inspect_uniswap_swap_tx(
                swap.get("swap"), wallet=wallet, side=side,
                input_atomic=input_atomic, eth_balance=str(eth_balance))
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError):
        raise HTTPException(502, "Safe Uniswap ETH swap transaction could not be verified") from None

    return {
        "mode": "LIVE_ETH_SWAP_PLAN_ONLY",
        "asset": "ETH",
        "chain_id": 1,
        "side": side,
        "input_asset": token_in,
        "output_asset": "ETH" if side == "BUY" else stable,
        "input_amount": amount,
        "expected_output_amount": str(
            Decimal(expected) / Decimal(10**(18 if side == "BUY" else 6))),
        "minimum_output_amount": str(
            Decimal(minimum) / Decimal(10**(18 if side == "BUY" else 6))),
        "state": "UNSIGNED_TX_REVIEW_REQUIRED",
        "erc20_approval_required": False,
        "permit_signature_required": False,
        "transaction_built": True,
        "execution_enabled": False,
        "automatic_signing_enabled": False,
        "can_submit": False,
        **safe,
        "warning": "Uniswap simulated unsigned transaction. Nested router calldata, wallet authority and nonce still need independent verification. No funds moved.",
    }
