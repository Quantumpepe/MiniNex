"""Shared six-EVM exact FIFO gross spread only, never invented net PnL."""
from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend.shared_evm_fifo import evm_fifo_gross_preview,read_evm_fifo_gross_preview
from backend.shared_evm_swap_receipts import _pair


def fill(asset,side,spent,received,key,stable=None,gas="0.001"):
    stable=stable or ("USDT" if asset=="BNB" else "USDC")
    p=_pair(asset,stable,side)
    return {
        "asset":asset,"chain_id":p["chain_id"],"network":p["network"],
        "stable":stable,"side":side,"intent_key":key,
        "spent_asset":stable if side=="BUY" else asset,
        "received_asset":asset if side=="BUY" else stable,
        "spent_amount":str(spent),"received_amount":str(received),
        "network_fee_native":gas,
        "onchain_finalized":True,
    }


def history(asset,*fills,truncated=False):
    return {
        "asset":asset,"mode":"FINALIZED_EVM_REAL_FILLS_ONLY",
        "includes_simulation":False,"history_truncated":truncated,
        "fills":list(fills),
    }


@pytest.mark.parametrize("asset",["ETH","BNB","POL","AVAX","LINK","UNI","ARB"])
def test_six_assets_fifo_partial_sells_are_separate_and_gas_not_fake_usd(asset):
    rows=history(asset,
        fill(asset,"SELL","0.75","90","sell2",gas="0.004"),
        fill(asset,"SELL","0.25","30","sell1",gas="0.003"),
        fill(asset,"BUY","100","1","buy",gas="0.002"))
    v=evm_fifo_gross_preview(rows)
    stable="USDT" if asset=="BNB" else "USDC"
    assert v["asset"]==asset
    assert Decimal(v["gross_spread_by_stable"][stable])==20
    assert [r["status"] for r in v["closed_trades"]]==[
        "MATCHED_GROSS_ONLY","MATCHED_GROSS_ONLY"]
    assert Decimal(v["closed_trades"][0]["allocated_buy_cost_stable"])==75
    assert Decimal(v["closed_trades"][1]["allocated_buy_cost_stable"])==25
    assert v["verified_swap_network_fee_native"]=="0.009"
    assert v["verified_net_pnl_usd"] is None
    assert v["performance_fee_assessed"] is False
    assert v["fee_paid_onchain"] is False


def test_fifo_multiple_buy_lots_and_partial_exit_cost_correct():
    rows=history("LINK",
        fill("LINK","SELL","1.5","220","sell"),
        fill("LINK","BUY","140","1","buy2"),
        fill("LINK","BUY","100","1","buy1"))
    v=evm_fifo_gross_preview(rows)
    assert Decimal(v["gross_spread_by_stable"]["USDC"])==50
    assert Decimal(v["closed_trades"][0]["allocated_buy_cost_stable"])==170
    assert Decimal(v["remaining_fifo_units_not_wallet_balance"]["USDC"])==Decimal("0.5")


def test_unmatched_sell_does_not_reuse_known_inventory_for_later_sale():
    rows=history("AVAX",
        fill("AVAX","SELL","1","100","sell2"),
        fill("AVAX","SELL","2","250","sell1"),
        fill("AVAX","BUY","100","1","buy"))
    v=evm_fifo_gross_preview(rows)
    assert v["unmatched_sell_count"]==2
    assert v["gross_spread_by_stable"]["USDC"] is None
    assert v["closed_trades"][0]["status"]=="UNMATCHED_COST_BASIS"
    assert v["closed_trades"][1]["status"]=="UNMATCHED_COST_BASIS"


def test_stablecoin_fifo_lots_never_cross_usdc_and_usdt():
    rows=history("UNI",
        fill("UNI","SELL","1","120","sell","USDT"),
        fill("UNI","BUY","100","1","buy","USDC"))
    v=evm_fifo_gross_preview(rows)
    assert v["unmatched_sell_count"]==1
    assert v["gross_spread_by_stable"]["USDC"] is None
    assert v["gross_spread_by_stable"]["USDT"] is None


