"""Fail-closed, read-only comparison of independently observed ETH swap legs.

This is a *diagnostic*; it never proves that a particular MiniNex order
initiated either transaction. The live executor and durable settlement ledger
are deliberately disabled. No signing, broadcast, balance credit or HWM writes.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ASSETS = {"USDC", "USDT"}


def _amount(value: object, *, places: int) -> Decimal:
    if type(value) is not str or not value or len(value) > 72:
        raise ValueError("Missing exact on-chain amount")
    if "e" in value.lower() or value.startswith(("+", "-")):
        raise ValueError("Non-canonical on-chain amount")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid on-chain amount") from None
    if (not amount.is_finite() or amount <= 0 or amount >= Decimal("1e27")
            or amount.as_tuple().exponent < -places):
        raise ValueError("On-chain amount out of bounds")
    return amount


def _fee(value: object) -> Decimal:
    if type(value) is not str or not value or len(value) > 72:
        raise ValueError("Missing exact gas fee")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid gas fee") from None
    if (not amount.is_finite() or amount < 0 or amount > Decimal("100")
            or amount.as_tuple().exponent < -18):
        raise ValueError("Gas fee is invalid")
    return amount


def compare_eth_native_cycle(
    *, swap: dict, conversion: dict, side: str, stable: str,
) -> dict:
    """Cross-check 2 FINALIZED receipts without establishing order ownership.

    BUY: stable -> WETH swap, then WETH -> ETH withdrawal.
    SELL: ETH -> WETH deposit, then WETH -> stable swap.

    A caller MUST independently bind both receipts to the same Privy wallet,
    trade intent, and chronological durable execution log before settlement.
    Receipt payload fields are not proof of that binding.
    """
    if type(swap) is not dict or type(conversion) is not dict:
        raise ValueError("Both transaction receipts are required")
    if side not in ("BUY", "SELL") or stable not in _ASSETS:
        raise ValueError("Unsupported native ETH cycle")
    for report in (swap, conversion):
        if report.get("asset") != "ETH" or report.get("network") != "ethereum-mainnet":
            raise ValueError("Mixed chain or asset")
        tx = report.get("tx_hash")
        if type(tx) is not str or not _HASH.fullmatch(tx):
            raise ValueError("Transaction identity is missing")
        if (report.get("execution_enabled") is not False
                or report.get("can_submit") is not False):
            raise ValueError("Untrusted execution claim")
    if swap["tx_hash"] == conversion["tx_hash"]:
        raise ValueError("Both legs cannot share a transaction hash")
    if (swap.get("state") != "FINALIZED_ERC20_SWAP_LEG"
            or swap.get("transaction_finalized") is not True
            or swap.get("swap_erc20_leg_verified") is not True
            or swap.get("side") != side or swap.get("stable") != stable
            or swap.get("swap_output_asset") != ("WETH" if side == "BUY" else stable)):
        raise ValueError("Swap leg is not independently finalized")
    expected_op = "WITHDRAW" if side == "BUY" else "DEPOSIT"
    if (conversion.get("state") != "FINALIZED_WETH9_CONVERSION"
            or conversion.get("transaction_finalized") is not True
            or conversion.get("confirmed_native_conversion") is not True
            or conversion.get("operation") != expected_op
            or conversion.get("converted_asset") != ("ETH" if side == "BUY" else "WETH")):
        raise ValueError("Native conversion is not independently finalized")
    source = _amount(swap.get("source_spent_amount"),
                     places=6 if side == "BUY" else 18)
    destination = _amount(swap.get("destination_received_amount"),
                          places=18 if side == "BUY" else 6)
    converted = _amount(conversion.get("weth9_event_amount"), places=18)
    weth_swap = destination if side == "BUY" else source
    if converted != weth_swap:
        raise ValueError("WETH swap and conversion quantities differ")
    swap_gas = _fee(swap.get("network_fee_eth"))
    conversion_gas = _fee(conversion.get("network_fee_eth"))
    total_gas = swap_gas + conversion_gas
    if side == "BUY":
        stable_spent, stable_received = source, None
        native_received, native_spent = converted, None
    else:
        stable_spent, stable_received = None, destination
        native_received, native_spent = None, converted
    return {
        "mode": "ETH_NATIVE_CYCLE_RECEIPT_COMPARISON_ONLY",
        "asset": "ETH", "network": "ethereum-mainnet",
        "side": side, "stable": stable,
        "swap_tx_hash": swap["tx_hash"],
        "conversion_tx_hash": conversion["tx_hash"],
        "independent_legs_finalized": True,
        "weth_quantities_match": True,
        "stable_spent": str(stable_spent) if stable_spent is not None else None,
        "stable_received": str(stable_received) if stable_received is not None else None,
        "native_eth_spent": str(native_spent) if native_spent is not None else None,
        "native_eth_received": str(native_received) if native_received is not None else None,
        "total_network_fee_eth": str(total_gas),
        "same_order_proven": False,
        "same_owner_binding_proven": False,
        "leg_order_proven": False,
        "ledger_settled": False,
        "portfolio_credit_allowed": False,
        "performance_fee_allowed": False,
        "privy_grant_verified": False,
        "execution_enabled": False, "can_submit": False,
    }
