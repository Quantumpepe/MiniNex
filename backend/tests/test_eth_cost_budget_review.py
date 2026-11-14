"""Offline regression checks for dormant ETH cost and capital safety review."""
import pytest
from backend.eth_cost_budget_review import review_eth_cost_budget


def evidence(**changes):
    data = dict(
        side="BUY", stable="USDC", input_atomic=100_000_000,
        minimum_output_atomic=50_000_000_000_000_000,
        gas_limit=100_000, max_fee_per_gas_wei=2_000_000_000,
        eth_usd="2000", eth_reference_age_seconds=5,
        stable_usd="1.00", stable_reference_age_seconds=7,
        input_balance_atomic=200_000_000,
        eth_balance_wei=100_000_000_000_000_000,
        provider_fee_stable_atomic=0,
        live_limits={"asset": "ETH", "revision": 1, "trading_paused": False,
                     "planned_capital_usd": "200", "max_order_usd": "110",
                     "max_daily_turnover_usd": "200"},
        pending_capital_usd="0", spent_24h_usd="0",
    )
    data.update(changes)
    return data


def test_precise_costs_and_no_execution_authority():
    result = review_eth_cost_budget(**evidence())
    assert result["max_possible_gas_eth"] == "0.0002"
    assert result["max_possible_gas_usd_indicative"] == "0.4000"
    assert result["max_total_input_plus_costs_usd_indicative"] == "100.4000"
    assert result["within_declared_cost_limits"] is True
    assert all(result[k] is False for k in (
        "can_reserve", "can_sign", "can_submit", "live_trading_enabled",
        "inputs_independently_verified_here", "capital_row_lock_verified_here"))


def test_paused_account_never_gets_trading_green_light():
    args = evidence()
    args["live_limits"] = {**args["live_limits"], "trading_paused": True}
    result = review_eth_cost_budget(**args)
    assert "ETH_STOP_ACTIVE" in result["blockers"]
    assert not result["within_declared_cost_limits"]


@pytest.mark.parametrize("changes", [
    {"stable": "BTC"}, {"side": "SHORT"},
    {"gas_limit": 2_000_001},
    {"max_fee_per_gas_wei": 300_000_000_001},
    {"gas_limit": 1_000_000, "max_fee_per_gas_wei": 5_000_000_000},
    {"eth_usd": "NaN"}, {"eth_usd": "-1"},
    {"stable_usd": "0.90"}, {"stable_usd": "1.04"},
    {"eth_reference_age_seconds": 31},
    {"stable_reference_age_seconds": 31},
    {"input_atomic": True}, {"gas_limit": 0},
    {"input_balance_atomic": "200000000"},
    {"live_limits": {"asset": "BTC", "revision": 1, "trading_paused": False}},
    {"live_limits": {"asset": "ETH", "revision": 1, "trading_paused": "false"}},
])
def test_untrusted_malformed_or_cross_asset_inputs_rejected(changes):
    with pytest.raises(ValueError):
        review_eth_cost_budget(**evidence(**changes))


@pytest.mark.parametrize("changes,code", [
    ({"input_balance_atomic": 99_999_999}, "INPUT_BALANCE_TOO_LOW"),
    ({"eth_balance_wei": 5_100_000_000_000_000}, "ETH_GAS_RESERVE_NOT_COVERED"),
    ({"pending_capital_usd": "150"}, "RESERVED_CAPITAL_OVER_LIMIT"),
    ({"spent_24h_usd": "150"}, "ROLLING_TURNOVER_OVER_LIMIT"),
    ({"minimum_output_atomic": 48_000_000_000_000_000}, "MINIMUM_OUTPUT_TOO_LOW"),
    ({"provider_fee_stable_atomic": 10_000_000}, "ORDER_PLUS_FEES_OVER_LIMIT"),
])
def test_distinct_budget_or_quote_blockers(changes, code):
    result = review_eth_cost_budget(**evidence(**changes))
    assert code in result["blockers"]
    assert not result["within_declared_cost_limits"]
    assert result["can_submit"] is False


def test_native_sale_spend_and_gas_reserve():
    args = evidence(side="SELL", input_atomic=50_000_000_000_000_000,
                    minimum_output_atomic=99_000_000,
                    input_balance_atomic=50_000_000_000_000_000,
                    eth_balance_wei=55_200_000_000_000_000)
    result = review_eth_cost_budget(**args)
    assert "ETH_GAS_RESERVE_NOT_COVERED" not in result["blockers"]
    assert result["can_submit"] is False
    args["eth_balance_wei"] = 55_100_000_000_000_000
    assert "ETH_GAS_RESERVE_NOT_COVERED" in review_eth_cost_budget(**args)["blockers"]


def test_stable_fee_requires_separate_balance():
    result = review_eth_cost_budget(**evidence(
        provider_fee_stable_atomic=1_000_000,
        input_balance_atomic=100_000_000))
    assert "INPUT_BALANCE_TOO_LOW" in result["blockers"]
    assert result["explicit_provider_fee_usd"] == "1.00"
