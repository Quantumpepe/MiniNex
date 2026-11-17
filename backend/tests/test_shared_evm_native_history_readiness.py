"""Read-only native custody readiness sources ALL owner original fills."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from backend import shared_evm_native_history_readiness as reader
from backend.shared_evm_swap_receipts import _pair

OWNER="0x"+"ab"*20
TX1="0x"+"11"*32
TX2="0x"+"22"*32


def proof(asset,tx,side,block):
    stable="USDT" if asset=="BNB" else "USDC"
    route=_pair(asset,stable,side)
    return {
        "mode":"INDEPENDENT_FINALIZED_EVM_EXECUTION",
        "asset":asset,"side":side,
        "stable":stable,"network":route["network"],
        "chain_id":route["chain_id"],
        "wallet_address":OWNER,"tx_hash":tx,
        "block_number":block,"transaction_index":2,
        "historic_usd_priced":True,"native_cycle_complete":True,
        "native_conversion":{
            "tx_hash":"0x"+"ee"*32,
            "block_number":block+1,
        },
        "native_conversion_trace":{
            "original_finalized_internal_value_proven":True,
            "full_historical_wallet_activity_proven":False,
        },
    }


def audit(asset):
    return {
        "mode":"READ_ONLY_NATIVE_VALUE_INTERVAL_AUDIT",
        "asset":asset,"wallet_address":OWNER,
        "start_block":100,"end_block":200,
        "verified_native_conversion_count":2,
        "unexpected_native_value_movements":[],
        "external_native_value_free_interval":True,
        "complete_wallet_lifetime_and_cost_basis_proven":False,
        "eligible_to_assess_five_percent_fee":False,
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_native_history_reads_full_original_wallet_and_stays_fee_blocked(
    monkeypatch,asset,
):
    stored=[
        {"tx_hash":TX2,"wallet_address":OWNER},
        {"tx_hash":TX1,"wallet_address":OWNER},
    ]
    priced={
        TX1:proof(asset,TX1,"BUY",100),
        TX2:proof(asset,TX2,"SELL",200),
    }
    seen=[]
    monkeypatch.setattr(reader,"_load_all_verified_owner_native_fills",
                        lambda did,symbol:stored)
    async def price(*,did,row,client,now,durable_rates):
        assert durable_rates is True
        seen.append(row["tx_hash"])
        return priced[row["tx_hash"]]
    async def interval(**kwargs):
        assert [r["tx_hash"] for r in kwargs["fills"]]==[TX1,TX2]
        assert kwargs["asset"]==asset and kwargs["wallet"]==OWNER
        return audit(asset)
    monkeypatch.setattr(reader,"independently_price_stored_evm_fill",price)
    monkeypatch.setattr(reader,"audit_original_native_value_interval",interval)
    async def run():
        async with httpx.AsyncClient() as client:
            return await reader.read_owner_native_value_history_readiness(
                did="did:privy:owner_123",asset=asset,client=client)
    result=asyncio.run(run())
    assert seen==[TX2,TX1]
    assert result["original_finalized_fill_count"]==2
    assert result["all_original_conversion_trace_calls_verified"] is True
    assert result["external_value_free_within_observed_interval"] is True
    assert result["fee_assessment_eligible"] is False
    assert result["full_owner_wallet_cost_basis_proven"] is False
    assert result["fee_transferred"] is False


def test_external_native_value_seen_in_interval_never_becomes_hwm(monkeypatch):
    asset="BNB"
    stored=[{"tx_hash":TX1,"wallet_address":OWNER},
            {"tx_hash":TX2,"wallet_address":OWNER}]
    priced={TX1:proof(asset,TX1,"BUY",100),
            TX2:proof(asset,TX2,"SELL",200)}
    monkeypatch.setattr(reader,"_load_all_verified_owner_native_fills",
                        lambda *args:stored)
    async def price(**kw):return priced[kw["row"]["tx_hash"]]
    async def interval(**kw):
        report=audit(asset)
        report["external_native_value_free_interval"]=False
        report["unexpected_native_value_movements"]=[
            {"tx_hash":"0x"+"bb"*32,"direction":"IN","atomic_amount":"3",
             "block_number":150}]
        return report
    monkeypatch.setattr(reader,"independently_price_stored_evm_fill",price)
    monkeypatch.setattr(reader,"audit_original_native_value_interval",interval)
    async def run():
        async with httpx.AsyncClient() as client:
            return await reader.read_owner_native_value_history_readiness(
                did="did:privy:owner_123",asset=asset,client=client)
    result=asyncio.run(run())
    assert len(result["unaccounted_native_transfers"])==1
    assert result["fee_assessment_eligible"] is False
    assert result["hwm_updated"] is False


@pytest.mark.parametrize("alter",[
    lambda p:p.update(historic_usd_priced=False),
    lambda p:p.update(native_cycle_complete=False),
    lambda p:p.update(wallet_address="0x"+"cc"*20),
    lambda p:p["native_conversion_trace"].update(
        original_finalized_internal_value_proven=False),
    lambda p:p["native_conversion_trace"].update(
        full_historical_wallet_activity_proven=True),
])
def test_original_native_unverified_history_fails_before_wallet_audit(
    monkeypatch,alter,
):
    original=proof("AVAX",TX1,"BUY",100)
    alter(original)
    monkeypatch.setattr(reader,"_load_all_verified_owner_native_fills",
                        lambda *_:[{"tx_hash":TX1,"wallet_address":OWNER}])
    async def price(**kw):return original
    async def must_not_run(**kw):
        raise AssertionError("Unproven source must not be audited or charged")
    monkeypatch.setattr(reader,"independently_price_stored_evm_fill",price)
    monkeypatch.setattr(reader,"audit_original_native_value_interval",
                        must_not_run)
    async def run():
        async with httpx.AsyncClient() as client:
            return await reader.read_owner_native_value_history_readiness(
                did="did:privy:owner_123",asset="AVAX",client=client)
    with pytest.raises(ValueError):
        asyncio.run(run())


@pytest.mark.parametrize("tamper",[
    lambda a:a.update(verified_native_conversion_count=3),
    lambda a:a.update(complete_wallet_lifetime_and_cost_basis_proven=True),
    lambda a:a.update(eligible_to_assess_five_percent_fee=True),
    lambda a:a.update(wallet_address="0x"+"cc"*20),
    lambda a:a.update(mode="CLIENT_SUPPLIED_CUSTODY"),
])
def test_native_readiness_never_accepts_fee_eligible_or_fake_interval(
    monkeypatch,tamper,
):
    a=proof("POL",TX1,"BUY",100)
    monkeypatch.setattr(reader,"_load_all_verified_owner_native_fills",
                        lambda did,asset:[{"tx_hash":TX1,"wallet_address":OWNER}])
    async def price(**kw):return a
    async def interval(**kw):
        r=audit("POL")
        r["verified_native_conversion_count"]=1
        tamper(r)
        return r
    monkeypatch.setattr(reader,"independently_price_stored_evm_fill",price)
    monkeypatch.setattr(reader,"audit_original_native_value_interval",interval)
    async def run():
        async with httpx.AsyncClient() as client:
            return await reader.read_owner_native_value_history_readiness(
                did="did:privy:owner_123",asset="POL",client=client)
    # Every deliberately forged key/claim must be rejected.
    with pytest.raises(ValueError):
        asyncio.run(run())


def test_no_native_orders_never_proves_empty_wallet_hwm(monkeypatch):
    monkeypatch.setattr(reader,"_load_all_verified_owner_native_fills",
                        lambda *_:[])
    async def run():
        async with httpx.AsyncClient() as client:
            return await reader.read_owner_native_value_history_readiness(
                did="did:privy:owner_123",asset="BNB",client=client)
    report=asyncio.run(run())
    assert report["original_finalized_fill_count"]==0
    assert report["fee_assessment_eligible"] is False