def test_history_truncated_never_claims_complete_pnl():
    v=evm_fifo_gross_preview(history("BNB",
        fill("BNB","SELL","1","150","sell"),
        fill("BNB","BUY","100","1","buy"),truncated=True))
    assert v["gross_spread_by_stable"]["USDT"] is None
    assert "HISTORY_TRUNCATED" in v["fee_blockers"]
    assert v["hwm_updated"] is False


@pytest.mark.parametrize("bad",["NaN","Infinity","-0.1","1e28",None,"0.0000000000000000001"])
def test_wrong_amounts_or_precision_cannot_enter_fifo(bad):
    rows=history("POL",fill("POL","BUY","100","1","buy"))
    rows["fills"][0]["spent_amount"]=bad
    with pytest.raises(ValueError):
        evm_fifo_gross_preview(rows)


@pytest.mark.parametrize("bad",["NaN","-1","Infinity",None,"1e28"])
def test_unverified_native_network_costs_cannot_be_zeroed(bad):
    rows=history("BNB",fill("BNB","BUY","100","1","buy"))
    rows["fills"][0]["network_fee_native"]=bad
    with pytest.raises(ValueError):
        evm_fifo_gross_preview(rows)


def test_verified_zero_gas_and_duplicate_id_handling():
    rows=history("POL",fill("POL","BUY","100","1","buy",gas="0"))
    assert evm_fifo_gross_preview(rows)["verified_swap_network_fee_native"]=="0"
    rows["fills"].append(deepcopy(rows["fills"][0]))
    with pytest.raises(ValueError):
        evm_fifo_gross_preview(rows)


def test_unverified_chain_or_paper_rows_rejected():
    row=fill("LINK","BUY","100","1","buy")
    row["chain_id"]=56
    with pytest.raises(ValueError):
        evm_fifo_gross_preview(history("LINK",row))
    row=fill("LINK","BUY","100","1","buy")
    row["onchain_finalized"]=False
    with pytest.raises(ValueError):
        evm_fifo_gross_preview(history("LINK",row))


def test_owner_scoped_endpoint_only_reads_authenticated_real_fills():
    import inspect
    from backend.app import app
    api=next(x for x in app.routes
        if x.path=="/api/live/evm/fifo-preview/{asset}")
    source=inspect.getsource(api.endpoint)
    assert "verify_privy_bearer" in source
    assert "read_evm_fifo_gross_preview" in source
    assert "record_internal_evm_realized_close" not in source
    assert api.methods=={"GET"}


def test_reader_rejects_untrusted_source(monkeypatch):
    from backend import shared_evm_fill_history as h
    monkeypatch.setattr(h,"get_owner_evm_fill_history",
        lambda did,*,asset,limit:{"asset":asset,"mode":"PAPER","fills":[]})
    with pytest.raises(HTTPException) as err:
        read_evm_fifo_gross_preview("did:privy:test","BNB")
    assert err.value.status_code==503


def test_cross_chain_history_does_not_mix_local_cost_basis_or_gas():
    buy=fill("POL","BUY","100","2","cross_buy")
    buy.update(ethereum_settlement_verified=True,chain_id=1,network="ethereum-mainnet",destination_chain_id=137)
    sell=fill("POL","SELL","1","70","cross_sell")
    sell.update(ethereum_settlement_verified=True,destination_chain_id=1)
    legacy=fill("POL","BUY","10","3","legacy")
    result=evm_fifo_gross_preview(history("POL",sell,buy,legacy))
    cross=result["ethereum_settlement"]
    assert Decimal(cross["gross_spread_by_stable"]["USDC"])==20
    assert Decimal(cross["remaining_fifo_units_not_wallet_balance"]["USDC"])==1
    assert Decimal(result["remaining_fifo_units_not_wallet_balance"]["USDC"])==3
    assert set(cross["source_gas_by_network"])=={"ethereum-mainnet","polygon-pos-mainnet"}
    assert cross["verified_net_pnl_usd"] is None
    assert not cross["performance_fee_assessed"]
