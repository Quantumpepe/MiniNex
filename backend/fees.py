"""MiniNex 5% realized-net performance fee, high-water-mark calculation.

Deltas represent verified realized PnL in USD-equivalent AFTER trading costs
but BEFORE performance fees, sourced by a future trusted execution reconciler.
No demo values, wallet deposits or withdrawals belong in this sequence.
Unrealized losses require an equity-level gate before enabling real collection.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

RATE = Decimal("0.05")
CENT_MICRO = Decimal("0.000001")
ACCOUNT_PRECISION = Decimal("0.000000000001")


@dataclass(frozen=True)
class FeeAssessment:
    cumulative_usd: Decimal
    high_water_mark_usd: Decimal
    new_profit_above_hwm_usd: Decimal
    fee_assessed_usd: Decimal


def decimal_amount(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("amount is required")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid monetary amount") from None
    if not amount.is_finite() or abs(amount) >= Decimal("1e22"):
        raise ValueError("amount outside safe range")
    return amount.quantize(ACCOUNT_PRECISION)


def assess_realized_gain(
    cumulative_usd: object, high_water_mark_usd: object, realized_delta_usd: object
) -> FeeAssessment:
    cumulative = decimal_amount(cumulative_usd)
    hwm = decimal_amount(high_water_mark_usd)
    delta = decimal_amount(realized_delta_usd)
    if hwm < 0:
        raise ValueError("negative high-water mark is forbidden")
    updated = cumulative + delta
    gain = max(Decimal("0"), updated - hwm)
    fee = (gain * RATE).quantize(CENT_MICRO, rounding=ROUND_HALF_UP)
    next_hwm = max(hwm, updated)
    return FeeAssessment(
        cumulative_usd=updated,
        high_water_mark_usd=next_hwm,
        new_profit_above_hwm_usd=gain,
        fee_assessed_usd=fee,
    )
