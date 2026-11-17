"""Pure historical FIFO/USD reconciliation: fees must remain disabled."""
from copy import deepcopy
from decimal import Decimal

import pytest

from backend.shared_evm_fifo_net import reconcile_verified_evm_fifo_net
from backend.shared_evm_swap_receipts import _pair

WALLET = "0x" + "ab"*20
TX1 = "0x" + "11"*32
TX2 = "0x" + "22"*32
TX3 = "0x" + "33"*32


def price(asset, timestamp, amount):
    return {
        "mode": "HISTORICAL_EXECUTION_USD_REFERENCE_ONLY",
        "provider": "COINGECKO_MARKET_CHART_RANGE",
        "asset": asset, "execution_block_unix": timestamp,
        "rate_observed_unix": timestamp + 20, "usd_price": str(amount),
    }


def trade(asset, side, stable, block, tx, source, destination,
          stable_price="1", native_price="2000", network_fee="0.001"):
    pair = _pair(asset, stable, side)
    timestamp = 1000000 + block*10
    row = {
        "mode": "INDEPENDENT_FINALIZED_EVM_EXECUTION",
        "asset": asset, "chain_id": pair["chain_id"],
        "network": pair["network"], "wallet_address": WALLET,
        "intent_key": "sig_" + tx[2:40], "tx_hash": tx,
        "onchain_finalized": True,
        "side": side, "stable": stable,
        "source_amount": str(source), "destination_amount": str(destination),
        "network_fee_native": str(network_fee),
        "block_number": block, "transaction_index": 0,
        "block_unix": timestamp,
        "stable_usd_proof": price(stable, timestamp, stable_price),
        "native_usd_proof": price(
            asset if asset in ("BNB", "POL", "AVAX") else "ETH",
            timestamp, native_price),
    }
    if asset in ("BNB", "POL", "AVAX"):
        native_block = block + 1 if side == "BUY" else block - 1
        row["native_conversion"] = {
            "finalized": True,
            "operation": "WITHDRAW" if side == "BUY" else "DEPOSIT",
            "tx_hash": "0x" + ("aa" if side == "BUY" else "bb")*32,
            "block_number": native_block, "block_unix": timestamp + (
                10 if side == "BUY" else -10),
            "quantity_native": str(destination if side == "BUY" else source),
            "network_fee_native": "0.002",
            "native_usd_proof": price(asset, timestamp + (
                10 if side == "BUY" else -10), native_price),
        }
    return row


def ledger(asset, fills):
    return {
        "mode": "SERVER_VERIFIED_EVM_CUSTODY_HISTORY",
        "asset": asset, "wallet_address": WALLET,
        "wallet_ownership_verified": True,
        "history_not_truncated": True, "contains_paper": False,
        "asset_transfer_audit": {
            "mode": "INDEPENDENT_ONCHAIN_ASSET_TRANSFER_AUDIT",
            "zero_initial_asset_balance_verified": True,
            "all_asset_movements_match_verified_orders": True,
            "external_inventory_reconciled": True,
            "start_block": 1, "end_block": 1000,
        },
        "fills": fills,
    }


@pytest.mark.parametrize("asset,stable", [
    ("BNB", "USDT"), ("POL", "USDC"), ("AVAX", "USDC"),
    ("LINK", "USDC"), ("UNI", "USDT"),
])
def test_real_buy_sell_fifo_net_includes_historical_gas(asset, stable):
    records = [
        trade(asset,"BUY",stable,100,TX1,"100","2",native_price="100"),
        trade(asset,"SELL",stable,200,TX2,"1","75",native_price="200"),
    ]
    result = reconcile_verified_evm_fifo_net(ledger(asset,records))
    assert result["fee_assessed"] is False
    assert result["automatic_fee_activated"] is False
    assert result["remaining_fifo_units"] == "1"
    close = result["closed_trades"][0]
    assert close["net_pnl_is_read_only"] is True
    assert Decimal(close["fifo_acquisition_cost_usd"]) == Decimal("50")
    expected = Decimal("75") - Decimal("50") - Decimal("0.05") - Decimal("0.2")
    if asset in ("BNB", "POL", "AVAX"):
        expected -= Decimal("0.1") + Decimal("0.4")
    assert Decimal(close["realized_net_usd"]) == expected


def test_fifo_spans_usdc_and_usdt_buy_lots_for_link():
    records = [
        trade("LINK","BUY","USDC",100,TX1,"80","2",network_fee="0"),
        trade("LINK","BUY","USDT",120,TX2,"90","2",network_fee="0"),
        trade("LINK","SELL","USDC",140,TX3,"3","180",network_fee="0"),
    ]
    result = reconcile_verified_evm_fifo_net(ledger("LINK",records))
    close = result["closed_trades"][0]
    assert Decimal(close["fifo_acquisition_cost_usd"]) == Decimal("125")
    assert Decimal(close["realized_net_usd"]) == Decimal("55")
    assert result["remaining_fifo_units"] == "1"


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(history_not_truncated=False),
    lambda e: e.update(contains_paper=True),
    lambda e: e["asset_transfer_audit"].update(external_inventory_reconciled=False),
    lambda e: e["asset_transfer_audit"].update(zero_initial_asset_balance_verified=False),
    lambda e: e["fills"][1].update(onchain_finalized=False),
    lambda e: e["fills"][1].update(wallet_address="0x"+"cc"*20),
    lambda e: e["fills"][1].update(block_number=50),
    lambda e: e["fills"][1].update(tx_hash=TX1),
    lambda e: e["fills"][0]["stable_usd_proof"].update(usd_price="0.8"),
    lambda e: e["fills"][1]["native_usd_proof"].update(execution_block_unix=999),
])
def test_missing_or_conflicting_verification_cannot_claim_net_fee(mutation):
    values = ledger("LINK",[
        trade("LINK","BUY","USDC",100,TX1,"100","2"),
        trade("LINK","SELL","USDC",200,TX2,"1","75"),
    ])
    mutation(values)
    with pytest.raises((ValueError, TypeError)):
        reconcile_verified_evm_fifo_net(values)


def test_no_external_or_unmatched_inventory_can_be_turned_into_fee():
    record = trade("UNI","SELL","USDC",100,TX2,"2","200")
    with pytest.raises(ValueError,match="unmatched SELL"):
        reconcile_verified_evm_fifo_net(ledger("UNI",[record]))


def test_no_synthetic_fee_signer_or_database_persistence_in_math_module():
    import inspect
    import backend.shared_evm_fifo_net as module
    source = inspect.getsource(module)
    assert "psycopg" not in source
    assert "sign_privy" not in source
    assert "record_internal_evm_realized_close" not in source
    assert "fee_assessed\" = True" not in source
