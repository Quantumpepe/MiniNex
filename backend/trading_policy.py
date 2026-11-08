"""Fail-closed future trade-policy evaluation; no signing or order execution.

Preview values are *not* authorization. In a future live system the user policy
must be stored server-side, bound to an authenticated DID and a verified wallet,
and checked again immediately before every transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

ASSETS = ("BTC", "ETH")
STABLES = ("USDC", "USDT")
MAX_SLIPPAGE_BPS = 100  # cap for candidate validation, not a live setting


@dataclass(frozen=True)
class BudgetPreview:
    asset: str
    percent: int
    available_usd: Decimal
    reserved_usd: Decimal
    unallocated_usd: Decimal


def _amount(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("invalid amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid amount") from None
    if not amount.is_finite() or not 0 <= amount <= Decimal("1000000000000"):
        raise ValueError("amount out of bounds")
    return amount


def preview_budget(*, asset: str, percent: int, available_usd: object) -> BudgetPreview:
    """Calculate a non-authorizing budget; BTC and ETH must be called separately."""
    if asset not in ASSETS:
        raise ValueError("unsupported asset")
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError("percent must be an integer between 0 and 100")
    available = _amount(available_usd)
    reserve = available * Decimal(percent) / Decimal(100)
    return BudgetPreview(
        asset=asset, percent=percent, available_usd=available,
        reserved_usd=reserve, unallocated_usd=available-reserve,
    )


def inspect_future_order(
    *, asset: str, stable: str, budget_pct: int, available_usd: object,
    proposed_notional_usd: object, slippage_bps: int,
    authenticated: bool, wallet_verified: bool, route_ready: bool,
    policy_persisted: bool, signing_authorized: bool, live_enabled: bool,
) -> tuple[bool, str]:
    """Always fail closed: no live caller can pass with current configuration."""
    if authenticated is not True:
        return False, "AUTH_REQUIRED"
    if asset not in ASSETS or stable not in STABLES:
        return False, "UNSUPPORTED_PAIR"
    if wallet_verified is not True:
        return False, "WALLET_NOT_VERIFIED"
    if policy_persisted is not True:
        return False, "BUDGET_NOT_AUTHORIZED"
    if route_ready is not True:
        return False, "ROUTE_NOT_READY"
    if signing_authorized is not True:
        return False, "SIGNING_NOT_AUTHORIZED"
    if live_enabled is not True:
        return False, "LIVE_DISABLED"
    try:
        budget = preview_budget(asset=asset, percent=budget_pct, available_usd=available_usd)
        amount = _amount(proposed_notional_usd)
    except ValueError:
        return False, "INVALID_BUDGET"
    if type(slippage_bps) is not int or not 0 <= slippage_bps <= MAX_SLIPPAGE_BPS:
        return False, "SLIPPAGE_LIMIT"
    if amount <= 0 or amount > budget.reserved_usd:
        return False, "BUDGET_EXCEEDED"
    return True, "POLICY_CHECKS_PASSED_NOT_EXECUTION_AUTHORIZATION"
