"""SwapRouter02 policy feasibility: test only, never sign or broadcast."""
from __future__ import annotations

import copy

import pytest

from backend import eth_swaprouter02_preflight as scope
from backend.live_eth_quotes import STABLES


WALLET = "0x" + "a" * 40


def word(value):
    return value.to_bytes(32, "big")


def calldata(*, token_in, token_out, recipient=WALLET, fee=3000,
             amount_in=100_000_000, minimum_out=100_000_000_000_000_000,
             sqrt_limit=0):
    packed = b"".join([
        word(int(token_in, 16)), word(int(token_out, 16)),
        word(fee), word(int(recipient, 16)),
        word(amount_in), word(minimum_out), word(sqrt_limit),
    ])
    return scope.EXACT_INPUT_SINGLE_SELECTOR + packed.hex()


def transaction(*, stable="USDC", side="BUY", wallet=WALLET, **changes):
    if side == "BUY":
        args = {
            "token_in": STABLES[stable],
            "token_out": scope.WETH_MAINNET,
            "amount_in": 100_000_000,
            "minimum_out": 100_000_000_000_000_000,
        }
    else:
        args = {
            "token_in": scope.WETH_MAINNET,
            "token_out": STABLES[stable],
            "amount_in": 100_000_000_000_000_000,
            "minimum_out": 100_000_000,
        }
    return {
        "from": wallet,
        "to": scope.SWAPROUTER02_MAINNET,
        "chainId": 1,
        "value": "0",
        "data": calldata(**args),
        **changes,
    }


def check(tx, *, stable="USDC", side="BUY", wallet=WALLET):
    return scope.inspect_swaprouter02_exact_input(
        tx, wallet=wallet, side=side, stable=stable,
        input_atomic=("100000000" if side == "BUY"
                      else "100000000000000000"),
        minimum_output_atomic=("100000000000000000" if side == "BUY"
                               else "100000000"),
    )


@pytest.mark.parametrize("stable", ["USDC", "USDT"])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_only_allowed_weth_stable_directions_are_readable_and_never_executable(
    stable, side,
):
    result = check(transaction(stable=stable, side=side),
                   side=side, stable=stable)
    assert result["token_pair_verified"] is True
    assert result["recipient_verified"] is True
    assert result["selector_checked"] is True
    assert result["quote_minimum_verified"] is True
    assert result["route_settlement"] == ("WETH" if side == "BUY" else stable)
    assert result["wallet_native_eth_settlement_complete"] is False
    assert result["safe_to_sign"] is False
    assert result["can_submit"] is False
    assert result["execution_enabled"] is False
    assert "data" not in result
    assert "private_key" not in result


@pytest.mark.parametrize("mutation", [
    {"from": "0x" + "b" * 40},
    {"from": None},
    {"to": "0x" + "b" * 40},
    {"to": None},
    {"chainId": 10},
    {"chainId": True},
    {"value": "1"},
    {"value": False},
    {"value": "0x00"},
    {"data": "0x3593564c" + "00" * (7 * 32)},
    {"data": "0x04e45aaf"},
    {"data": "0x04e45aaf" + "00" * (8 * 32)},
])
def test_rejects_wrong_network_sender_router_native_value_or_abi(mutation):
    with pytest.raises(ValueError):
        check(transaction(**mutation))


def test_rejects_token_redirection_wrong_amount_minimum_fee_or_hidden_router_call():
    normal = transaction()["data"]
    wrong = (
        calldata(token_in=STABLES["USDT"], token_out=scope.WETH_MAINNET),
        calldata(token_in=STABLES["USDC"], token_out=STABLES["USDT"]),
        calldata(token_in=STABLES["USDC"], token_out=scope.WETH_MAINNET,
                 recipient="0x" + "b" * 40),
        calldata(token_in=STABLES["USDC"], token_out=scope.WETH_MAINNET,
                 fee=200),
        calldata(token_in=STABLES["USDC"], token_out=scope.WETH_MAINNET,
                 amount_in=99_999_999),
        calldata(token_in=STABLES["USDC"], token_out=scope.WETH_MAINNET,
                 minimum_out=1),
        calldata(token_in=STABLES["USDC"], token_out=scope.WETH_MAINNET,
                 sqrt_limit=1234),
        normal + "00" * 32,
    )
    for encoded in wrong:
        with pytest.raises(ValueError):
            check(transaction(data=encoded))
    # The special router "use entire contract balance" uint256 max may
    # never bypass exact quote-bound spending.
    encoded = calldata(
        token_in=STABLES["USDC"], token_out=scope.WETH_MAINNET,
        amount_in=2**256 - 1,
    )
    with pytest.raises(ValueError):
        check(transaction(data=encoded))


def test_invalid_wallet_token_unit_inputs_fail_closed():
    for invalid in (
        {"wallet": "0x123"},
        {"wallet": None},
        {"stable": []},
        {"stable": "DAI"},
        {"side": "BRIDGE"},
    ):
        with pytest.raises(ValueError):
            scope.inspect_swaprouter02_exact_input(
                transaction(), wallet=WALLET, stable="USDC", side="BUY",
                input_atomic="100000000",
                minimum_output_atomic="100000000000000000",
                **invalid,
            )


def test_policy_scope_is_informational_not_privy_authorization():
    report = scope.swaprouter02_policy_scope()
    assert report["router"] == scope.SWAPROUTER02_MAINNET
    assert report["function_selector"] == "0x04e45aaf"
    assert report["calldata_function"] == "exactInputSingle"
    assert report["native_eth_trade_cycle_complete"] is False
    assert report["privy_policy_created"] is False
    assert report["privy_permission_granted"] is False
    assert report["can_submit"] is False
    assert report["execution_enabled"] is False
    abi = scope.EXACT_INPUT_SINGLE_ABI[0]
    assert abi["name"] == "exactInputSingle"
    assert [arg["name"] for arg in abi["inputs"][0]["components"]] == [
        "tokenIn", "tokenOut", "fee", "recipient", "amountIn",
        "amountOutMinimum", "sqrtPriceLimitX96",
    ]
    assert "deadline" not in [arg["name"] for arg in abi["inputs"][0]["components"]]
