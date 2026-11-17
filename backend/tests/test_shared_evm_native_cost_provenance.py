"""Native EVM gas must come from original owner-signed finalized WETH conversion."""
from copy import deepcopy

import pytest
from backend.shared_evm_native_cost_provenance import (
    bind_original_finalized_native_cost, check_original_cycle_link,
)
from backend.shared_evm_swap_receipts import _pair

WALLET = "0x" + "ab"*20
SWAP = "0x" + "11"*32
CONV = "0x" + "22"*32
HASH = "0x" + "cc"*32
KEY = "sig_00000123456789"


def fixture(asset, side):
    stable = "USDT" if asset == "BNB" else "USDC"
    route = _pair(asset,stable,side)
    fill = dict(mode="INDEPENDENT_FINALIZED_EVM_EXECUTION",
                onchain_finalized=True, asset=asset, stable=stable,
                side=side, wallet_address=WALLET, chain_id=route["chain_id"],
                network=route["network"], tx_hash=SWAP, intent_key=KEY,
                block_number=100, source_amount="1.5",
                destination_amount="1.5")
    cycle = dict(asset=asset,stable=stable,side=side,intent_key=KEY,
                 wallet_address=WALLET,chain_id=route["chain_id"],
                 swap_tx_hash=SWAP,conversion_tx_hash=CONV)
    conversion = dict(mode="SHARED_FINALIZED_NATIVE_EVM_CONVERSION",
                      state="FINALIZED_NATIVE_CONVERSION",
                      transaction_finalized=True,
                      confirmed_native_conversion=True,asset=asset,
                      network=route["network"],chain_id=route["chain_id"],
                      wallet_address=WALLET,tx_hash=CONV,
                      operation="WITHDRAW" if side=="BUY" else "DEPOSIT",
                      amount_native="1.5",network_fee_native="0.002",
                      block_number=101 if side=="BUY" else 99)
    height=conversion["block_number"]
    timestamp=1800100
    receipt=dict(transactionHash=CONV,from_=WALLET,
                 blockHash=HASH, blockNumber=hex(height))
    receipt["from"]=receipt.pop("from_")
    block=dict(number=hex(height),hash=HASH,timestamp=hex(timestamp))
    finalized=dict(number="0x66")
    rate=dict(mode="HISTORICAL_EXECUTION_USD_REFERENCE_ONLY",
              provider="COINGECKO_MARKET_CHART_RANGE",asset=asset,
              execution_block_unix=timestamp,rate_observed_unix=timestamp+12,
              usd_price="500")
    return (stable,fill,cycle,conversion,receipt,block,finalized,rate)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_complete_original_native_conversion_economic_provenance(asset,side):
    stable,fill,cycle,conversion,receipt,block,finalized,rate=fixture(asset,side)
    assert check_original_cycle_link(
        asset=asset,side=side,stable=stable,fill=fill,cycle=cycle)==CONV
    result=bind_original_finalized_native_cost(
        asset=asset,side=side,stable=stable,fill=fill,
        conversion=conversion,receipt=receipt,block=block,
        finalized=finalized,native_usd_proof=rate)
    assert result["network_fee_native"]=="0.002"
    assert result["native_usd_proof"]["usd_price"]=="500"
    assert result["finalized"] is True
    assert result["fee_assessed"] is False


@pytest.mark.parametrize("mutation",[
    lambda d:d[2].update(swap_tx_hash=CONV),
    lambda d:d[2].update(conversion_tx_hash=SWAP),
    lambda d:d[2].update(wallet_address="0x"+"ff"*20),
    lambda d:d[2].update(intent_key="other_signal"),
    lambda d:d[2].update(chain_id=1),
])
def test_native_cycle_db_link_cannot_be_substituted(mutation):
    data=list(fixture("BNB","BUY"))
    mutation(data)
    stable,fill,cycle,*_=data
    with pytest.raises(ValueError):
        check_original_cycle_link(asset="BNB",side="BUY",stable=stable,
                                  fill=fill,cycle=cycle)


@pytest.mark.parametrize("mutation",[
    lambda d:d[3].update(wallet_address="0x"+"ff"*20),
    lambda d:d[3].update(operation="DEPOSIT"),
    lambda d:d[3].update(amount_native="1.4"),
    lambda d:d[3].update(network_fee_native="-1"),
    lambda d:d[3].update(transaction_finalized=False),
    lambda d:d[4].update(blockHash=SWAP),
    lambda d:d[5].update(number="0x65"),
    lambda d:d[6].update(number="0x20"),
    lambda d:d[7].update(execution_block_unix=1800200),
    lambda d:d[7].update(usd_price="NaN"),
])
def test_malformed_native_receipt_gas_or_price_never_becomes_fifo_fee(mutation):
    data=list(fixture("AVAX","SELL"))
    mutation(data)
    stable,fill,cycle,conversion,receipt,block,finalized,rate=data
    with pytest.raises((ValueError,TypeError)):
        bind_original_finalized_native_cost(
            asset="AVAX",side="SELL",stable=stable,fill=fill,
            conversion=conversion,receipt=receipt,block=block,
            finalized=finalized,native_usd_proof=rate)
