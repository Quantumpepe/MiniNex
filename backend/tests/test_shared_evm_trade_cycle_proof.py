"""Fully finalized owner-bound EVM trade cycles require correct swap and native legs."""
from copy import deepcopy

import pytest

from backend.shared_evm_swap_receipts import _pair
from backend.shared_evm_trade_cycle_proof import verify_evm_trade_cycle

WALLET="0x"+"ab"*20
SWAP="0x"+"cd"*32
NATIVE="0x"+"ef"*32
STABLE={"ETH":"USDC","BNB":"USDT","POL":"USDC",
        "AVAX":"USDC","LINK":"USDC","UNI":"USDC"}


def proof(asset,side):
    stable=STABLE[asset]
    pair=_pair(asset,stable,side)
    swap={
        "mode":"SHARED_EVM_FINALIZED_SWAP_VERIFICATION",
        "asset":asset,"side":side,"stable":stable,
        "network":pair["network"],"chain_id":pair["chain_id"],
        "wallet_address":WALLET,"tx_hash":SWAP,
        "state":"FINALIZED_ERC20_SWAP_LEG",
        "transaction_finalized":True,"swap_erc20_leg_verified":True,
        "block_number":100,
        "source_spent_amount":"100" if side=="BUY" else "2",
        "destination_received_amount":"2" if side=="BUY" else "110",
    }
    conversion=(None if asset in ("LINK","UNI") else {
        "mode":"SHARED_FINALIZED_NATIVE_EVM_CONVERSION",
        "asset":asset,"network":pair["network"],
        "chain_id":pair["chain_id"],
        "wallet_address":WALLET,"tx_hash":NATIVE,
        "state":"FINALIZED_NATIVE_CONVERSION",
        "transaction_finalized":True,"confirmed_native_conversion":True,
        "operation":"WITHDRAW" if side=="BUY" else "DEPOSIT",
        "block_number":101 if side=="BUY" else 99,
        "amount_native":"2",
    })
    return swap,conversion


@pytest.mark.parametrize("asset",["ETH","BNB","POL","AVAX","LINK","UNI"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_shared_evm_cycle_reconciles_only_correct_confirmed_legs(asset,side):
    swap,native=proof(asset,side)
    r=verify_evm_trade_cycle(
        asset=asset,side=side,stable=STABLE[asset],
        wallet=WALLET,swap_proof=swap,native_proof=native)
    assert r["required_transaction_legs_complete"] is True
    assert r["swap_tx_hash"]==SWAP
    assert r["native_conversion_verified"]==(asset not in ("LINK","UNI"))
    assert r["native_conversion_tx_hash"]==(
        None if asset in ("LINK","UNI") else NATIVE)
    assert r["five_percent_fee_assessed"] is False
    assert r["inventory_or_cost_basis_settled"] is False
    assert r["live_execution_enabled_by_this_verifier"] is False


@pytest.mark.parametrize("asset",["ETH","BNB","POL","AVAX"])
@pytest.mark.parametrize("side",["BUY","SELL"])
@pytest.mark.parametrize("mutate",[
    lambda n:n.update(amount_native="1.999"),
    lambda n:n.update(wallet_address="0x"+"aa"*20),
    lambda n:n.update(tx_hash=SWAP),
    lambda n:n.update(transaction_finalized=False),
    lambda n:n.update(state="INCLUDED_SUCCESS"),
    lambda n:n.update(operation="DEPOSIT" if n["operation"]=="WITHDRAW" else "WITHDRAW"),
    lambda n:n.update(block_number=100),
])
def test_native_asset_cannot_silently_skip_conversion_or_match_wrong_tx(
    asset,side,mutate
):
    swap,native=proof(asset,side)
    mutate(native)
    with pytest.raises(ValueError):
        verify_evm_trade_cycle(
            asset=asset,side=side,stable=STABLE[asset],
            wallet=WALLET,swap_proof=swap,native_proof=native)


@pytest.mark.parametrize("asset",["ETH","BNB","POL","AVAX","LINK","UNI"])
@pytest.mark.parametrize("mutate",[
    lambda s:s.update(transaction_finalized=False),
    lambda s:s.update(swap_erc20_leg_verified=False),
    lambda s:s.update(state="INCLUDED_SUCCESS"),
    lambda s:s.update(wallet_address="0x"+"aa"*20),
    lambda s:s.update(chain_id=999),
    lambda s:s.update(source_spent_amount="NaN"),
])
def test_no_bad_erc20_swap_may_complete_trade(asset,mutate):
    swap,native=proof(asset,"BUY")
    mutate(swap)
    with pytest.raises(ValueError):
        verify_evm_trade_cycle(
            asset=asset,side="BUY",stable=STABLE[asset],
            wallet=WALLET,swap_proof=swap,native_proof=native)


def test_token_only_asset_rejects_unrelated_native_conversion():
    swap,native=proof("LINK","SELL")
    other=proof("ETH","SELL")[1]
    with pytest.raises(ValueError):
        verify_evm_trade_cycle(
            asset="LINK",side="SELL",stable="USDC",
            wallet=WALLET,swap_proof=swap,native_proof=other)


def test_owner_api_is_read_only_and_requires_jwt():
    import inspect
    from backend.app import app
    r=next(r for r in app.routes
        if r.path=="/api/live/evm/completed-cycle/{asset}/{swap_tx_hash}")
    source=inspect.getsource(r.endpoint)
    assert "verify_privy_bearer" in source
    assert "check_user_evm_swap" in source
    assert "check_user_native_evm_conversion" in source
    assert r.methods=={"GET"}
