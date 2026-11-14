"""Authenticated, deliberately non-executable ETH swap cost preview.

The existing /swap-plan, wallet, PAPER strategies, and Privy enrollment remain
unchanged. This separate user-initiated flow combines a reviewed unsigned plan
with server-only wallet, independent reference prices, and user ETH LIVE ledger.
No amount here is permission to create or submit an order.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import time

from fastapi import HTTPException

from .eth_cost_budget_review import review_eth_cost_budget
from .eth_privy_policy_plan import fetch_reference_prices, _parsed_price
from .eth_live_ledger_audit import get_eth_live_ledger_audit
from .live_eth_controls import get_live_eth_limits
from .live_eth_quotes import _quantity
from .live_eth_swap_plan import make_eth_swap_plan
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import get_wallet_balances, verified_addresses
from .storage import _safe_did

_BALANCE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_MAX_AGE = 30
_ALWAYS_BLOCKED = (
    "LIVE_EXECUTOR_DISABLED",
    "PRIVY_WALLET_SIGNING_NOT_AUTHORIZED_BY_PREVIEW",
    "NO_ATOMIC_ORDER_RESERVATION",
)


def _atomic_balance(value: object, decimals: int) -> int:
    """Exact verified unit count, no silent float/decimal rounding."""
    if type(value) is not str or len(value) > 64 or not _BALANCE.fullmatch(value):
        raise ValueError("Invalid verified onchain token balance")
    try:
        number = Decimal(value) * (Decimal(10) ** decimals)
    except (InvalidOperation, OverflowError):
        raise ValueError("Unverifiable token balance") from None
    if not number.is_finite() or number != number.to_integral_value() or number < 0 or number >= 10**45:
        raise ValueError("Unverifiable onchain token units")
    return int(number)


def _fresh_reference(snapshot: object, symbol: str, *, now: int) -> tuple[str, int]:
    if type(snapshot) is not dict:
        raise ValueError("Price snapshot missing")
    item = snapshot.get({"ETH": "ethereum", "USDC": "usd-coin", "USDT": "tether"}[symbol])
    if type(item) is not dict or type(item.get("last_updated_at")) is not int:
        raise ValueError("Reference timestamp missing")
    age = now - item["last_updated_at"]
    if not 0 <= age <= _MAX_AGE:
        raise ValueError("Independent reference price too old or in future")
    price = _parsed_price(snapshot, symbol, now)
    return format(price, "f"), age


def _checked_plan(plan: object, *, side: str, stable: str, amount: str) -> bool:
    """Return False only for the known unbuilt approval/permit branch."""
    if type(plan) is not dict or any((
        plan.get("mode") != "LIVE_ETH_SWAP_PLAN_ONLY",
        plan.get("asset") != "ETH",
        type(plan.get("chain_id")) is not int or plan.get("chain_id") != 1,
        plan.get("side") != side,
        plan.get("input_asset") != (stable if side == "BUY" else "ETH"),
        plan.get("output_asset") != ("ETH" if side == "BUY" else stable),
        plan.get("input_amount") != amount,
        plan.get("execution_enabled") is not False,
        plan.get("can_submit") is not False,
        plan.get("automatic_signing_enabled") is not False,
    )):
        raise ValueError("Unrecognized or inconsistent verified swap plan")
    if plan.get("state") == "APPROVAL_OR_PERMIT_REQUIRED":
        if plan.get("transaction_built") is not False:
            raise ValueError("Conflicting approval-only swap plan")
        return False
    if plan.get("state") != "UNSIGNED_TX_REVIEW_REQUIRED":
        raise ValueError("Unrecognized unsigned swap plan state")
    if plan.get("transaction_built") is not True or not all(
        plan.get(k) is True for k in (
            "tx_outer_fields_verified", "router_command_pattern_verified",
            "router_commands_decoded", "router_command_contents_verified",
            "router_recipients_verified", "router_quote_bounds_verified",
        )
    ) or plan.get("safe_to_sign") is not False:
        raise ValueError("Provider plan missing strict router verification")
    if plan.get("erc20_approval_required") is not False or plan.get("permit_signature_required") is not False:
        raise ValueError("Swap plan requires unreviewed token permission")
    return True


def _ledger_limits(ledger: object, limits: object) -> tuple[dict, str, str, tuple[str, ...]]:
    if (type(ledger) is not dict or ledger.get("mode") != "ETH_LIVE_LEDGER_READ_ONLY"
            or ledger.get("asset") != "ETH" or ledger.get("chain_id") != 1
            or ledger.get("execution_enabled") is not False
            or ledger.get("paper_simulation_included") is not False
            or type(limits) is not dict or limits.get("asset") != "ETH"
            or limits.get("chain_id") != 1 or limits.get("trading_paused") is not True):
        raise ValueError("Verified user ETH ledger or stop state unavailable")
    if (type(ledger.get("limits_revision")) is not int
            or ledger["limits_revision"] != limits.get("revision")
            or ledger.get("trading_paused") is not limits.get("trading_paused")
            or str(ledger.get("planned_capital_usd")) != limits.get("planned_capital_usd")
            or str(ledger.get("max_order_usd")) != limits.get("max_order_usd")
            or str(ledger.get("rolling_24h_limit_usd")) != limits.get("max_daily_turnover_usd")):
        raise ValueError("User ETH limits changed during cost preview")
    for field in ("unresolved_order_count", "reconciliation_required_count", "older_than_30m_pending_count"):
        if type(ledger.get(field)) is not int or ledger[field] < 0:
            raise ValueError("ETH pending order count unavailable")
    blockers = []
    if ledger["unresolved_order_count"]:
        blockers.append("UNRESOLVED_LIVE_ETH_ORDER")
    if ledger["reconciliation_required_count"]:
        blockers.append("ETH_RECEIPT_RECONCILIATION_REQUIRED")
    if ledger["older_than_30m_pending_count"]:
        blockers.append("STALE_PENDING_ETH_ORDER")
    data = {
        "asset": "ETH",
        "revision": limits["revision"],
        "trading_paused": limits["trading_paused"],
        "planned_capital_usd": limits["planned_capital_usd"],
        "max_order_usd": limits["max_order_usd"],
        "max_daily_turnover_usd": limits["max_daily_turnover_usd"],
    }
    return (data, str(ledger["pending_reserved_usd"]),
            str(ledger["confirmed_24h_usd"]), tuple(blockers))


def _fresh_wallet(snapshot: object, user: object, did: str, input_symbol: str) -> tuple[int, int]:
    if type(snapshot) is not dict or snapshot.get("mode") != "VERIFIED_READ_ONLY" or snapshot.get("wallet_ownership_verified") is not True:
        raise ValueError("Verified wallet balance evidence missing")
    try:
        owner_wallet = verified_addresses(user, did)["ETH"]
    except (ValueError, TypeError, KeyError):
        raise ValueError("Owner Privy wallet could not be independently checked") from None
    wallets, balances = snapshot.get("wallets"), snapshot.get("balances")
    if (type(owner_wallet) is not str or type(wallets) is not dict
            or type(wallets.get("ETH")) is not str
            or wallets["ETH"].lower() != owner_wallet.lower()
            or type(balances) is not dict):
        raise ValueError("Verified wallet mismatch during cost preview")
    try:
        checked_at = datetime.fromisoformat(snapshot["as_of"])
        if checked_at.tzinfo is None:
            raise ValueError("Unverified balance timestamp")
        age = (datetime.now(timezone.utc) - checked_at).total_seconds()
        if not -2 <= age <= _MAX_AGE:
            raise ValueError("Stale wallet balance snapshot")
    except (KeyError, TypeError, ValueError):
        raise ValueError("Unverified wallet balance timestamp") from None
    for asset in (input_symbol, "ETH"):
        record = balances.get(asset)
        if type(record) is not dict or record.get("status") != "verified" or record.get("network") != "ethereum-mainnet":
            raise ValueError("Independent onchain balance missing")
    input_decimals = 6 if input_symbol in ("USDC", "USDT") else 18
    return (_atomic_balance(balances[input_symbol].get("balance"), input_decimals),
            _atomic_balance(balances["ETH"].get("balance"), 18))


def _review_verified_evidence(
    *, plan: dict, did: str, side: str, stable: str, amount: str,
    user: object, wallet: object, prices: object, ledger: object,
    limits: object, now: int,
) -> dict:
    if not _checked_plan(plan, side=side, stable=stable, amount=amount):
        raise ValueError("Incomplete Uniswap plan cannot receive gas-cost review")
    input_symbol = stable if side == "BUY" else "ETH"
    input_balance, eth_balance = _fresh_wallet(wallet, user, did, input_symbol)
    eth_price, eth_age = _fresh_reference(prices, "ETH", now=now)
    stable_price, stable_age = _fresh_reference(prices, stable, now=now)
    budget, pending, spent, ledger_blockers = _ledger_limits(ledger, limits)
    requested, atomic = _quantity(amount, 6 if side == "BUY" else 18)
    minimum, min_atomic = _quantity(
        str(plan.get("minimum_output_amount")),
        18 if side == "BUY" else 6,
    )
    if requested <= 0 or minimum <= 0:
        raise ValueError("Invalid quote amount")
    gas_limit = plan.get("max_gas_limit")
    if type(gas_limit) is not int or gas_limit < 1:
        raise ValueError("Missing verified gas limit")
    fee_gwei = plan.get("max_fee_per_gas_gwei")
    if type(fee_gwei) is not str or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?", fee_gwei):
        raise ValueError("Missing exact verified max gas fee")
    gas_wei = Decimal(fee_gwei) * 10**9
    if gas_wei != gas_wei.to_integral_value():
        raise ValueError("Fractional gas wei")
    review = review_eth_cost_budget(
        side=side, stable=stable, input_atomic=int(atomic),
        minimum_output_atomic=int(min_atomic),
        gas_limit=gas_limit, max_fee_per_gas_wei=int(gas_wei),
        eth_usd=eth_price, eth_reference_age_seconds=eth_age,
        stable_usd=stable_price, stable_reference_age_seconds=stable_age,
        input_balance_atomic=input_balance, eth_balance_wei=eth_balance,
        provider_fee_stable_atomic=0,
        live_limits=budget, pending_capital_usd=pending,
        spent_24h_usd=spent,
    )
    review["operational_blockers"] = list(_ALWAYS_BLOCKED + ledger_blockers)
    review["mode"] = "ETH_LIVE_COST_PREVIEW_ONLY"
    review["state"] = "INDICATIVE_COST_REVIEW"
    review["cost_estimate_available"] = True
    review["wallet_identity_checked_at_preview"] = True
    review["price_reference_source"] = "COINGECKO_LAST_UPDATED_AT"
    review["onchain_balance_source"] = "EXPLORER_OR_PUBLIC_RPC_ESTIMATE"
    review["same_transaction_budget_lock_held"] = False
    review["can_reserve"] = False
    review["can_sign"] = False
    review["can_submit"] = False
    review["live_trading_enabled"] = False
    return review


async def get_eth_readonly_cost_preview(did: str, *, side: str, stable: str, amount: str) -> dict:
    """Separate preview route. Nothing can sign, grant, reserve or broadcast."""
    did = _safe_did(did)
    plan = await make_eth_swap_plan(did, side=side, stable=stable, amount=amount)
    try:
        reviewed = _checked_plan(plan, side=side, stable=stable, amount=amount)
    except (ValueError, TypeError):
        raise HTTPException(503, "Unable to verify ETH swap plan evidence") from None
    if not reviewed:
        return {
            "mode": "ETH_LIVE_COST_PREVIEW_ONLY", "asset": "ETH", "chain_id": 1,
            "state": "APPROVAL_OR_PERMIT_REQUIRED",
            "cost_estimate_available": False,
            "operational_blockers": list(_ALWAYS_BLOCKED + ("APPROVAL_OR_PERMIT_REQUIRED",)),
            "can_reserve": False, "can_sign": False, "can_submit": False,
            "live_trading_enabled": False,
        }
    try:
        user, wallet, prices, ledger, limits = await asyncio.gather(
            fetch_privy_user(did),
            get_wallet_balances(did),
            fetch_reference_prices(),
            asyncio.to_thread(get_eth_live_ledger_audit, did),
            asyncio.to_thread(get_live_eth_limits, did),
        )
        return _review_verified_evidence(
            plan=plan, did=did, side=side, stable=stable, amount=amount,
            user=user, wallet=wallet, prices=prices, ledger=ledger,
            limits=limits, now=int(time.time()),
        )
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        raise HTTPException(503, "Independent ETH cost or budget evidence unavailable") from None
