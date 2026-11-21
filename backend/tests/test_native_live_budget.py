"""Native owner budgets must use REAL holdings, unclosed funds and gas."""
from decimal import Decimal

import pytest

from backend.native_live_budget import (
    exact_owner_native_buy_amount,native_pending_buy_stable_total,
)

NETWORKS={
    "BTC":"bitcoin-mainnet","SOL":"solana-mainnet",
    "XRP":"xrpl-mainnet","TRX":"tron-mainnet",
    "DOGE":"dogecoin-mainnet",
}

def amount(asset="BTC",stable="USDC",budget=500,owned="0",liquid="1000",
           price="100",stable_price="1",pending=0,fees=0):
    return exact_owner_native_buy_amount(
        asset=asset,stable=stable,budget_usd=budget,
        native_wallet_evidence={
            "asset":asset,"network":NETWORKS.get(asset),
            "status":"verified","balance":owned},
        stable_wallet_evidence={
            "network":"ethereum-mainnet","status":"verified",
            "balance":liquid},
        market_price_usd=price,stable_price_usd=stable_price,
        pending_buy_stable=pending,observed_source_fees_usd=fees,
    )


@pytest.mark.parametrize("asset,stable",[
    ("BTC","USDC"),("BTC","USDT"),("SOL","USDT"),("XRP","USDC"),
    ("TRX","USDT"),("DOGE","USDC"),("DOGE","USDT"),
])
def test_native_each_coin_honors_actual_owner_budget_not_platform_cap(asset,stable):
    assert amount(asset,stable,budget=500,liquid="800")=="500"
    assert amount(asset,stable,budget=7000,liquid="800")=="800"
    assert amount(asset,stable,budget=500,owned="3",price="100")=="200"
    assert amount(asset,stable,budget=500,owned="10",price="100") is None
    assert amount(asset,stable,budget=500,pending="300")=="200"
    assert amount(asset,stable,budget=500,fees="3.25")=="496.75"


def test_owner_source_spends_only_actual_stable_and_respects_depeg():
    assert amount(liquid="1.2345679")=="1.234567"
    assert amount(stable_price="1.01",budget=100,liquid="300")=="99.0099"
    assert amount(stable_price="0.94") is None
    assert amount(stable_price="1.06") is None


@pytest.mark.parametrize("bad",[
    {"owned":None},{"owned":"NaN"},{"owned":"-1"},
    {"liquid":"Infinity"},{"liquid":"-0.01"},{"price":0},
    {"pending":True},{"pending":"-0.1"},{"fees":"not-a-number"},
    {"stable_price":"NaN"},
])
def test_native_unverified_capital_is_never_paper_buy(bad):
    assert amount(**bad) is None


def test_cross_coin_and_unverified_receipts_never_free_other_coin_budget():
    rows=[{"asset":"BTC","side":"BUY","stable":"USDC",
           "status":"SUBMITTED","input_amount":"90.5"},
          {"asset":"BTC","side":"BUY","stable":"USDC",
           "status":"SOURCE_FINALIZED","input_amount":"9.5"},
          {"asset":"BTC","side":"BUY","stable":"USDC",
           "status":"DESTINATION_FINALIZED","input_amount":"40"}]
    assert native_pending_buy_stable_total(rows,asset="BTC",stable="USDC")==Decimal(100)
    assert amount(pending=native_pending_buy_stable_total(
        rows,asset="BTC",stable="USDC"))=="400"
    with pytest.raises(ValueError):
        native_pending_buy_stable_total(
            rows+[{"asset":"SOL","side":"BUY","stable":"USDC",
                   "status":"SUBMITTED","input_amount":"50"}],
            asset="BTC",stable="USDC")


def test_no_fake_refund_from_unrecognized_journal_state():
    with pytest.raises(ValueError):
        native_pending_buy_stable_total([
            {"asset":"DOGE","side":"BUY","stable":"USDT",
             "status":"CREDITED_WITHOUT_CHAIN_RECEIPT",
             "input_amount":"25"}],asset="DOGE",stable="USDT")


def test_trx_rejects_usdc_until_actual_native_route_supported():
    assert amount("TRX","USDC") is None


def test_native_budget_accepts_exact_decimal_owner_selection_without_rounding_up():
    assert amount(budget=500,liquid="400")=="400"
    value=exact_owner_native_buy_amount(
        asset="BTC",stable="USDC",budget_usd=Decimal("2.50000050"),
        native_wallet_evidence={"asset":"BTC","network":"bitcoin-mainnet",
                                "status":"verified","balance":"0"},
        stable_wallet_evidence={"status":"verified","network":"ethereum-mainnet",
                                "balance":"100"},
        market_price_usd="100",stable_price_usd="1")
    assert value=="2.5"
