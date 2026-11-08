"""No trades: exhaustive boundary and negative-case tests for the future policy."""
from decimal import Decimal

import pytest

from backend.trading_policy import preview_budget, inspect_future_order


def test_asset_budgets_are_independent():
    btc = preview_budget(asset="BTC", percent=50, available_usd="400")
    eth = preview_budget(asset="ETH", percent=25, available_usd="200")
    assert btc.reserved_usd == Decimal("200")
    assert btc.unallocated_usd == Decimal("200")
    assert eth.reserved_usd == Decimal("50")
    assert eth.unallocated_usd == Decimal("150")


@pytest.mark.parametrize("pct", [-1, 101, 1.2, True, "50", None])
def test_invalid_percentage_fails(pct):
    with pytest.raises(ValueError):
        preview_budget(asset="BTC", percent=pct, available_usd="100")


@pytest.mark.parametrize("value", ["nan", "Infinity", "-10", "1e30", None, True, "invalid"])
def test_invalid_balance_fails(value):
    with pytest.raises(ValueError):
        preview_budget(asset="ETH", percent=50, available_usd=value)


def test_preview_never_authorizes_execution():
    values = dict(asset="ETH", stable="USDC", budget_pct=50, available_usd="100",
                  proposed_notional_usd="20", slippage_bps=50,
                  authenticated=True, wallet_verified=False, route_ready=False,
                  policy_persisted=False, signing_authorized=False, live_enabled=False)
    allowed, reason = inspect_future_order(**values)
    assert allowed is False
    assert reason == "WALLET_NOT_VERIFIED"
    for field, reason in (
        ("wallet_verified", "WALLET_NOT_VERIFIED"),
        ("policy_persisted", "BUDGET_NOT_AUTHORIZED"),
        ("route_ready", "ROUTE_NOT_READY"),
        ("signing_authorized", "SIGNING_NOT_AUTHORIZED"),
        ("live_enabled", "LIVE_DISABLED"),
    ):
        current = {**values, "wallet_verified": True, "policy_persisted": True,
                   "route_ready": True, "signing_authorized": True, "live_enabled": True}
        current[field] = False
        assert inspect_future_order(**current) == (False, reason)


def test_future_order_boundary_rejections():
    valid = dict(asset="BTC", stable="USDT", budget_pct=50, available_usd="100",
                 proposed_notional_usd="51", slippage_bps=20,
                 authenticated=True, wallet_verified=True, route_ready=True,
                 policy_persisted=True, signing_authorized=True, live_enabled=True)
    assert inspect_future_order(**valid) == (False, "BUDGET_EXCEEDED")
    assert inspect_future_order(**{**valid, "slippage_bps": 101}) == (False, "SLIPPAGE_LIMIT")
    assert inspect_future_order(**{**valid, "asset": "SOL"}) == (False, "UNSUPPORTED_PAIR")
    assert inspect_future_order(**{**valid, "stable": "DAI"}) == (False, "UNSUPPORTED_PAIR")
    assert inspect_future_order(**{**valid, "authenticated": False}) == (False, "AUTH_REQUIRED")
    assert inspect_future_order(**{**valid, "proposed_notional_usd": "0"}) == (False, "BUDGET_EXCEEDED")
