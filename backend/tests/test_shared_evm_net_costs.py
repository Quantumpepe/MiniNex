"""Exact shared six-EVM net settlement math rejects incomplete gas and FX."""
from decimal import Decimal

import pytest

from backend.shared_evm_swap_receipts import _pair
from backend.shared_evm_net_costs import calculate_verified_evm_net_costs


def evidence(asset,stable=None):
    stable=stable or ("USDT" if asset=="BNB" else "USDC")
    pair=_pair(asset,stable,"SELL")
    return {
        "mode":"SERVER_RECONCILED_EVM_NET_COST_EVIDENCE",
        "asset":asset,"stable":stable,"side":"SELL",
        "chain_id":pair["chain_id"],"network":pair["network"],
        "intent_key":"sig_abcdef01234567890123456789",
        "tx_hash":"0x"+"ab"*32,"wallet_address":"0x"+"cd"*20,
        "finalized_onchain_sell_proved":True,
        "complete_fifo_lot_provenance":True,
        "complete_historical_price_provenance":True,
        "stablecoin_usd_rates_at_each_fill_verified":True,
        "gas_native_usd_rates_at_each_fill_verified":True,
        "all_native_conversion_costs_reconciled":True,
        "external_inventory_reconciled":True,
        "history_not_truncated":True,
        "execution_fees_reconciled":True,
        "contains_paper":False,
        "confirmed_sell_proceeds_usd":"120.5",
        "fifo_acquisition_cost_usd":"100.0",
        "allocated_buy_network_gas_usd":"0.2",
        "sell_network_gas_usd":"0.3",
        "native_conversion_gas_usd":"0.4",
        "other_confirmed_execution_cost_usd":"0.1",
    }


@pytest.mark.parametrize("asset",["ETH","BNB","POL","AVAX","LINK","UNI"])
def test_all_six_evm_coins_share_exact_net_settlement_math(asset):
    p=evidence(asset)
    r=calculate_verified_evm_net_costs(p)
    assert Decimal(r["total_execution_cost_usd"])==Decimal("101")
    assert Decimal(r["realized_net_usd"])==Decimal("19.5")
    assert r["fee_assessed"] is False
    assert r["fee_paid_onchain"] is False
    assert r["hwm_updated"] is False
    assert r["wallet_signed"] is False


@pytest.mark.parametrize("profit,cost,expected",[
    ("101","100","0.0"),("110","100","9.0"),
    ("90","100","-11.0"),("100.50","100","-0.5"),
])
def test_losses_and_breakeven_not_invented_as_profit(profit,cost,expected):
    e=evidence("LINK")
    e["confirmed_sell_proceeds_usd"]=profit
    e["fifo_acquisition_cost_usd"]=cost
    r=calculate_verified_evm_net_costs(e)
    assert Decimal(r["realized_net_usd"])==Decimal(expected)
    assert r["fee_assessed"] is False


@pytest.mark.parametrize("flag",[
    "finalized_onchain_sell_proved",
    "complete_fifo_lot_provenance",
    "complete_historical_price_provenance",
    "stablecoin_usd_rates_at_each_fill_verified",
    "gas_native_usd_rates_at_each_fill_verified",
    "all_native_conversion_costs_reconciled",
    "external_inventory_reconciled",
    "history_not_truncated",
    "execution_fees_reconciled",
])
def test_missing_historical_price_or_conversion_proof_blocks_net(flag):
    e=evidence("AVAX")
    e[flag]=False
    with pytest.raises(ValueError):
        calculate_verified_evm_net_costs(e)


@pytest.mark.parametrize("field,bad",[
    ("confirmed_sell_proceeds_usd","NaN"),
    ("confirmed_sell_proceeds_usd","0"),
    ("fifo_acquisition_cost_usd","-1"),
    ("allocated_buy_network_gas_usd",None),
    ("sell_network_gas_usd","Infinity"),
    ("native_conversion_gas_usd","1e10"),
    ("other_confirmed_execution_cost_usd","0.0000000000001"),
])
def test_incomplete_or_untrusted_exact_costs_are_rejected(field,bad):
    e=evidence("POL")
    e[field]=bad
    with pytest.raises(ValueError):
        calculate_verified_evm_net_costs(e)


@pytest.mark.parametrize("key,bad",[
    ("chain_id",56),("asset","DOGE"),("side","BUY"),
    ("network","ethereum-mainnet"),("contains_paper",True),
    ("wallet_address","0x"+"aa"*19),
    ("tx_hash","0x"+"ab"*31),
    ("intent_key","bad"),
])
def test_invalid_order_identity_does_not_make_net_fee_evidence(key,bad):
    e=evidence("AVAX")
    e[key]=bad
    with pytest.raises(ValueError):
        calculate_verified_evm_net_costs(e)


def test_net_engine_has_no_api_writer_or_privy_signer():
    import inspect
    from backend import shared_evm_net_costs as mod
    from backend.app import app
    code=inspect.getsource(mod)
    assert "psycopg" not in code
    assert "sign_privy" not in code
    assert "httpx" not in code
    assert all("evm-net-costs" not in route.path for route in app.routes)
