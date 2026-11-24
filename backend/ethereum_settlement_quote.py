"""Exact Ethereum-settled EVM cross-chain quote, not a transaction authorizer."""
from __future__ import annotations

import asyncio
from decimal import Decimal, localcontext
import os
import time

import httpx
from fastapi import HTTPException

from .ethereum_settlement_journal import store_quote
from .ethereum_settlement_calldata import inspect_across_v4
from .live_eth_quotes import ETH_NATIVE, STABLES, _quantity, _valid_units
from .multicoin_evm_balances import _read_chain, _WRAPPED_GROUPS
from .manual_swap_routes import _asset_network
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses, _eth_rpc_call

CROSS_EVM = {
    "BNB": (56, ETH_NATIVE),
    "POL": (137, ETH_NATIVE),
    "AVAX": (43114, ETH_NATIVE),
    "ARB": (42161, "0x912CE59144191C1204E64559FE8253a0e49E6548"),
}
QUOTE_URL = "https://li.quest/v1/quote"


def is_ethereum_cross_evm_pair(source: str, target: str) -> bool:
    return ((source in CROSS_EVM and target in STABLES)
            or (target in CROSS_EVM and source in STABLES))


def _source_token(asset: str, amount: Decimal, balances: dict) -> str:
    """Prefer existing wrapped inventory; never wrap the same coins twice."""
    native = balances.get(asset, {})
    if asset in _WRAPPED_GROUPS:
        symbol, contract, _ = _WRAPPED_GROUPS[asset]
        wrapped = balances.get(symbol, {})
        if (wrapped.get("status") == "verified"
                and Decimal(wrapped["balance"]) >= amount):
            return contract
    if (native.get("status") == "verified"
            and Decimal(native["balance"]) >= amount):
        return CROSS_EVM[asset][1]
    raise HTTPException(409, "Verified source balance insufficient")


def _token_matches(token, chain_id, address, decimals):
    return (isinstance(token, dict)
            and token.get("chainId") == chain_id
            and token.get("decimals") == decimals
            and isinstance(token.get("address"), str)
            and token["address"].lower() == address.lower())


def _read_quote(payload, *, wallet, source_chain, dest_chain, token_in,
                token_out, source_decimals, dest_decimals, units):
    """Verify the exact owner, source asset/amount and Ethereum destination.

    Never expose or execute opaque provider calldata from a price quote.
    """
    action, estimate = payload["action"], payload["estimate"]
    if (action.get("fromChainId") != source_chain
            or action.get("toChainId") != dest_chain
            or str(action.get("fromAddress", "")).lower() != wallet.lower()
            or str(action.get("toAddress", "")).lower() != wallet.lower()
            or action.get("fromAmount") != units
            or not _token_matches(action.get("fromToken"), source_chain,
                                  token_in, source_decimals)
            or not _token_matches(action.get("toToken"), dest_chain,
                                  token_out, dest_decimals)
            or estimate.get("fromAmount") != units):
        raise ValueError("Cross-chain quote does not match owner or settlement")
    output = _valid_units(estimate["toAmount"])
    minimum = _valid_units(estimate["toAmountMin"])
    if minimum > output:
        raise ValueError("Invalid minimum cross-chain output")
    return (format(Decimal(output).scaleb(-dest_decimals), "f"),
            format(Decimal(minimum).scaleb(-dest_decimals), "f"))


