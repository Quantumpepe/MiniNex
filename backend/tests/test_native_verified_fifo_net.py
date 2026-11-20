"""Verified native FIFO arithmetic before first real HWM 5% fee obligation."""
from copy import deepcopy
import pytest
from backend import native_verified_fifo_net as m

DID="did:privy:owner_native_financial_001"
ASSETS={"BTC":8,"SOL":9,"XRP":6}
NETWORK={"BTC":"bitcoin-mainnet","SOL":"solana-mainnet","XRP":"xrpl-mainnet"}


def usd(asset,when,price="1"):
    return {"mode":"HISTORICAL_EXECUTION_USD_REFERENCE_ONLY",
            "asset":asset,"provider":"COINGECKO_MARKET_CHART_RANGE",
            "execution_block_unix":when,"rate_observed_unix":when,
            "usd_price":price}


def leg(asset,side,index,source,receive_atomic,stable="USDC",
        source_fee="0"):
    at=1803000000+index*60
    source_asset="ETH" if side=="BUY" else asset
    gas_asset_usd="2000" if side=="BUY" else "100"
    original_hash="0x"+"d"*64 if side=="BUY" else "a"*64
    destination_hash="b"*64 if side=="BUY" else "0x"+"e"*64
    proof="c"*64
    return {
        "privy_did":DID,"asset":asset,"side":side,
        "status":"DESTINATION_FINALIZED","stable":stable,
        "intent_key":"native_"+format(index,"064x"),
        "payload_sha256":"1"*64,
        "source_proof_sha256":proof,
        "destination_proof_sha256":"2"*64,
        "execution_block_unix":at,
        "source_tx_hash":original_hash,
        "destination_tx_hash":destination_hash,
        "input_amount":source,
        "destination_received_atomic":str(receive_atomic),
        "stable_usd_reference":usd(stable,at),
        "network_fee":{
            "mode":"CONFIRMED_NATIVE_ORIGINAL_SOURCE_NETWORK_COST",
            "asset":source_asset,
            "chain":"ethereum-mainnet" if side=="BUY" else NETWORK[asset],
            "source_tx_hash":original_hash,
            "execution_block_unix":at,
            "original_receipt_proof_sha256":proof,
            "fee_asset_amount":source_fee,
            "fee_asset_usd_reference":usd(source_asset,at,gas_asset_usd),
        },
    }


def history(asset="BTC"):
    return {"asset":asset,"privy_did":DID,"fills":[]}


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_one_original_roundtrip_hwm_5pct_net_profit(asset):
    atomic=10**ASSETS[asset]
    data=history(asset)
    data["fills"]=[
        leg(asset,"BUY",1,"100",atomic),
        leg(asset,"SELL",2,"1",120000000),
    ]
    report=m.preview_native_fifo_net_hwm(data)
    close=report["close_previews"][0]
    assert close["realized_net_usd"]=="20.000000000000"
    assert close["net_gain_above_previous_hwm_usd"]=="20.000000000000"
    assert close["indicative_5pct_hwm_usd"]=="1.000000"
    assert close["fee_assessed"] is False
    assert report["fee_obligation_created"] is False
    assert report["fee_payment_transferred"] is False
    assert report["complete_original_wallet_history_independently_audited"] is False
    assert report["remaining_native_quantity"]=="0"


def test_realized_losses_reduce_next_fee_until_prior_hwm_recovered():
    a=10**ASSETS["BTC"]
    h=history()
    h["fills"]=[
        leg("BTC","BUY",1,"100",a),
        leg("BTC","SELL",2,"0.5",60000000),
        leg("BTC","SELL",3,"0.5",30000000),
        leg("BTC","BUY",4,"50",a),
        leg("BTC","SELL",5,"1",75000000),
    ]
    r=m.preview_native_fifo_net_hwm(h)
    assert [x["realized_net_usd"] for x in r["close_previews"]]==[
        "10.000000000000","-20.000000000000","25.000000000000"]
    assert [x["indicative_5pct_hwm_usd"] for x in r["close_previews"]]==[
        "0.500000","0.000000","0.250000"]
    assert r["cumulative_realized_net_usd"]=="15.000000000000"
    assert r["high_water_mark_usd"]=="15.000000000000"
    assert r["fee_payment_transferred"] is False


