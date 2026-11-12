"""Network-accurate manual swap route discovery for all twelve MiniNex coins.

A route *classification* is not an executable route. Actual wallet ownership,
token contracts, a licensed venue, quotes, signatures, fees and chain receipts
must all be verified separately before any execution is available.
This module is deterministic: no RPC, Privy call, provider, polling or storage.
"""
from __future__ import annotations

from fastapi import HTTPException
from .live_asset_registry import ASSET_ORDER, BY_SYMBOL

STABLE_ASSETS = ("USDC", "USDT")
ALL_SWAP_ASSETS = ASSET_ORDER + STABLE_ASSETS
_ETHEREUM = "ethereum-mainnet"
_NO_EXECUTION = (
    "verified_source_and_destination_wallets",
    "independently_verified_provider_and_contracts",
    "exact_spend_allowance_and_fees",
    "restricted_user_wallet_signing",
    "onchain_receipt_and_refund_reconciliation",
)


def _asset_network(symbol: str) -> dict:
    if symbol in STABLE_ASSETS:
        # Wallet's USDC/USDT are Ethereum ERC-20 tokens. Never imply these
        # balances are the same asset on XRPL, TRON, BSC or other networks.
        return {"symbol": symbol, "network": _ETHEREUM,
                "chain_id": 1, "kind": "ethereum-erc20"}
    coin = BY_SYMBOL[symbol]
    return {"symbol": coin.symbol, "network": coin.blockchain,
            "chain_id": coin.chain_id, "kind": coin.execution_family}


def build_manual_swap_route(source: str, target: str) -> dict:
    """Exact allowed assets and network-aware path classification, never trade."""
    if type(source) is not str or type(target) is not str:
        raise HTTPException(422, "Invalid manual swap assets")
    if source not in ALL_SWAP_ASSETS or target not in ALL_SWAP_ASSETS:
        raise HTTPException(422, "Unsupported manual swap assets")
    if source == target:
        raise HTTPException(422, "Cannot swap an asset to itself")

    incoming, outgoing = _asset_network(source), _asset_network(target)
    same_network = incoming["network"] == outgoing["network"]
    eth_stable_pair = (
        {source, target}.intersection(STABLE_ASSETS)
        and {source, target}.intersection({"ETH"})
        and len({source, target}) == 2
    )
    btc_stable_pair = (
        {source, target}.intersection(STABLE_ASSETS)
        and {source, target}.intersection({"BTC"})
        and len({source, target}) == 2
    )
    # Every pair can request a real live market quote through the public
    # provider. A quote exists ONLY when actual liquidity is available;
    # in particular ADA and UNI may have no active THORChain pool.
    # This NEVER means an executable route or wallet signer is present.
    quote_asset = "ETH" if eth_stable_pair else (
        "BTC" if btc_stable_pair else "MULTICHAIN")
    quote_endpoint = ("/api/live/eth/quote" if quote_asset == "ETH" else
                      "/api/live/btc/quote" if quote_asset == "BTC" else
                      "/api/wallet/swap/live-quote")
    # Real quote and fee provider status are NOT inferred from configuration
    # or a user-selected token ticker.
    required = list(_NO_EXECUTION)
    if not same_network:
        required.append("cross_chain_finality_and_refund_path")
    if incoming["kind"] in {"xrpl", "tron", "cardano-eutxo"} or outgoing["kind"] in {
            "xrpl", "tron", "cardano-eutxo"}:
        required.append("native_wallet_signer_and_token_issuer_validation")
    return {
        "mode": "MANUAL_SWAP_ROUTE_DISCOVERY_ONLY",
        "source": incoming,
        "destination": outgoing,
        "route_class": "SAME_CHAIN" if same_network else "CROSS_CHAIN",
        "quote_asset": quote_asset,
        "quote_endpoint": quote_endpoint,
        "quote_preview_implemented": True,
        "active_liquidity_not_guaranteed": True,
        "execution_ready": False,
        "signing_authorized": False,
        "transaction_built": False,
        "can_submit": False,
        "network_fee_verified": False,
        "guaranteed_minimum_received": False,
        "required_checks": required,
    }
