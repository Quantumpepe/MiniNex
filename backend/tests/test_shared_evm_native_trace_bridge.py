"""Original native EVM historical cost bridge must independently inspect call traces."""
from __future__ import annotations

import asyncio
import httpx
import pytest

from backend import shared_evm_historical_fill_bridge as bridge
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
HASH="0x"+"12"*32


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_native_price_bridge_never_skips_original_native_trace(
    monkeypatch,asset,side,
):
    stable="USDT" if asset=="BNB" else "USDC"
    pair=_pair(asset,stable,side)
    original={
        "asset":asset,"side":side,"stable":stable,
        "chain_id":pair["chain_id"],"network":pair["network"],
        "wallet_address":WALLET,"tx_hash":HASH,
        "intent_key":"sig_original_12345678",
        "onchain_finalized":True,
        "spent_amount":"2.5","received_amount":"100",
        "network_fee_native":"0.0001",
    }
    proof={
        **{key:original[key] for key in (
            "asset","side","stable","chain_id","network",
            "wallet_address","tx_hash","onchain_finalized",
        )},
        "mode":"INDEPENDENT_FINALIZED_EVM_EXECUTION",
        "block_number":100,"block_unix":1800100,
        "transaction_index":0,"source_amount":"2.5",
        "destination_amount":"100","network_fee_native":"0.0001",
    }
    calls=[]
    async def confirmed(**kwargs):
        calls.append("verified original fill")
        return proof
    async def prices(**kwargs):
        calls.append("historical "+kwargs["asset"])
        return {
            "mode":"HISTORICAL_EXECUTION_USD_REFERENCE_ONLY",
            "asset":kwargs["asset"],
            "execution_block_unix":kwargs["block_unix"],
            "rate_observed_unix":kwargs["block_unix"],
            "usd_price":"1",
        }
    async def native_cost(**kwargs):
        calls.append("native conversion")
        return {"tx_hash":"0x"+"ef"*32}
    async def trace(**kwargs):
        calls.append("native value trace")
        assert kwargs["asset"]==asset and kwargs["side"]==side
        assert kwargs["conversion"]["tx_hash"]=="0x"+"ef"*32
        return {
            "original_finalized_internal_value_proven":True,
            "full_historical_wallet_activity_proven":False,
        }
    monkeypatch.setattr(bridge,"read_owner_finalized_evm_swap_block",confirmed)
    monkeypatch.setattr(bridge,"fetch_recent_historical_usd_observation",prices)
    monkeypatch.setattr(bridge,"read_original_native_execution_cost",native_cost)
    monkeypatch.setattr(bridge,"read_original_native_conversion_trace",trace)
    async def run():
        async with httpx.AsyncClient() as client:
            return await bridge.independently_price_stored_evm_fill(
                did="did:privy:owner_123",row=original,client=client,
                now=1800200,
            )
    result=asyncio.run(run())
    assert calls==[
        "verified original fill","historical "+stable,
        "historical "+asset,"native conversion","native value trace",
    ]
    assert result["native_cycle_complete"] is True
    assert result["external_inventory_reconciled"] is False
    assert result["native_conversion_trace"][
        "original_finalized_internal_value_proven"] is True
    assert result["fee_assessed"] is False


def test_fake_native_historical_wallet_audit_cannot_authorize_pnl(monkeypatch):
    asset="AVAX";stable="USDC";side="SELL"
    pair=_pair(asset,stable,side)
    row={
        "asset":asset,"side":side,"stable":stable,
        "chain_id":pair["chain_id"],"network":pair["network"],
        "wallet_address":WALLET,"tx_hash":HASH,
        "intent_key":"sig_original_12345678",
        "onchain_finalized":True,"spent_amount":"1",
        "received_amount":"100","network_fee_native":"0.0001",
    }
    proof={
        **{k:row[k] for k in (
            "asset","side","stable","chain_id","network",
            "wallet_address","tx_hash","onchain_finalized")},
        "mode":"INDEPENDENT_FINALIZED_EVM_EXECUTION",
        "block_number":100,"block_unix":1800100,"transaction_index":0,
        "source_amount":"1","destination_amount":"100",
        "network_fee_native":"0.0001",
    }
    async def original(**kw):return proof
    async def rate(**kw):
        return {"asset":kw["asset"],"execution_block_unix":kw["block_unix"]}
    async def cost(**kw):return {"tx_hash":"0x"+"ef"*32}
    async def fraudulent(**kw):
        return {
            "original_finalized_internal_value_proven":False,
            "full_historical_wallet_activity_proven":True,
        }
    monkeypatch.setattr(bridge,"read_owner_finalized_evm_swap_block",original)
    monkeypatch.setattr(bridge,"fetch_recent_historical_usd_observation",rate)
    monkeypatch.setattr(bridge,"read_original_native_execution_cost",cost)
    monkeypatch.setattr(bridge,"read_original_native_conversion_trace",fraudulent)
    async def run():
        async with httpx.AsyncClient() as client:
            return await bridge.independently_price_stored_evm_fill(
                did="did:privy:owner_123",row=row,client=client)
    with pytest.raises(ValueError):
        asyncio.run(run())
