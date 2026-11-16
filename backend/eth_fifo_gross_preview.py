"""Conservative read-only ETH cost-basis preview from confirmed Router02 swaps.

This is NOT realized *net USD* income, a tax lot book, or authority to assess
the 5% fee. It excludes historical ETH gas USD valuation, verified bridge/
deposit flows, cross-stable conversion, and any pre-history beyond the query
window. Only stable-denominated gross swap spread is shown.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal, InvalidOperation, localcontext

from fastapi import HTTPException

_STABLES = ("USDC", "USDT")


def _amount(value: object) -> Decimal:
    if type(value) is not str or len(value) > 78:
        raise ValueError("Missing precise finalized swap amount")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid confirmed swap quantity") from None
    if (not amount.is_finite() or amount <= 0 or amount >= Decimal("1e25")
            or amount.as_tuple().exponent < -18):
        raise ValueError("Confirmed swap quantity outside supported precision")
    return amount


def eth_fifo_gross_preview(history: dict) -> dict:
    """Match eligible confirmed WETH quantities FIFO within each stablecoin.

    Never silently use an unmatched SELL's unknown cost basis or treat gross
    swap spread as a net USD performance-fee amount. External native inventory,
    stable changes and network fees require separate reconciliation.
    """
    if (type(history) is not dict
            or history.get("source") != "CONFIRMED_AUTO_ROUTER02_SWAPS"
            or history.get("asset") != "ETH"
            or history.get("includes_paper") is not False
            or type(history.get("history_truncated")) is not bool
            or type(history.get("swaps")) is not list):
        raise ValueError("Verified, bounded ETH trade history required")
    rows = history["swaps"]
    if len(rows) > 100:
        raise ValueError("Unbounded confirmed swap history")
    inventories = {stable: deque() for stable in _STABLES}
    gross = {stable: Decimal(0) for stable in _STABLES}
    close_events = []
    unmatched = 0
    # The API returns newest first; reverse chronologically for FIFO.
    # Distinct intent IDs are mandatory, not merely matching raw amounts.
    seen = set()
    with localcontext() as context:
        context.prec = 60
        for row in reversed(rows):
            if type(row) is not dict:
                raise ValueError("Malformed confirmed swap row")
            key = row.get("intent_key")
            stable = row.get("stable")
            side = row.get("side")
            if (type(key) is not str or not key or key in seen
                    or stable not in _STABLES or side not in ("BUY", "SELL")
                    or row.get("onchain_finalized") is not True):
                raise ValueError("Ambiguous finalized trade identity")
            seen.add(key)
            source = _amount(row.get("spent_amount"))
            received = _amount(row.get("received_amount"))
            if side == "BUY":
                if (row.get("spent_asset") != stable
                        or row.get("received_asset") != "WETH"):
                    raise ValueError("Invalid BUY source or destination")
                inventories[stable].append([received, source])
                continue
            if (row.get("spent_asset") != "WETH"
                    or row.get("received_asset") != stable):
                raise ValueError("Invalid SELL source or destination")
            quantity = source
            pending = quantity
            matched_cost = Decimal(0)
            lots = inventories[stable]
            # Do not emit a partially costed trade as a realized profit.
            if sum((lot[0] for lot in lots), Decimal(0)) < quantity:
                unmatched += 1
                close_events.append({
                    "intent_key": key, "stable": stable,
                    "status": "UNMATCHED_COST_BASIS",
                    "gross_spread_stable": None,
                })
                # Remaining known inventory still came from BUY events and
                # may subsequently be sold. This SELL consumes it first;
                # unknown external inventory cannot generate known profit.
                while pending > 0 and lots:
                    lot = lots[0]
                    consumed = min(pending, lot[0])
                    old_qty = lot[0]
                    lot[1] -= lot[1] * consumed / old_qty
                    lot[0] -= consumed
                    pending -= consumed
                    if lot[0] == 0:
                        lots.popleft()
                continue
            while pending > 0:
                lot = lots[0]
                consumed = min(pending, lot[0])
                allocated = lot[1] * consumed / lot[0]
                matched_cost += allocated
                lot[0] -= consumed
                lot[1] -= allocated
                pending -= consumed
                if lot[0] == 0:
                    lots.popleft()
            spread = received - matched_cost
            gross[stable] += spread
            close_events.append({
                "intent_key": key, "stable": stable,
                "status": "MATCHED_GROSS_ONLY",
                "gross_spread_stable": format(spread, "f"),
                "allocated_buy_cost_stable": format(matched_cost, "f"),
                "gross_sell_proceeds_stable": format(received, "f"),
            })
    complete = unmatched == 0 and history["history_truncated"] is False
    return {
        "asset": "ETH",
        "mode": "READ_ONLY_GROSS_SWAP_SPREAD",
        "source": "CONFIRMED_AUTO_ROUTER02_SWAPS",
        "historical_sample_truncated": history["history_truncated"],
        "unmatched_sell_count": unmatched,
        "all_sell_cost_basis_matched_within_sample": complete,
        "closed_trades": list(reversed(close_events)),
        "gross_spread_by_stable": {
            stable: format(gross[stable], "f") if complete else None
            for stable in _STABLES
        },
        "stablecoin_fx_and_eth_gas_costs_reconciled": False,
        "external_inventory_reconciled": False,
        "verified_net_pnl_usd": None,
        "high_water_mark_updated": False,
        "five_percent_fee_assessed": False,
        "fee_collection_enabled": False,
    }


def read_eth_fifo_gross_preview(did: str) -> dict:
    from .real_eth_trade_legs import get_confirmed_eth_swaps
    history = get_confirmed_eth_swaps(did)
    try:
        return eth_fifo_gross_preview(history)
    except (ValueError, TypeError, ArithmeticError):
        raise HTTPException(503, "ETH realized cost basis preview unavailable") from None
