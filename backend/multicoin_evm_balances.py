"""Real on-chain balance readings for MiniNex EVM coins and ERC-20 assets.

This is a practical wallet integration, not a PAPER valuation. BNB, POL and
AVAX use the verified user's existing Privy EVM address on their own networks;
LINK/UNI are Ethereum ERC-20 assets; ARB is an Arbitrum One ERC-20. Only fixed, chain-verified public
RPC reads occur. No wallet approval, private key or transaction is needed.
"""
from __future__ import annotations

import asyncio
import os
import re

import httpx
from fastapi import HTTPException

from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses, _rpc_quantity, _units, WEI
from .shared_evm_execution import chain_for_asset
from .live_asset_registry import get_asset
from .storage import _safe_did

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_RPC = {
    "BNB": (56, "bsc-mainnet", "https://bsc-dataseed.binance.org", "MININEX_BSC_RPC_URL"),
    "POL": (137, "polygon-pos-mainnet", "https://polygon-bor-rpc.publicnode.com", "MININEX_POLYGON_RPC_URL"),
    "AVAX": (43114, "avalanche-c-mainnet", "https://avalanche-c-chain-rpc.publicnode.com", "MININEX_AVALANCHE_RPC_URL"),
    "ETH_TOKENS": (1, "ethereum-mainnet", "https://ethereum-rpc.publicnode.com", "MININEX_ETH_RPC_URL"),
    "ARB_TOKENS": (42161, "arbitrum-one-mainnet", "https://arb1.arbitrum.io/rpc", "MININEX_ARBITRUM_RPC_URL"),
}
_ETH_TOKENS = {
    "LINK": "0x514910771AF9CA656AF840DFF83E8264ECF986CA",
    "QNT": "0x4a220e6096b25eadb88358cb44068a3248254675",
    "UNI": "0x1f9840a85d5af5bf1d1762f925bddadc4201f984",
}
_ARB_TOKENS = {"ARB": "0x912CE59144191C1204E64559FE8253a0e49E6548"}
_TOKEN_GROUPS = {"ETH_TOKENS": _ETH_TOKENS, "ARB_TOKENS": _ARB_TOKENS}
# Fixed, authentic wrapped-native contracts. These are *not* additional
# user-facing coins: they represent a user's actual existing assets during
# wrap -> router swap / swap -> unwrap, and must count towards their native
# asset's total wallet valuation. Read only, never sign or move assets.
_WRAPPED_GROUPS = {
    "BNB": ("WBNB", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "BNB"),
    "POL": ("WPOL", "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "POL"),
    "AVAX": ("WAVAX", "0xB31f66AA3C1e785363f0875a1b74e27b85FD66c7", "AVAX"),
    "ETH_TOKENS": ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "ETH"),
}
WRAPPED_ORDER = ("WBNB", "WPOL", "WAVAX", "WETH")
ORDER = ("BNB", "POL", "AVAX", "LINK", "QNT", "UNI", "ARB")
# Native EVM units and the allowlisted ERC-20 contracts use 18 decimals.
DECIMALS = {asset: 18 for asset in ORDER}


