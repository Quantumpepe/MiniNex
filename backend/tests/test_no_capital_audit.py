"""No-capital MiniNex audit: deterministic policy and route checks only.

Never calls a live provider, reads user keys, broadcasts, writes accounts,
authorizes a trade, or modifies the trading engine.
"""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend import live_asset_registry as registry
from backend import manual_swap_routes as routes
from backend.fees import RATE, assess_realized_gain


@pytest.mark.parametrize("asset", registry.ASSET_ORDER)
def test_each_coin_has_independent_owner_scoped_execution_capabilities(asset):
    readiness = registry.get_global_live_readiness()
    row = readiness["assets"][asset]
    assert row["symbol"] == asset
    assert row["live_execution_ready"] is None
    assert row["allow_user_trading"] is None
    assert row["checks_evaluated_at"]=="OWNER_AUTHORIZED_SEND_AND_RECEIPT"
    assert all(value is None for value in row["safety_gates"].values())
    assert readiness["global_activation_blocked"] is False
    assert readiness["global_trading_enabled"] is None
    assert registry.require_live_engine(asset) is None


@pytest.mark.parametrize("asset", registry.ASSET_ORDER)
@pytest.mark.parametrize("stable", ("USDC", "USDT"))
def test_coin_to_stable_route_is_preview_only(asset, stable):
    route = routes.build_manual_swap_route(asset, stable)
    assert route["source"]["symbol"] == asset
    assert route["destination"]["symbol"] == stable
    assert route["quote_preview_implemented"] is True
    for key in (
        "execution_ready", "signing_authorized", "transaction_built",
        "can_submit", "network_fee_verified", "guaranteed_minimum_received",
    ):
        assert route[key] is False, (asset, stable, key)
    assert route["required_checks"]
    if asset in ("BTC", "SOL", "XRP", "TRX", "POL", "BNB", "AVAX", "DOGE", "ADA"):
        assert route["route_class"] == "CROSS_CHAIN"


@pytest.mark.parametrize("asset", registry.ASSET_ORDER)
def test_swap_reversals_are_preview_only(asset):
    quote = routes.build_manual_swap_route("USDC", asset)
    assert quote["execution_ready"] is False
    assert quote["can_submit"] is False
    assert quote["signing_authorized"] is False


def test_hwm_fee_does_not_charge_recovered_loss_twice():
    assert RATE == Decimal("0.05")
    loss = assess_realized_gain(0, 0, -100)
    assert loss.fee_assessed_usd == 0
    recovery = assess_realized_gain(
        loss.cumulative_usd, loss.high_water_mark_usd, 100
    )
    assert recovery.fee_assessed_usd == 0
    profit = assess_realized_gain(
        recovery.cumulative_usd, recovery.high_water_mark_usd, 20
    )
    assert profit.fee_assessed_usd == Decimal("1.000000")


def test_no_wallet_funds_needed_for_audit():
    assert len(registry.ASSET_ORDER) == 12
    assert len(registry.REGISTRY) == 12
    assert registry.get_global_live_readiness()["one_click_activation_enabled"] is None