async def get_ethereum_cross_evm_quote(did: str, source: str, target: str,
                                      amount: str) -> dict:
    if not is_ethereum_cross_evm_pair(source, target):
        raise HTTPException(422, "Unsupported Ethereum settlement pair")
    buy = source in STABLES
    asset, stable = (target, source) if buy else (source, target)
    chain_id, coin_token = CROSS_EVM[asset]
    source_decimals, dest_decimals = (6, 18) if buy else (18, 6)
    with localcontext() as context:
        context.prec=80
        exact_amount, units = _quantity(amount, source_decimals)
    try:
        user = await fetch_privy_user(did)
        wallet = verified_addresses(user, did)["ETH"]
        if not wallet:
            raise ValueError("Original Privy EVM wallet missing")
        headers = {"Accept": "application/json"}
        key = os.getenv("MININEX_LIFI_API_KEY", "").strip()
        if key:
            headers["x-lifi-api-key"] = key
        async with httpx.AsyncClient(timeout=httpx.Timeout(9),
                                     follow_redirects=False) as client:
            if buy:
                # Only the canonical Ethereum token may fund a purchase.
                if await _eth_rpc_call(client,"eth_chainId",[],1)!=1:
                    raise ValueError("Wrong Ethereum source chain")
                calldata="0x70a08231"+wallet[2:].lower().rjust(64,"0")
                balance=await _eth_rpc_call(client,"eth_call",[
                    {"to":STABLES[stable],"data":calldata},"latest"],2)
                if balance<int(units):
                    raise HTTPException(409,"Verified Ethereum settlement balance insufficient")
            else:
                # Read the existing source-chain balances, not all networks.
                balances = await _read_chain(
                    client, "ARB_TOKENS" if asset == "ARB" else asset, wallet)
                coin_token = _source_token(asset, exact_amount, balances)
            source_chain, dest_chain = (1, chain_id) if buy else (chain_id, 1)
            token_in, token_out = ((STABLES[stable], coin_token) if buy
                                   else (coin_token, STABLES[stable]))
            response = await client.get(QUOTE_URL, headers=headers, params={
                "fromChain": source_chain, "toChain": dest_chain,
                "fromToken": token_in, "toToken": token_out,
                "fromAddress": wallet, "toAddress": wallet,
                "fromAmount": units, "slippage": "0.005",
            })
            response.raise_for_status()
            if len(response.content) > 512_000:
                raise ValueError("Oversized cross-chain quote")
            payload=response.json()
            expected, minimum = _read_quote(
                payload, wallet=wallet, source_chain=source_chain,
                dest_chain=dest_chain, token_in=token_in, token_out=token_out,
                source_decimals=source_decimals, dest_decimals=dest_decimals,
                units=units)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, ArithmeticError):
        raise HTTPException(503, "Ethereum settlement route unavailable") from None
    now = int(time.time())
    with localcontext() as context:
        context.prec=80
        _, minimum_atomic = _quantity(minimum, dest_decimals)
    terms = {
        "wallet_address": wallet.lower(), "asset": asset,
        "side": "BUY" if buy else "SELL", "stable": stable,
        "source_chain_id": source_chain, "destination_chain_id": dest_chain,
        "source_token": token_in.lower(), "destination_token": token_out.lower(),
        "input_atomic": units, "minimum_output_atomic": minimum_atomic,
        "issued_at": now, "expires_at": now + 30,
    }
    # Only a supported, decoded route may receive a durable calldata digest.
    # Unsupported routes still show the real quote, never a signable request.
    inspected=None
    try:
        inspected=inspect_across_v4(payload["transactionRequest"],terms=terms,now=now)
    except Exception:
        # ABI decode exceptions and unsupported provider route shapes are
        # intentionally indicative-only; no transaction authority is returned.
        pass
    terms["route_sha256"]=inspected["route_sha256"] if inspected else None
    quote_id = await asyncio.to_thread(store_quote, did, terms)
    return {
        "quote_id": quote_id,
        "restricted_calldata_verified": inspected is not None,
        "mode": "ETHEREUM_SETTLEMENT_CROSS_EVM_QUOTE",
        "source_asset": source, "destination_asset": target,
        "source_network": _asset_network(source)["network"],
        "destination_network": _asset_network(target)["network"],
        "source_chain_id": source_chain, "destination_chain_id": dest_chain,
        "input_amount": amount, "estimated_output_amount": expected,
        "minimum_output_amount": minimum, "quote_provider": "LIFI",
        "quote_checked_at": now, "quote_expires_at": now + 30,
        "pool_route_quoted": True, "execution_enabled": False,
        "can_submit": False, "transaction_built": False,
        "signing_authorized": False, "deposit_address_provided": False,
        "fee_breakdown_verified": False, "minimum_received_guaranteed": False,
    }
