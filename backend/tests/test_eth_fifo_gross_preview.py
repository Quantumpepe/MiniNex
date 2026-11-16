"""ETH FIFO is a read-only gross stablecoin preview, not taxable/fee net PnL."""
from decimal import Decimal

import pytest
from fastapi import HTTPException
from backend.eth_fifo_gross_preview import eth_fifo_gross_preview
from backend import eth_fifo_gross_preview as mod


def row(side, stable, spent, received, key):
    return {
        "intent_key":key, "stable":stable, "side":side,
        "spent_asset":stable if side=="BUY" else "WETH",
        "received_asset":"WETH" if side=="BUY" else stable,
        "spent_amount":str(spent), "received_amount":str(received),
        "onchain_finalized":True,
    }


def history(*rows, truncated=False):
    return {
        "source":"CONFIRMED_AUTO_ROUTER02_SWAPS",
        "asset":"ETH", "includes_paper":False,
        "history_truncated":truncated, "swaps":list(rows),
    }


def test_fifo_partial_sell_and_recovery_are_matched_separately():
    h=history(
        row("SELL","USDC","0.5","60","sell2"),
        row("SELL","USDC","0.5","55","sell1"),
        row("BUY","USDC","100","1","buy1"),
    )
    result=eth_fifo_gross_preview(h)
    assert result["all_sell_cost_basis_matched_within_sample"] is True
    assert Decimal(result["gross_spread_by_stable"]["USDC"])==15
    assert result["gross_spread_by_stable"]["USDT"]=="0"
    assert result["closed_trades"][0]["allocated_buy_cost_stable"]=="50.0"
    assert result["five_percent_fee_assessed"] is False
    assert result["verified_net_pnl_usd"] is None


def test_multi_buy_fifo_cost_basis_with_partial_lot():
    h=history(
        row("SELL","USDT","1.5","220","sell"),
        row("BUY","USDT","140","1","buy2"),
        row("BUY","USDT","100","1","buy1"),
    )
    result=eth_fifo_gross_preview(h)
    assert Decimal(result["gross_spread_by_stable"]["USDT"])==50


def test_different_stable_never_treated_as_same_cost_basis():
    h=history(
        row("SELL","USDT","1","120","sell"),
        row("BUY","USDC","100","1","buy"),
    )
    result=eth_fifo_gross_preview(h)
    assert result["unmatched_sell_count"]==1
    assert result["gross_spread_by_stable"]["USDT"] is None
    assert result["verified_net_pnl_usd"] is None


def test_external_inventory_cannot_be_assigned_a_fake_cost_basis():
    h=history(row("SELL","USDC","2","200","sell"),
              row("BUY","USDC","100","1","buy"))
    result=eth_fifo_gross_preview(h)
    assert result["unmatched_sell_count"]==1
    assert result["closed_trades"][0]["gross_spread_stable"] is None
    assert result["gross_spread_by_stable"]["USDC"] is None


def test_truncated_history_never_advertises_complete_spread():
    h=history(row("SELL","USDC","1","110","sell"),
              row("BUY","USDC","100","1","buy"),truncated=True)
    result=eth_fifo_gross_preview(h)
    assert result["historical_sample_truncated"] is True
    assert result["gross_spread_by_stable"]["USDC"] is None


@pytest.mark.parametrize("invalid",["NaN","-1","0","Infinity","1e27",None])
def test_bad_amounts_fail_closed(invalid):
    h=history(row("BUY","USDC",invalid,"1","buy"))
    with pytest.raises(ValueError):
        eth_fifo_gross_preview(h)


def test_repeat_execution_id_is_not_double_accounted():
    h=history(row("BUY","USDC","20","0.01","buy"),
              row("BUY","USDC","20","0.01","buy"))
    with pytest.raises(ValueError,match="Ambiguous"):
        eth_fifo_gross_preview(h)


def test_read_only_endpoint_requires_privy_authentication():
    import inspect
    from backend.app import app
    route=next(r for r in app.routes if r.path=="/api/live/eth/gross-spread-preview")
    assert "verify_privy_bearer" in inspect.getsource(route.endpoint)


def test_reader_rejects_untrusted_history(monkeypatch):
    monkeypatch.setattr(
        __import__("backend.real_eth_trade_legs",fromlist=["get_confirmed_eth_swaps"]),
        "get_confirmed_eth_swaps",
        lambda did: {"source":"PAPER","swaps":[]},
    )
    with pytest.raises(HTTPException) as error:
        mod.read_eth_fifo_gross_preview("did:privy:verified")
    assert error.value.status_code==503


def test_recorded_swap_and_verified_native_conversion_gas_are_exact():
    buy=row("BUY","USDC","100","1","buy")
    buy.update({
        "network_fee_eth":"0.00015",
        "native_conversion_status":"CONFIRMED_VERIFIED",
        "native_conversion_network_fee_eth":"0.00005",
    })
    sell=row("SELL","USDC","1","125","sell")
    sell["network_fee_eth"]="0.00020"
    result=eth_fifo_gross_preview(history(sell,buy))
    assert result["verified_recorded_network_fee_eth"]=="0.00040"
    assert result["native_buy_conversion_unresolved_count"]==0
    assert result["historical_network_fee_usd"] is None
    assert result["recorded_gas_is_exhaustive"] is False
    assert result["five_percent_fee_assessed"] is False


def test_unfinished_native_conversion_not_invented_as_completed_gas():
    buy=row("BUY","USDC","100","1","buy")
    buy.update({
        "network_fee_eth":"0.00015",
        "native_conversion_status":"SUBMITTED",
        "native_conversion_network_fee_eth":None,
    })
    result=eth_fifo_gross_preview(history(buy))
    assert result["verified_recorded_network_fee_eth"]=="0.00015"
    assert result["native_buy_conversion_unresolved_count"]==1
    assert result["recorded_gas_is_exhaustive"] is False


def test_missing_verified_gas_never_becomes_zero():
    buy=row("BUY","USDC","100","1","buy")
    assert eth_fifo_gross_preview(history(buy))[
        "verified_recorded_network_fee_eth"] is None


@pytest.mark.parametrize("bad",["NaN","-0.00001","Infinity","1e30","0.0000000000000000001"])
def test_invalid_recorded_gas_is_rejected(bad):
    buy=row("BUY","USDC","100","1","buy")
    buy["network_fee_eth"]=bad
    with pytest.raises(ValueError):
        eth_fifo_gross_preview(history(buy))
