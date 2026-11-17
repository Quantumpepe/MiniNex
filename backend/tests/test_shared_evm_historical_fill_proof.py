"""Canonical on-chain fill block time cannot be invented from UI-created_at."""
from copy import deepcopy

import pytest

from backend.shared_evm_historical_fill_proof import (
    verified_evm_swap_block_evidence,
)
from backend.shared_evm_swap_receipts import _pair

TX = "0x" + "ab"*32
BLOCK_HASH = "0x" + "cd"*32
WALLET = "0x" + "ef"*20


def setup(asset):
    stable = "USDT" if asset == "BNB" else "USDC"
    route = _pair(asset, stable, "SELL")
    proof = {
        "mode": "SHARED_EVM_FINALIZED_SWAP_VERIFICATION",
        "state": "FINALIZED_ERC20_SWAP_LEG",
        "transaction_finalized": True,
        "swap_erc20_leg_verified": True,
        "tx_hash": TX, "asset": asset, "stable": stable, "side": "SELL",
        "chain_id": route["chain_id"], "network": route["network"],
        "wallet_address": WALLET, "block_number": 100,
        "source_spent_amount": "1.5",
        "destination_received_amount": "350.5",
        "network_fee_native": "0.00021",
    }
    receipt = {
        "transactionHash": TX, "blockHash": BLOCK_HASH, "from": WALLET,
        "blockNumber": "0x64", "transactionIndex": "0x3",
    }
    block = {
        "hash": BLOCK_HASH, "number": "0x64",
        "timestamp": "0x19a3a22f",
    }
    return stable, proof, receipt, block


@pytest.mark.parametrize("asset", ["BNB", "POL", "AVAX", "LINK", "UNI"])
def test_original_mined_transaction_time_and_index_are_proven(asset):
    stable, proof, receipt, block = setup(asset)
    result = verified_evm_swap_block_evidence(
        proof=proof, tx_hash=TX, asset=asset, stable=stable,
        side="SELL", receipt=receipt, block=block)
    assert result["block_number"] == 100
    assert result["transaction_index"] == 3
    assert result["block_unix"] == int(block["timestamp"], 16)
    assert result["network_fee_native"] == "0.00021"
    assert result["onchain_finalized"] is True
    assert result["external_inventory_reconciled"] is False
    assert result["fee_assessed"] is False


@pytest.mark.parametrize("change", [
    lambda p, r, b: p.update(transaction_finalized=False),
    lambda p, r, b: p.update(swap_erc20_leg_verified=False),
    lambda p, r, b: p.update(wallet_address="0x"+"cd"*20),
    lambda p, r, b: p.update(block_number=99),
    lambda p, r, b: p.update(network_fee_native="-0.1"),
    lambda p, r, b: p.update(destination_received_amount="NaN"),
    lambda p, r, b: r.update(blockHash="0x"+"ff"*32),
    lambda p, r, b: r.update(transactionIndex="-0x1"),
    lambda p, r, b: b.update(timestamp="0x0"),
    lambda p, r, b: b.update(number="0x65"),
])
def test_any_inconsistent_historical_swap_source_fails_closed(change):
    stable, proof, receipt, block = setup("LINK")
    change(proof, receipt, block)
    with pytest.raises(ValueError):
        verified_evm_swap_block_evidence(
            proof=proof, tx_hash=TX, asset="LINK", stable=stable,
            side="SELL", receipt=receipt, block=block)
