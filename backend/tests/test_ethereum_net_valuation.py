import asyncio
from fractions import Fraction
import pytest
from backend import ethereum_net_valuation as m
from backend.staged_net_valuation import read_ratio

@pytest.mark.parametrize("asset",m.ASSETS)
def test_central_fill_valuation_uses_original_gas_and_rates(monkeypatch,asset):
    wallet="0x"+"a"*40;h="0x"+"b"*64
    fill=dict(asset=asset,chain_id=1,privy_did="did:privy:owner",tx_hash=h,
        wallet_address=wallet,side="SELL",stable="USDC",source_amount="1",
        destination_amount="100",network_fee_native="0.001")
    async def swap(*args,**kwargs):
        return dict(asset=asset,side="SELL",stable="USDC",chain_id=1,tx_hash=h,
            wallet_address=wallet,state="FINALIZED_ERC20_SWAP_LEG",
            transaction_finalized=True,swap_erc20_leg_verified=True,
            source_spent_amount="1",destination_received_amount="100",network_fee_native="0.001")
    async def mined(client,chain,tx):
        return dict(chain_id=chain,tx_hash=tx,block_unix=1000,gas_atomic="1000000000000000",**{"from":wallet})
    async def historic(*,asset,block_unix,client):
        return dict(asset=asset,provider="COINGECKO_MARKET_CHART_RANGE",
            execution_block_unix=block_unix,rate_observed_unix=block_unix,
            usd_price="2000" if asset=="ETH" else "0.99")
    monkeypatch.setattr(m,"check_user_evm_swap",swap)
    monkeypatch.setattr(m,"mined_evidence",mined)
    monkeypatch.setattr(m,"fetch_recent_historical_usd_observation",historic)
    result=asyncio.run(m.value_ethereum_order(None,fill,[]))
    assert result["stable_atomic"]=="100000000"
    assert read_ratio(result["stable_usd"])==Fraction(99,100)
    assert read_ratio(result["network_cost_usd"])==2

def test_unresolved_cost_never_assumed_free():
    with pytest.raises(ValueError):
        asyncio.run(m.check_cost_transaction(None,{},{"tx_hash":None,"kind":"APPROVAL","status":"SENDING"}))