def test_native_sell_network_fee_reduces_verified_realized_net_profit():
    h=history("BTC")
    h["fills"]=[
        leg("BTC","BUY",1,"100",100000000,source_fee="0.001"),
        leg("BTC","SELL",2,"1",130000000,source_fee="0.01"),
    ]
    r=m.preview_native_fifo_net_hwm(h)
    close=r["close_previews"][0]
    # BUY pays $2 ETH gas. SELL pays $1 BTC miner fee.
    assert close["realized_net_usd"]=="27.000000000000"
    assert close["indicative_5pct_hwm_usd"]=="1.350000"
    assert close["original_source_network_fee_usd"]=="1.000000000000"


def test_strict_fifo_allows_partial_close_without_double_counting_buy_fee():
    h=history("SOL")
    h["fills"]=[
        leg("SOL","BUY",1,"40",2*10**9,source_fee="0.001"),
        leg("SOL","SELL",2,"1",30000000),
    ]
    r=m.preview_native_fifo_net_hwm(h)
    # Original BUY cost 42 USD; one SOL sold from two => 21 USD allocated.
    assert r["close_previews"][0]["buy_fifo_allocated_cost_usd"]=="21.000000000000"
    assert r["close_previews"][0]["realized_net_usd"]=="9.000000000000"
    assert r["remaining_native_quantity"]=="1"


@pytest.mark.parametrize("mutate",[
    lambda leg:leg.update(status="SOURCE_FINALIZED"),
    lambda leg:leg.update(status="REFUNDED_VERIFIED"),
    lambda leg:leg.update(payload_sha256="bad"),
    lambda leg:leg.update(destination_proof_sha256="bad"),
    lambda leg:leg.update(stable="FAKE"),
    lambda leg:leg.update(source_tx_hash="unrelated"),
    lambda leg:leg.update(execution_block_unix=0),
    lambda leg:leg["stable_usd_reference"].update(asset="BTC"),
    lambda leg:leg["stable_usd_reference"].update(usd_price="0.6"),
    lambda leg:leg["network_fee"].update(source_tx_hash="other"),
    lambda leg:leg["network_fee"].update(fee_asset_amount="-0.1"),
    lambda leg:leg["network_fee"]["fee_asset_usd_reference"].update(asset="DOGE"),
])
def test_fake_paper_or_mismatched_original_chain_receipt_cannot_create_fee(mutate):
    a=leg("BTC","BUY",1,"100",100000000)
    b=leg("BTC","SELL",2,"1",120000000)
    mutate(b)
    with pytest.raises(ValueError):
        m.preview_native_fifo_net_hwm({
            "asset":"BTC","privy_did":DID,"fills":[a,b]})


def test_same_original_source_key_cannot_create_two_sell_fees():
    h=history()
    h["fills"]=[
        leg("BTC","BUY",1,"100",100000000),
        leg("BTC","SELL",2,"0.5",70000000),
        leg("BTC","SELL",2,"0.5",70000000),
    ]
    with pytest.raises(ValueError):
        m.preview_native_fifo_net_hwm(h)


def test_missing_acquisition_history_does_not_invent_coins():
    h=history()
    h["fills"]=[leg("BTC","SELL",1,"1",100000000)]
    with pytest.raises(ValueError):
        m.preview_native_fifo_net_hwm(h)


def test_native_coin_budgets_and_profits_never_mix_across_assets():
    h=history("SOL")
    h["fills"]=[
        leg("SOL","BUY",1,"100",10**9),
        leg("BTC","SELL",2,"1",120000000),
    ]
    with pytest.raises(ValueError):
        m.preview_native_fifo_net_hwm(h)
