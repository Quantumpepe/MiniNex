"""Common twelve-coin trade routing for *real* provider-backed swap prices.

One API contract dispatches a proposed BUY/SELL to existing network adapters:
Ethereum uses the same user-owned verified Uniswap swap planner, Bitcoin uses
the verified native wallet cross-chain quote, and the nine additional assets
share a live venue-price adapter. The latter have no proven owner/signer yet.

This is deliberately a route planner, NOT a fake order, paper fill, wallet
permission, auto-trader, or substitute for actual signed execution. The same
entry point will feed per-chain executors after real signing is implemented.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from fastapi import HTTPException

from .live_asset_registry import get_asset
from .shared_evm_execution import chain_for_asset
from .live_eth_swap_plan import make_eth_swap_plan
from .eth_erc20_trade_quote import quote_owner_erc20_trade
from .native_evm_v3_quote import quote_native_evm_trade
from .live_btc_quotes import quote_native_btc
from .manual_swap_live_quotes import get_native_swap_quote
from .native_trade_owner_quote import quote_owner_native_trade
from .storage import _safe_did
from .strategy_replay import REPLAY_ASSETS


# All twelve coins can obtain a read-only quote, but real execution
# requires a separately owner-granted policy and verified on-chain route.
TRADE_COINS = tuple(REPLAY_ASSETS)
_MONEY = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_DRY_RUN_ONLY = {
    "real_order_created": False,
    "wallet_signed": False,
    "funds_transferred": False,
    "can_submit": False,
    "execution_enabled": False,
}


def trade_plan_request(asset: object, side: object, stable: object,
                       amount: object) -> dict:
    """One shared action contract; exact input amount in the SOURCE asset.

    Coin-specific network and settlement are derived from the server registry,
    not from caller-provided addresses, token contracts, or chain choices.
    """
    if type(asset) is not str or asset not in TRADE_COINS:
        raise HTTPException(404, "Coin is not available for trading")
    info = get_asset(asset)
    if side not in ("BUY", "SELL") or type(side) is not str:
        raise HTTPException(422, "Invalid trading side")
    if type(stable) is not str or stable not in info.stablecoin_candidates:
        raise HTTPException(422, "Settlement asset is unavailable on this coin")
    if type(amount) is not str or len(amount)>45 or not _MONEY.fullmatch(amount):
        raise HTTPException(422, "Invalid exact input amount")
    try:
        value=Decimal(amount)
    except InvalidOperation:
        raise HTTPException(422, "Invalid exact input amount") from None
    if not value.is_finite() or value <= 0:
        raise HTTPException(422, "Input amount must be positive")
    evm_chain_id = None
    if info.execution_family in ("evm", "evm-erc20"):
        evm_chain_id, network = chain_for_asset(asset)
        if network != info.blockchain:
            raise HTTPException(503, "EVM asset routing mismatch")
    return {
        "asset": asset, "side": side, "stable": stable,
        "chain_id": evm_chain_id,
        "source_asset": stable if side=="BUY" else asset,
        "destination_asset": asset if side=="BUY" else stable,
        "input_amount": amount, "network": info.blockchain,
        "execution_family": info.execution_family,
    }


async def get_unified_trade_plan(did: str, *, asset: str, side: str,
                                 stable: str, amount: str) -> dict:
    """Use ONE entry point for twelve coins instead of eleven UI/backend flows.

    ETH/BTC provider adapters check the user's actual Privy wallets and funds.
    The other nine adapters provide ONLY a real venue market quote, not wallet
    spendability or cross-chain execution. These are different quote levels
    that must be explicit and cannot be conflated with an executable order.
    """
    did = _safe_did(did)
    request = trade_plan_request(asset, side, stable, amount)

    if asset=="ETH":
        upstream=await make_eth_swap_plan(
            did, side=side, stable=stable, amount=amount)
        if (not isinstance(upstream,dict) or
            upstream.get("mode")!="LIVE_ETH_SWAP_PLAN_ONLY" or
            upstream.get("asset")!=asset or upstream.get("side")!=side or
            upstream.get("input_amount")!=amount or
            upstream.get("execution_enabled") is not False or
            upstream.get("can_submit") is not False):
            raise HTTPException(502,"Ethereum trade planner mismatch")
        result={
            "quote_level":"WALLET_VERIFIED_UNSIGNED_SWAP",
            "wallet_funds_verified":True,
            "quote_provider":"UNISWAP_ETHEREUM",
            "estimated_output_amount":upstream.get("expected_output_amount"),
            "minimum_output_amount":upstream.get("minimum_output_amount"),
            "approval_or_permit_required":upstream.get("state")=="APPROVAL_OR_PERMIT_REQUIRED",
            "transaction_preview_created":upstream.get("transaction_built") is True,
        }
    elif asset in ("LINK","UNI"):
        upstream=await quote_owner_erc20_trade(
            did,asset=asset,side=side,stable=stable,amount=amount)
        if (type(upstream) is not dict
                or upstream.get("mode")!="SHARED_EVM_ERC20_ROUTER02_QUOTE"
                or upstream.get("asset")!=asset
                or upstream.get("network")!="ethereum-mainnet"
                or upstream.get("side")!=side
                or upstream.get("stable")!=stable
                or upstream.get("input_amount")!=amount
                or upstream.get("wallet_identity_verified") is not True
                or upstream.get("wallet_funds_verified") is not True
                or upstream.get("execution_enabled") is not False
                or upstream.get("can_submit") is not False):
            raise HTTPException(502,"ERC-20 EVM trade quote mismatch")
        result={
            "quote_level":"WALLET_VERIFIED_UNSIGNED_ERC20_SWAP",
            "wallet_funds_verified":True,
            "quote_provider":"UNISWAP_ETHEREUM_ERC20",
            "estimated_output_amount":upstream.get("expected_output_amount"),
            "minimum_output_amount":upstream.get("minimum_output_amount"),
            "approval_or_permit_required":True,
            "transaction_preview_created":True,
        }
    elif asset in ("BNB","POL","AVAX","ARB"):
        upstream=await quote_native_evm_trade(
            did,asset=asset,side=side,stable=stable,amount=amount)
        if (type(upstream) is not dict
                or upstream.get("mode")!=("SHARED_EVM_ERC20_V3_QUOTE"
                    if asset=="ARB" else "SHARED_NATIVE_EVM_V3_QUOTE")
                or upstream.get("asset")!=asset
                or upstream.get("network")!=request["network"]
                or upstream.get("chain_id")!=request["chain_id"]
                or upstream.get("side")!=side
                or upstream.get("stable")!=stable
                or upstream.get("input_amount")!=amount
                or upstream.get("onchain_quote_verified") is not True
                or upstream.get("wallet_identity_verified") is not True
                or upstream.get("wallet_funds_verified") is not True
                or upstream.get("execution_enabled") is not False
                or upstream.get("can_submit") is not False):
            raise HTTPException(502,"Native EVM v3 route mismatch")
        result={
            "quote_level":("WALLET_VERIFIED_UNSIGNED_ERC20_SWAP"
                           if asset=="ARB" else "WALLET_VERIFIED_UNSIGNED_NATIVE_EVM_SWAP"),
            "wallet_funds_verified":True,
            "quote_provider":"UNISWAP_V3_CHAIN_SPECIFIC",
            "estimated_output_amount":upstream.get("expected_output_amount"),
            "minimum_output_amount":upstream.get("minimum_output_amount"),
            "approval_or_permit_required":True,
            "transaction_preview_created":True,
            "native_wrap_unwrap_not_yet_settled":asset!="ARB",
        }
    elif asset=="BTC":
        upstream=await quote_native_btc(
            did, side=side, stable=stable, amount=amount)
        if (not isinstance(upstream,dict) or
            upstream.get("mode")!="BTC_CROSSCHAIN_INDICATIVE_ONLY" or
            upstream.get("asset")!=asset or upstream.get("side")!=side or
            upstream.get("source_asset")!=request["source_asset"] or
            upstream.get("destination_asset")!=request["destination_asset"] or
            upstream.get("execution_enabled") is not False or
            upstream.get("can_submit") is not False):
            raise HTTPException(502,"Bitcoin trade quote mismatch")
        result={
            "quote_level":"WALLET_VERIFIED_CROSSCHAIN_INDICATIVE",
            "wallet_funds_verified":True,
            "quote_provider":"THORCHAIN_PUBLIC_MCP",
            "estimated_output_amount":upstream.get("estimated_output_amount"),
            "minimum_output_amount":None,
            "approval_or_permit_required":None,
            "transaction_preview_created":False,
        }
    elif asset in ("SOL","XRP"):
        upstream=await quote_owner_native_trade(
            did,asset=asset,side=side,stable=stable,amount=amount)
        if (type(upstream) is not dict
                or upstream.get("mode")!="OWNER_VERIFIED_NATIVE_MULTICHAIN_QUOTE_ONLY"
                or upstream.get("asset")!=asset
                or upstream.get("side")!=side
                or upstream.get("stable")!=stable
                or upstream.get("source_asset")!=request["source_asset"]
                or upstream.get("destination_asset")!=request["destination_asset"]
                or upstream.get("input_amount")!=amount
                or upstream.get("wallet_identity_verified") is not True
                or upstream.get("native_wallet_balance_verified") is not True
                or upstream.get("ethereum_settlement_wallet_verified") is not True
                or upstream.get("source_chain_balance_verified") is not True
                or upstream.get("network_fee_and_xrpl_reserve_spendability_verified") is not False
                or upstream.get("execution_enabled") is not False
                or upstream.get("can_submit") is not False):
            raise HTTPException(502,"Owner-bound native route mismatch")
        result={
            "quote_level":"OWNER_WALLET_BALANCE_VERIFIED_NATIVE_INDICATIVE",
            "wallet_funds_verified":True,
            "source_chain_balance_verified":True,
            "network_fee_spendability_verified":False,
            "destination_settlement_finalized":False,
            "quote_provider":"THORCHAIN_PUBLIC_MCP",
            "estimated_output_amount":upstream.get("estimated_output_amount"),
            "minimum_output_amount":None,
            "approval_or_permit_required":None,
            "transaction_preview_created":False,
        }
    else:
        upstream=await get_native_swap_quote(
            request["source_asset"], request["destination_asset"], amount)
        if (not isinstance(upstream,dict) or
            upstream.get("mode")!="NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY" or
            upstream.get("source_asset")!=request["source_asset"] or
            upstream.get("destination_asset")!=request["destination_asset"] or
            upstream.get("input_amount")!=amount or
            upstream.get("execution_enabled") is not False or
            upstream.get("can_submit") is not False):
            raise HTTPException(502,"Multi-chain trade quote mismatch")
        result={
            "quote_level":"VENUE_INDICATIVE_WALLET_NOT_VERIFIED",
            "wallet_funds_verified":False,
            "quote_provider":"THORCHAIN_PUBLIC_MCP",
            "estimated_output_amount":upstream.get("estimated_output_amount"),
            "minimum_output_amount":None,
            "approval_or_permit_required":None,
            "transaction_preview_created":False,
        }

    output=result["estimated_output_amount"]
    if (type(output) is not str or not _MONEY.fullmatch(output)
            or Decimal(output) <= 0):
        raise HTTPException(502,"Invalid provider trade output")
    if result["minimum_output_amount"] is not None:
        minout=result["minimum_output_amount"]
        if type(minout) is not str or not _MONEY.fullmatch(minout):
            raise HTTPException(502,"Invalid minimum provider output")

    return {
        "mode":"UNIFIED_ELEVEN_COIN_TRADE_PLAN",
        **request, **result, **_DRY_RUN_ONLY,
        "route_plan_only":True,
        "note":"Real quote; no wallet signature, order submission, or exchange fill.",
    }
