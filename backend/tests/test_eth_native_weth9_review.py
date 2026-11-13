"""Native ETH/WETH conversion is inspected, not authorized or broadcast."""
from copy import deepcopy

import pytest

from backend import app as api
from backend import eth_native_weth9_review as conversion


WALLET = "0x" + "a" * 40
ONE_ETH = 10**18
AMOUNT = 10**17


def deposit_tx(amount=AMOUNT):
    return {
        "from": WALLET,
        "to": conversion.WETH_MAINNET,
        "chainId": 1,
        "value": str(amount),
        "data": conversion.WETH9_DEPOSIT_SELECTOR,
    }


def withdraw_tx(amount=AMOUNT):
    return {
        "from": WALLET,
        "to": conversion.WETH_MAINNET,
        "chainId": 1,
        "value": "0",
        "data": conversion.WETH9_WITHDRAW_SELECTOR
                + amount.to_bytes(32, "big").hex(),
    }


def review(tx, *, operation="DEPOSIT", amount=AMOUNT,
           native=ONE_ETH, weth=ONE_ETH):
    return conversion.inspect_weth9_conversion(
        tx, wallet=WALLET, operation=operation,
        exact_amount_wei=str(amount),
        native_balance_wei=str(native),
        weth_balance_wei=str(weth),
    )


def test_exact_weth9_deposit_and_withdraw_are_readonly_and_not_signable():
    for tx, operation, expected in (
        (deposit_tx(), "DEPOSIT", "ETH_TO_WETH"),
        (withdraw_tx(), "WITHDRAW", "WETH_TO_ETH"),
    ):
        r = review(tx, operation=operation)
        assert r["chain_id"] == 1
        assert r["weth_contract"] == conversion.WETH_MAINNET
        assert r["movement"] == expected
        assert r["exact_amount_wei"] == str(AMOUNT)
        assert r["wallet_address_matched"] is True
        assert r["value_and_call_data_checked"] is True
        assert r["independent_rpc_balance_proof"] is False
        assert r["confirmed_token_deltas_verified"] is False
        assert r["privy_authorization_verified"] is False
        assert r["execution_enabled"] is False
        assert r["safe_to_sign"] is False
        assert r["can_submit"] is False
        assert "data" not in r
        assert "private_key" not in r


@pytest.mark.parametrize("patch", [
    {"from": "0x" + "b" * 40},
    {"from": None},
    {"to": "0x" + "b" * 40},
    {"to": None},
    {"chainId": 56},
    {"chainId": True},
    {"data": "0x"},
    {"data": "0xdeadbeef"},
    {"data": conversion.WETH9_DEPOSIT_SELECTOR + "00" * 32},
    {"value": "0"},
    {"value": 0},
    {"value": True},
    {"value": "0x16345785d8a0000"},
    {"value": "100000000000000001"},
])
def test_deposit_rejects_wrong_wallet_contract_chain_method_and_amount(patch):
    with pytest.raises(ValueError):
        review({**deposit_tx(), **patch})


@pytest.mark.parametrize("patch", [
    {"from": "0x" + "b" * 40},
    {"to": "0x" + "b" * 40},
    {"chainId": 137},
    {"data": conversion.WETH9_WITHDRAW_SELECTOR},
    {"data": conversion.WETH9_WITHDRAW_SELECTOR + "00" * 31},
    {"data": conversion.WETH9_WITHDRAW_SELECTOR
             + AMOUNT.to_bytes(32, "big").hex() + "00"},
    {"data": conversion.WETH9_WITHDRAW_SELECTOR
             + (AMOUNT + 1).to_bytes(32, "big").hex()},
    {"value": "1"},
    {"value": True},
])
def test_withdraw_rejects_wrong_wallet_chain_amount_and_unexpected_value(patch):
    with pytest.raises(ValueError):
        review({**withdraw_tx(), **patch}, operation="WITHDRAW")


