"""Regression coverage for a read-only ETH two-receipt comparison."""
from copy import deepcopy

import pytest

from backend.eth_native_cycle_comparison import compare_eth_native_cycle

A = "0x" + "a" * 64
B = "0x" + "b" * 64


def evidence(side="BUY", stable="USDC"):
    amount = "0.42"
    return ({
        "asset": "ETH", "network": "ethereum-mainnet",
        "tx_hash": A, "side": side, "stable": stable,
        "swap_output_asset": "WETH" if side == "BUY" else stable,
        "state": "FINALIZED_ERC20_SWAP_LEG",
        "transaction_finalized": True, "swap_erc20_leg_verified": True,
        "source_spent_amount": "1000.50" if side == "BUY" else amount,
        "destination_received_amount": amount if side == "BUY" else "1000.50",
        "network_fee_eth": "0.00011",
        "execution_enabled": False, "can_submit": False,
    }, {
        "asset": "ETH", "network": "ethereum-mainnet",
        "tx_hash": B, "operation": "WITHDRAW" if side == "BUY" else "DEPOSIT",
        "converted_asset": "ETH" if side == "BUY" else "WETH",
        "state": "FINALIZED_WETH9_CONVERSION",
        "transaction_finalized": True, "confirmed_native_conversion": True,
        "weth9_event_amount": amount, "network_fee_eth": "0.00012",
        "execution_enabled": False, "can_submit": False,
    })


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("stable", ["USDC", "USDT"])
def test_both_directions_stablecoins_are_only_diagnostic(side, stable):
    swap, conversion = evidence(side, stable)
    report = compare_eth_native_cycle(swap=swap, conversion=conversion,
                                      side=side, stable=stable)
    assert report["independent_legs_finalized"]
    assert report["total_network_fee_eth"] == "0.00023"
    assert report["same_order_proven"] is False
    assert report["ledger_settled"] is False
    assert report["portfolio_credit_allowed"] is False
    assert report["performance_fee_allowed"] is False
    assert report["execution_enabled"] is False
    assert report["native_eth_received" if side == "BUY" else "native_eth_spent"] == "0.42"


@pytest.mark.parametrize("mutate", [
    lambda s,c: s.update(state="PENDING"),
    lambda s,c: c.update(confirmed_native_conversion=False),
    lambda s,c: c.update(weth9_event_amount="0.41"),
    lambda s,c: c.update(tx_hash=A),
    lambda s,c: s.update(network="bitcoin-mainnet"),
    lambda s,c: s.update(stable="USDT"),
    lambda s,c: c.update(operation="DEPOSIT"),
    lambda s,c: s.update(destination_received_amount="NaN"),
    lambda s,c: s.update(destination_received_amount="0.4200000000000000001"),
    lambda s,c: c.update(network_fee_eth="-0.01"),
    lambda s,c: s.update(network_fee_eth=None),
    lambda s,c: s.update(execution_enabled=True),
    lambda s,c: c.update(can_submit=True),
    lambda s,c: s.update(tx_hash="0xabc"),
    lambda s,c: c.update(transaction_finalized=1),
])
def test_invalid_or_unsettled_receipts_fail_closed(mutate):
    swap, conversion = evidence()
    mutate(swap, conversion)
    with pytest.raises(ValueError):
        compare_eth_native_cycle(swap=swap, conversion=conversion,
                                 side="BUY", stable="USDC")


@pytest.mark.parametrize("side,stable", [("SHORT", "USDC"), ("BUY", "BTC")])
def test_unsupported_route_rejected(side, stable):
    swap, conversion = evidence()
    with pytest.raises(ValueError):
        compare_eth_native_cycle(swap=swap, conversion=conversion,
                                 side=side, stable=stable)


def test_input_evidence_is_not_mutated():
    swap, conversion = evidence()
    original = deepcopy((swap, conversion))
    compare_eth_native_cycle(swap=swap, conversion=conversion,
                             side="BUY", stable="USDC")
    assert (swap, conversion) == original
