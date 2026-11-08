"""Regression tests for HWM losses, per-asset isolation and no duplicate public write."""
from decimal import Decimal
import pytest

from backend.fees import RATE, assess_realized_gain, decimal_amount
from backend.storage import SCHEMA, get_account_summary, record_trusted_realized_close


def test_fee_is_fixed_five_percent():
    assert RATE == Decimal("0.05")


def test_loss_must_be_recouped_before_five_percent_fee():
    first=assess_realized_gain(0,0,-100)
    assert first.cumulative_usd==Decimal("-100")
    assert first.high_water_mark_usd==0
    assert first.fee_assessed_usd==0
    second=assess_realized_gain(first.cumulative_usd,first.high_water_mark_usd,150)
    assert second.cumulative_usd==50
    assert second.new_profit_above_hwm_usd==50
    assert second.fee_assessed_usd==Decimal("2.500000")


def test_repeat_losses_then_recovery_are_not_double_charged():
    state=assess_realized_gain(0,0,100)
    assert state.fee_assessed_usd==5
    state=assess_realized_gain(state.cumulative_usd,state.high_water_mark_usd,-35)
    assert state.fee_assessed_usd==0
    state=assess_realized_gain(state.cumulative_usd,state.high_water_mark_usd,35)
    assert state.fee_assessed_usd==0
    state=assess_realized_gain(state.cumulative_usd,state.high_water_mark_usd,40)
    assert state.fee_assessed_usd==2
    assert state.high_water_mark_usd==140


def test_cash_flows_never_increase_hwm_automatically():
    # Only realized net-PnL deltas are accepted. Wallet cashflows have no
    # accounting entry point in the fee module.
    state=assess_realized_gain(0,0,0)
    assert state.high_water_mark_usd==0 and state.fee_assessed_usd==0


def test_strict_amount_validation():
    for value in (None,True,"nan","infinity","not-money","1e30"):
        with pytest.raises(ValueError):
            decimal_amount(value)


def test_missing_db_config_returns_503_without_privy_id_leak(monkeypatch):
    monkeypatch.delenv("DATABASE_URL",raising=False)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        get_account_summary("did:privy:user-test")
    assert exc.value.status_code==503
    assert "user-test" not in exc.value.detail


def test_public_api_does_not_offer_fee_write_route():
    from backend.app import app
    assert not any(route.path.startswith("/api/fees/record") for route in app.routes)
    assert not any("record_trusted_realized_close" in str(route.endpoint) for route in app.routes)


def test_schema_per_asset_and_idempotency_key():
    combined="\n".join(SCHEMA)
    assert "PRIMARY KEY (privy_did,asset,execution_id)" in combined
    assert "CHECK (asset IN ('BTC','ETH'))" in combined
    assert "REFERENCES mininex_accounts" in combined
    assert "FOR UPDATE" in __import__("inspect").getsource(record_trusted_realized_close)