async def _rpc(client: httpx.AsyncClient, url: str, method: str, params: list, rid: int) -> int:
    response = await client.post(
        url, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    response.raise_for_status()
    if len(response.content) > 4096:
        raise ValueError("Unexpected mainnet RPC response size")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("id") != rid:
        raise ValueError("RPC response request mismatch")
    return _rpc_quantity(payload, fixed_word=method == "eth_call")


def _entry(asset: str, status: str, balance: str | None = None) -> dict:
    rpc_key = asset if asset in _RPC else ("ARB_TOKENS" if asset == "ARB" else "ETH_TOKENS")
    network = _RPC[rpc_key][1]
    return {
        "asset": asset, "network": network, "status": status,
        "balance": balance, "native": asset not in _ETH_TOKENS and asset not in _ARB_TOKENS,
        "chain_id": _RPC[rpc_key][0],
        "decimals": DECIMALS[asset],
        "wallet_funds_used_for_paper": False,
    }


def _wrapped_entry(name: str, status: str, balance: str | None = None) -> dict:
    wrapped, contract, underlying = _WRAPPED_GROUPS[name]
    chain_id, network, _, _ = _RPC[name]
    return {
        "asset": wrapped, "underlying": underlying, "network": network,
        "chain_id": chain_id, "contract": contract.lower(),
        "decimals": 18, "native": False,
        "status": status, "balance": balance,
        "wallet_funds_used_for_paper": False,
    }


async def _read_wrapped(client: httpx.AsyncClient, name: str,
                        url: str, wallet: str) -> dict:
    wrapped, contract, _ = _WRAPPED_GROUPS[name]
    selector = "0x70a08231" + ("0" * 24) + wallet[2:].lower()
    try:
        atomic = await _rpc(
            client, url, "eth_call", [{"to": contract, "data": selector},
                                     "latest"], 21)
        return {wrapped: _wrapped_entry(name, "verified", _units(atomic, WEI))}
    except (httpx.HTTPError, ValueError, TypeError, OverflowError):
        # An unreadable token balance is NEVER silently reported as zero.
        return {wrapped: _wrapped_entry(name, "unavailable")}


async def _read_chain(client: httpx.AsyncClient, name: str, wallet: str) -> dict:
    chain_id, _, fallback, env_name = _RPC[name]
    # The wallet reader and the real transaction encoder MUST agree on
    # chain IDs. Fail closed rather than showing a misleading spendable coin.
    assets = tuple(_TOKEN_GROUPS[name]) if name in _TOKEN_GROUPS else (name,)
    # ARB balance reading is deliberately independent of LIVE execution routing.
    if any((get_asset(asset).chain_id if asset == "ARB" else chain_for_asset(asset)[0]) != chain_id
           for asset in assets):
        raise ValueError("EVM balance and execution chain registry disagree")
    url = os.getenv(env_name, "").strip() or fallback
    # RPC URL is controlled exclusively by the operator, never by a user.
    if not url.startswith("https://") or any(c in url for c in " \t\n#@"):
        raise ValueError("HTTPS mainnet RPC configuration required")
    if await _rpc(client, url, "eth_chainId", [], 1) != chain_id:
        raise ValueError("Unexpected RPC chain ID")
    if name not in _TOKEN_GROUPS:
        wei = await _rpc(client, url, "eth_getBalance", [wallet, "latest"], 2)
        wrapped = await _read_wrapped(client, name, url, wallet)
        return {name: _entry(name, "verified", _units(wei, WEI)), **wrapped}

    selector = "0x70a08231" + ("0" * 24) + wallet[2:].lower()
    results = {}
    for i, (asset, contract) in enumerate(_TOKEN_GROUPS[name].items(), start=3):
        try:
            atomic = await _rpc(
                client, url, "eth_call",
                [{"to": contract, "data": selector}, "latest"], i,
            )
            results[asset] = _entry(asset, "verified", _units(atomic, WEI))
        except (httpx.HTTPError, ValueError, TypeError, OverflowError):
            results[asset] = _entry(asset, "unavailable")
    if name in _WRAPPED_GROUPS:
        results.update(await _read_wrapped(client, name, url, wallet))
    return results


async def get_multicoin_evm_balances(did: str) -> dict:
    """Independently read six REAL on-chain holdings belonging to this DID.

    Missing provider data remains unavailable, never a fabricated zero. The
    user must already have a unique linked Privy ETH wallet; EVM-compatible
    networks reuse that address without implying deposits are enabled.
    """
    did = _safe_did(did)
    user = await fetch_privy_user(did)
    try:
        address = verified_addresses(user, did)["ETH"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(503, "Unique user-owned Privy EVM wallet required") from None
    if not isinstance(address, str) or not _ADDR.fullmatch(address):
        raise HTTPException(503, "Verified EVM address missing")

    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0), follow_redirects=False) as client:
        replies = await asyncio.gather(
            *(_read_chain(client, chain, address) for chain in _RPC),
            return_exceptions=True,
        )
    assets = {coin: _entry(coin, "unavailable") for coin in ORDER}
    wrapped_assets = {
        info[0]: _wrapped_entry(chain, "unavailable")
        for chain, info in _WRAPPED_GROUPS.items()
    }
    for chain, response in zip(_RPC, replies):
        if isinstance(response, BaseException):
            continue
        if isinstance(response, dict):
            for asset, entry in response.items():
                if asset in assets and entry.get("status") == "verified":
                    assets[asset] = entry
                if asset in wrapped_assets and entry.get("status") == "verified":
                    wrapped_assets[asset] = entry

    return {
        "mode": "REAL_MAINNET_BALANCES_READ_ONLY",
        "wallet_ownership_verified": True,
        "asset_order": list(ORDER),
        "assets": assets,
        "wrapped_assets": wrapped_assets,
        "wallet_funds_used_for_paper": False,
        "real_trading_enabled": False,
        "execution_available": False,
    }
