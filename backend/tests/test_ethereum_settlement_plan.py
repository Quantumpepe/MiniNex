from decimal import Decimal
import pytest
from backend.ethereum_settlement_plan import (
    COIN_NETWORKS,ETHEREUM_STABLES,settlement_route,check_evm_quote_route,
    ethereum_buy_amount,ethereum_sell_amount,
)
from backend.live_asset_registry import ASSET_ORDER
from backend.manual_swap_routes import build_manual_swap_route

def test_all_twelve_coins_both_stables_both_directions():
    assert tuple(COIN_NETWORKS)==ASSET_ORDER
    for coin in ASSET_ORDER:
        for stable in ETHEREUM_STABLES:
            for side in ("BUY","SELL"):
                route=settlement_route(coin,side,stable)
                assert route["settlement_chain_id"]==1
                assert route["settlement_contract"]==ETHEREUM_STABLES[stable]
                assert route["source_chain_id" if side=="BUY" else "destination_chain_id"]==1
                assert build_manual_swap_route(route["source_asset"],route["destination_asset"])["ethereum_settlement"]==route

def test_noncanonical_stable_contract_cannot_fund_or_settle():
    terms=dict(asset="POL",side="BUY",stable="USDC",source_chain_id=1,
               destination_chain_id=137,source_token=ETHEREUM_STABLES["USDC"],destination_token="0x"+"0"*40)
    assert check_evm_quote_route(terms)["family"]=="CROSS_EVM"
    with pytest.raises(ValueError):
        check_evm_quote_route({**terms,"source_token":"0x"+"ab"*20})
    with pytest.raises(ValueError):
        check_evm_quote_route({**terms,"source_chain_id":137})

def test_independent_budget_and_shared_wallet_reservations_and_costs():
    terms=dict(budget_usd=100,position_usd=20,pending_coin_usd=5,
        wallet_stable_atomic=100_000_000,pending_wallet_stable_atomic=40_000_000,
        stable_usd="1.00",estimated_cost_usd=2)
    assert ethereum_buy_amount(**terms)["input_atomic"]=="60000000"
    assert ethereum_buy_amount(**{**terms,"pending_wallet_stable_atomic":0})["input_atomic"]=="73000000"
    assert ethereum_buy_amount(**{**terms,"pending_wallet_stable_atomic":200_000_000})["input_atomic"]=="0"
    assert ethereum_buy_amount(**terms)["can_submit"] is False
    with pytest.raises(ValueError):
        ethereum_buy_amount(**{**terms,"pending_coin_usd":-1})

def test_sell_only_strategy_inventory_and_preserve_native_gas():
    out=ethereum_sell_amount(asset="POL",wallet_coin_atomic=1000,
        owned_position_atomic=900,pending_sell_atomic=100,gas_reserve_atomic=300)
    assert out["input_atomic"]=="700"
    assert Decimal(out["input_amount"])==Decimal("0.0000000000000007")
    assert not out["can_submit"]