def test_wrap_and_unwrap_are_blocked_without_funded_reserve():
    reserve = conversion.MIN_NATIVE_GAS_RESERVE_WEI
    with pytest.raises(ValueError):
        review(deposit_tx(), native=AMOUNT + reserve - 1)
    assert review(deposit_tx(), native=AMOUNT + reserve)[
        "reported_balance_and_gas_reserve_checked"] is True
    with pytest.raises(ValueError):
        review(withdraw_tx(), operation="WITHDRAW",
               native=reserve - 1, weth=ONE_ETH)
    with pytest.raises(ValueError):
        review(withdraw_tx(), operation="WITHDRAW",
               native=ONE_ETH, weth=AMOUNT - 1)
    assert review(withdraw_tx(), operation="WITHDRAW",
                  native=reserve, weth=AMOUNT)[
        "reported_balance_and_gas_reserve_checked"] is True


def test_unknown_operation_and_ambiguous_atomic_units_are_rejected():
    with pytest.raises(ValueError):
        review(deposit_tx(), operation="TRANSFER")
    for amount in ("0", "-1", "1e18", "0x10", "01", "1.2",
                   "", " " + str(AMOUNT), str(10**45), True, 100):
        with pytest.raises(ValueError):
            conversion.inspect_weth9_conversion(
                deposit_tx(), wallet=WALLET, operation="DEPOSIT",
                exact_amount_wei=amount,
                native_balance_wei=str(ONE_ETH),
                weth_balance_wei=str(ONE_ETH),
            )
    with pytest.raises(ValueError):
        conversion.inspect_weth9_conversion(
            deposit_tx(), wallet="0x123", operation="DEPOSIT",
            exact_amount_wei=str(AMOUNT),
            native_balance_wei=str(ONE_ETH),
            weth_balance_wei="0",
        )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("stable", ["USDC", "USDT"])
def test_native_cycle_never_skips_chain_receipts(side, stable):
    result = conversion.native_eth_swap_stages(side=side, stable=stable)
    assert result["stages_are_non_atomic"] is True
    assert result["cannot_assume_quote_equals_actual_fill"] is True
    assert result["must_reconcile_each_stage"] is True
    assert result["cannot_skip_pending_or_uncertain_first_stage"] is True
    assert result["must_check_allowances_and_new_fees_before_second_stage"] is True
    assert result["privy_policy_created"] is False
    assert result["execution_enabled"] is False
    assert result["can_submit"] is False
    names = [x["name"] for x in result["stages"]]
    if side == "BUY":
        assert names == ["SWAP_STABLE_TO_WETH", "UNWRAP_RECEIVED_WETH"]
        assert result["stages"][0]["expected_output_asset"] == "WETH"
        assert result["stages"][1]["expected_output_asset"] == "ETH"
    else:
        assert names == ["WRAP_NATIVE_ETH", "SWAP_CONFIRMED_WETH_TO_STABLE"]
        assert result["stages"][1]["expected_output_asset"] == stable
    assert all(x["requires_confirmed_receipt"] is True for x in result["stages"])
    assert result["stages"][0]["contract"] != result["stages"][1]["contract"]
    assert result["stages"][1]["amount_source"] == (
        "INDEPENDENTLY_VERIFIED_WETH_TOKEN_DELTA"
    )


def test_invalid_asset_and_no_executable_route():
    for bad in ("STABLE", "SWAP", "", None, True):
        with pytest.raises(ValueError):
            conversion.native_eth_swap_stages(side=bad, stable="USDC")
    for bad in ("DAI", "ETH", None, True):
        with pytest.raises(ValueError):
            conversion.native_eth_swap_stages(side="BUY", stable=bad)
    route_paths = {route.path for route in api.app.routes}
    assert "/api/live/eth/weth/convert" not in route_paths
    assert "/api/live/eth/weth/execute" not in route_paths
    assert "/api/live/eth/auto-execute" not in route_paths
