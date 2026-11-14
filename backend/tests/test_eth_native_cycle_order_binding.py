"""Pure intent/receipt binding is NEVER a settlement authorization."""
from copy import deepcopy

import pytest

from backend.eth_native_cycle_order_binding import inspect_eth_order_receipt_binding

S = "0x" + "a" * 64
C = "0x" + "b" * 64

def evidence(side="BUY", stable="USDC"):
    order = dict(intent_key="safeorder0001", asset="ETH",
                 chain_network="ethereum-mainnet", state="SUBMITTED",
                 side=side, stable=stable, swap_tx_hash=S,
                 conversion_tx_hash=C, quote_digest="c"*64)
    swap = dict(asset="ETH", network="ethereum-mainnet", tx_hash=S,
                state="FINALIZED_ERC20_SWAP_LEG", side=side, stable=stable,
                swap_output_asset="WETH" if side=="BUY" else stable,
                transaction_finalized=True, swap_erc20_leg_verified=True,
                source_spent_amount="100" if side=="BUY" else "0.2",
                destination_received_amount="0.2" if side=="BUY" else "100",
                network_fee_eth="0.0001", execution_enabled=False, can_submit=False)
    conversion = dict(asset="ETH", network="ethereum-mainnet", tx_hash=C,
                      state="FINALIZED_WETH9_CONVERSION",
                      operation="WITHDRAW" if side=="BUY" else "DEPOSIT",
                      converted_asset="ETH" if side=="BUY" else "WETH",
                      transaction_finalized=True, confirmed_native_conversion=True,
                      weth9_event_amount="0.2", network_fee_eth="0.0002",
                      execution_enabled=False, can_submit=False)
    return order,swap,conversion

@pytest.mark.parametrize("side",["BUY","SELL"])
@pytest.mark.parametrize("stable",["USDC","USDT"])
def test_advisory_only_for_all_routes(side,stable):
    order,swap,conversion=evidence(side,stable)
    snapshot=deepcopy((order,swap,conversion))
    result=inspect_eth_order_receipt_binding(order=order,swap=swap,conversion=conversion)
    assert result["matched_recorded_hashes"]
    assert result["network_fee_eth"]=="0.0003"
    assert result["same_order_cryptographically_proven"] is False
    assert result["ledger_settled"] is False
    assert result["portfolio_credit_allowed"] is False
    assert result["performance_fee_allowed"] is False
    assert result["can_submit"] is False
    assert (order,swap,conversion)==snapshot

@pytest.mark.parametrize("field,value",[
    ("asset","BTC"), ("chain_network","bitcoin-mainnet"),
    ("state","SENDING"), ("state","RECONCILIATION_REQUIRED"),
    ("state","REORG_PENDING"), ("state","RESERVED"),
    ("state","REVERTED"), ("state","CANCELLED"),
    ("swap_tx_hash",C), ("conversion_tx_hash",S),
    ("swap_tx_hash","bad"), ("quote_digest","bad"),
    ("intent_key","bad"), ("stable","BTC"), ("side","SHORT"),
])
def test_untrusted_order_fails_closed(field,value):
    order,swap,conversion=evidence()
    order[field]=value
    with pytest.raises(ValueError):
        inspect_eth_order_receipt_binding(order=order,swap=swap,conversion=conversion)

@pytest.mark.parametrize("field,value",[
    ("tx_hash","0x"+"f"*64), ("state","PENDING"),
    ("transaction_finalized",False), ("swap_erc20_leg_verified",False),
    ("destination_received_amount","0.19"),
    ("network_fee_eth","NaN"),
])
def test_wrong_or_unconfirmed_swap_fails_closed(field,value):
    order,swap,conversion=evidence()
    swap[field]=value
    with pytest.raises(ValueError):
        inspect_eth_order_receipt_binding(order=order,swap=swap,conversion=conversion)

@pytest.mark.parametrize("field,value",[
    ("tx_hash","0x"+"f"*64), ("state","FINALIZED_REVERTED"),
    ("transaction_finalized",False), ("confirmed_native_conversion",False),
    ("weth9_event_amount","0.19"), ("network_fee_eth","-1"),
])
def test_wrong_or_unconfirmed_native_leg_fails_closed(field,value):
    order,swap,conversion=evidence()
    conversion[field]=value
    with pytest.raises(ValueError):
        inspect_eth_order_receipt_binding(order=order,swap=swap,conversion=conversion)
