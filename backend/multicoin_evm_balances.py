"""Real on-chain balance readings for MiniNex EVM coins and ERC-20 assets.

This is a practical wallet integration, not a PAPER valuation. BNB, POL and
AVAX use the verified user's existing Privy EVM address on their own networks;
LINK and UNI are Ethereum ERC-20 assets. Only fixed, chain-verified public
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
from .storage import _safe_did

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_RPC = {
    "BNB": (56, "bsc-mainnet", "https://bsc-dataseed.binance.org", "MININEX_BSC_RPC_URL"),
    "POL": (137, "polygon-pos-mainnet", "https://polygon-bor-rpc.publicnode.com", "MININEX_POLYGON_RPC_URL"),
    "AVAX": (43114, "avalanche-c-mainnet", "https://avalanche-c-chain-rpc.publicnode.com", "MININEX_AVALANCHE_RPC_URL"),
    "ETH_TOKENS": (1, "ethereum-mainnet", "https://ethereum-rpc.publicnode.com", "MININEX_ETH_RPC_URL"),
}
_ETH_TOKENS = {
    "LINK": "0x514910771AF9CA656AF840DFF83E8264ECF986CA",
    "UNI": "0x1f9840a85d5af5bf1d1762f925bddadc4201f984",
}
ORDER = ("BNB", "POL", "AVAX", "LINK", "UNI")
# Native EVM units and these two specific Ethereum contracts all use 18 decimals.
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
    network = _RPC[asset if asset in _RPC else "ETH_TOKENS"][1]
    return {
        "asset": asset, "network": network, "status": status,
        "balance": balance, "native": asset not in _ETH_TOKENS,
        "chain_id": _RPC[asset if asset in _RPC else "ETH_TOKENS"][0],
        "decimals": DECIMALS[asset],
        "wallet_funds_used_for_paper": False,
    }


async def _read_chain(client: httpx.AsyncClient, name: str, wallet: str) -> dict:
    chain_id, _, fallback, env_name = _RPC[name]
    url = os.getenv(env_name, "").strip() or fallback
    # RPC URL is controlled exclusively by the operator, never by a user.
    if not url.startswith("https://") or any(c in url for c in " \t\n#@"):
        raise ValueError("HTTPS mainnet RPC configuration required")
    if await _rpc(client, url, "eth_chainId", [], 1) != chain_id:
        raise ValueError("Unexpected RPC chain ID")
    if name != "ETH_TOKENS":
        wei = await _rpc(client, url, "eth_getBalance", [wallet, "latest"], 2)
        return {name: _entry(name, "verified", _units(wei, WEI))}

    selector = "0x70a08231" + ("0" * 24) + wallet[2:].lower()
    results = {}
    for i, (asset, contract) in enumerate(_ETH_TOKENS.items(), start=3):
        try:
            atomic = await _rpc(
                client, url, "eth_call",
                [{"to": contract, "data": selector}, "latest"], i,
            )
            results[asset] = _entry(asset, "verified", _units(atomic, WEI))
        except (httpx.HTTPError, ValueError, TypeError, OverflowError):
            results[asset] = _entry(asset, "unavailable")
    return results


async def get_multicoin_evm_balances(did: str) -> dict:
    """Independently read five REAL on-chain holdings belonging to this DID.

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
    for chain, response in zip(_RPC, replies):
        if isinstance(response, BaseException):
            continue
        if isinstance(response, dict):
            for asset, entry in response.items():
                if asset in assets and entry.get("status") == "verified":
                    assets[asset] = entry

    return {
        "mode": "REAL_MAINNET_BALANCES_READ_ONLY",
        "wallet_ownership_verified": True,
        "asset_order": list(ORDER),
        "assets": assets,
        "wallet_funds_used_for_paper": False,
        "real_trading_enabled": False,
        "execution_available": False,
    }
