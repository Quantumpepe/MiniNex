"""Fail-closed, non-executable ETH swap cost/capital arithmetic.

The future privileged executor must source every value from a verified wallet,
independent fresh oracle, exact quote and same-transaction ETH budget/ledger
locks. This pure function cannot attest to the provenance of its arguments.
It performs no network or database calls and never provides signing authority.
"""
from __future__ import annotations

from decimal import Decimal
import re

_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?$")
_WEI = Decimal(10) ** 18
_MICRO = Decimal(10) ** 6
_MAX_GAS_LIMIT = 2_000_000
_MAX_FEE_PER_GAS_WEI = 300_000_000_000
_MAX_GAS_WEI = 4_000_000_000_000_000
_GAS_CUSHION_WEI = 5_000_000_000_000_000
_MAX_REFERENCE_AGE_SECONDS = 30
_MAX_PRICE_IMPACT_BPS = Decimal(200)


def _integer(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value >= 10**45:
        raise ValueError("Invalid exact atomic amount or gas parameter")
    return value


def _decimal(value: object, *, positive: bool = False) -> Decimal:
    if type(value) is not str or len(value) > 48 or not _DECIMAL.fullmatch(value):
        raise ValueError("Untrusted decimal evidence")
    number = Decimal(value)
    if not number.is_finite() or number >= Decimal("10000000") or (number <= 0 if positive else number < 0):
        raise ValueError("Decimal outside supported bounds")
    return number


def _format(value: Decimal) -> str:
    return format(value, "f")


def review_eth_cost_budget(
    *, side: str, stable: str, input_atomic: int, minimum_output_atomic: int,
    gas_limit: int, max_fee_per_gas_wei: int,
    eth_usd: str, eth_reference_age_seconds: int,
    stable_usd: str, stable_reference_age_seconds: int,
    input_balance_atomic: int, eth_balance_wei: int,
    provider_fee_stable_atomic: int,
    live_limits: dict, pending_capital_usd: str, spent_24h_usd: str,
) -> dict:
    """Conservative arithmetic review, *never* a trade-ready or authority proof.

    Inputs require independent trusted evidence at the future call site; the
    function deliberately does NOT accept another coin's budget or PAPER funds.
    Spending is bounded by order cost including max gas and explicit fees.
    Both conversion prices are indicative, not stablecoin par assumptions.
    """
    if side not in ("BUY", "SELL") or stable not in ("USDC", "USDT"):
        raise ValueError("Unsupported single-chain ETH pair")
    if type(live_limits) is not dict or live_limits.get("asset") != "ETH":
        raise ValueError("Cross-coin or missing ETH capital budget")
    if type(live_limits.get("revision")) is not int or live_limits["revision"] < 0:
        raise ValueError("Invalid ETH budget revision")
    if type(live_limits.get("trading_paused")) is not bool:
        raise ValueError("Unknown ETH stop state")

    input_atomic = _integer(input_atomic, positive=True)
    minimum_output_atomic = _integer(minimum_output_atomic, positive=True)
    input_balance_atomic = _integer(input_balance_atomic)
    eth_balance_wei = _integer(eth_balance_wei)
    provider_fee_stable_atomic = _integer(provider_fee_stable_atomic)
    gas_limit = _integer(gas_limit, positive=True)
    max_fee_per_gas_wei = _integer(max_fee_per_gas_wei, positive=True)
    eth_reference_age_seconds = _integer(eth_reference_age_seconds)
    stable_reference_age_seconds = _integer(stable_reference_age_seconds)
    if (gas_limit > _MAX_GAS_LIMIT or max_fee_per_gas_wei > _MAX_FEE_PER_GAS_WEI
            or gas_limit * max_fee_per_gas_wei > _MAX_GAS_WEI):
        raise ValueError("Unsafe maximum gas liability")
    if (eth_reference_age_seconds > _MAX_REFERENCE_AGE_SECONDS
            or stable_reference_age_seconds > _MAX_REFERENCE_AGE_SECONDS):
        raise ValueError("Stale or unknown independent price")

    eth_price = _decimal(eth_usd, positive=True)
    stable_price = _decimal(stable_usd, positive=True)
    # A broken/depegged stablecoin must not be treated as exactly one USD.
    if stable_price < Decimal("0.98") or stable_price > Decimal("1.02"):
        raise ValueError("Unverifiable stable settlement price")
    capital = _decimal(live_limits.get("planned_capital_usd"))
    order_cap = _decimal(live_limits.get("max_order_usd"))
    daily_cap = _decimal(live_limits.get("max_daily_turnover_usd"))
    pending = _decimal(pending_capital_usd)
    spent = _decimal(spent_24h_usd)

    input_units = Decimal(input_atomic) / (_MICRO if side == "BUY" else _WEI)
    output_min_units = Decimal(minimum_output_atomic) / (_WEI if side == "BUY" else _MICRO)
    input_usd = input_units * (stable_price if side == "BUY" else eth_price)
    minimum_output_usd = output_min_units * (eth_price if side == "BUY" else stable_price)
    fee_usd = Decimal(provider_fee_stable_atomic) / _MICRO * stable_price
    worst_gas_wei = gas_limit * max_fee_per_gas_wei
    gas_usd = Decimal(worst_gas_wei) / _WEI * eth_price
    total_usd = input_usd + fee_usd + gas_usd
    # A minOut lower than the input by over 2% fails closed even when
    # the user-configured capital still covers the transaction.
    price_impact_bps = max(Decimal(0), (input_usd - minimum_output_usd) * 10_000 / input_usd)

    blockers = []
    if live_limits["trading_paused"]:
        blockers.append("ETH_STOP_ACTIVE")
    if not capital or not order_cap or not daily_cap:
        blockers.append("ETH_LIMITS_NOT_CONFIGURED")
    if input_balance_atomic < input_atomic + (provider_fee_stable_atomic if side == "BUY" else 0):
        blockers.append("INPUT_BALANCE_TOO_LOW")
    native_spend = input_atomic if side == "SELL" else 0
    if eth_balance_wei < native_spend + worst_gas_wei + _GAS_CUSHION_WEI:
        blockers.append("ETH_GAS_RESERVE_NOT_COVERED")
    if side == "SELL" and provider_fee_stable_atomic >= minimum_output_atomic:
        blockers.append("FEES_EXCEED_MINIMUM_PROCEEDS")
    if price_impact_bps > _MAX_PRICE_IMPACT_BPS:
        blockers.append("MINIMUM_OUTPUT_TOO_LOW")
    if total_usd > order_cap:
        blockers.append("ORDER_PLUS_FEES_OVER_LIMIT")
    if pending + total_usd > capital:
        blockers.append("RESERVED_CAPITAL_OVER_LIMIT")
    if spent + pending + total_usd > daily_cap:
        blockers.append("ROLLING_TURNOVER_OVER_LIMIT")
    return {
        "asset": "ETH", "chain_id": 1, "stable": stable, "side": side,
        "budget_revision": live_limits["revision"],
        "order_input_usd": _format(input_usd),
        "max_possible_gas_eth": _format(Decimal(worst_gas_wei) / _WEI),
        "max_possible_gas_usd_indicative": _format(gas_usd),
        "explicit_provider_fee_usd": _format(fee_usd),
        "max_total_input_plus_costs_usd_indicative": _format(total_usd),
        "minimum_output_value_usd_indicative": _format(minimum_output_usd),
        "minimum_output_impact_bps_indicative": _format(price_impact_bps),
        "price_evidence_max_age_seconds": max(eth_reference_age_seconds, stable_reference_age_seconds),
        "within_declared_cost_limits": not blockers,
        "blockers": blockers,
        "inputs_independently_verified_here": False,
        "capital_row_lock_verified_here": False,
        "quote_executable": False,
        "can_reserve": False,
        "can_sign": False,
        "can_submit": False,
        "live_trading_enabled": False,
    }
