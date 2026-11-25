import pytest
from backend.strategist_profit_allocation import propose_profit_allocation


def test_default_all_profit_saved():
    r=propose_profit_allocation(asset="ETH",stable="USDC",
        gross_reserved_atomic=310_000_000,net_profit_after_fees_atomic=300_000_000)
    assert r["suggested_saved_net_atomic"]=="300000000"
    assert r["suggested_reinvest_atomic"]=="0"
    assert not r["can_release"]


def test_partial_reinvestment_suggestion_requires_net_and_available_budget():
    args=dict(asset="POL",stable="USDT",gross_reserved_atomic=310_000_000,
              requested_reinvest_atomic=100_000_000,free_budget_atomic=100_000_000)
    r=propose_profit_allocation(**args,net_profit_after_fees_atomic=300_000_000)
    assert r["suggested_saved_net_atomic"]=="200000000"
    assert r["suggested_reinvest_atomic"]=="100000000"
    assert not r["can_release"]
    pending=propose_profit_allocation(**args)
    assert pending["cost_reconciliation_pending"]
    assert pending["suggested_reinvest_atomic"]=="0"
    args["free_budget_atomic"]=50_000_000
    assert propose_profit_allocation(**args,net_profit_after_fees_atomic=300_000_000)["suggested_reinvest_atomic"]=="50000000"


def test_proposal_does_not_make_up_cash_or_accept_negative_values():
    args=dict(asset="BTC",stable="USDC",gross_reserved_atomic=10,
              net_profit_after_fees_atomic=50,requested_reinvest_atomic=100,free_budget_atomic=100)
    assert propose_profit_allocation(**args)["suggested_reinvest_atomic"]=="10"
    with pytest.raises(ValueError):
        propose_profit_allocation(**{**args,"requested_reinvest_atomic":-1})
