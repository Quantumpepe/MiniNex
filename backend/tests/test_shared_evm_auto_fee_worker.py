"""Fee worker sends nothing until explicit owner wallet grant and toggle."""
import asyncio
import httpx
import pytest
from backend import shared_evm_auto_fee_worker as worker


def test_disabled_fee_worker_returns_no_candidates(monkeypatch):
    monkeypatch.setattr(worker,"automatic_fee_transfer_worker_enabled",
                        lambda:False)
    async def run():
        async with httpx.AsyncClient() as client:
            return await worker.run_owner_approved_auto_fee_batch(client=client)
    assert asyncio.run(run())=={
        "verified_fee_candidates":0,"new_fee_sends":0,
        "need_owner_or_network_reconciliation":0}


def test_verified_hwm_debt_is_processed_only_once_per_original_execution(monkeypatch):
    monkeypatch.setattr(worker,"automatic_fee_transfer_worker_enabled",
                        lambda:True)
    original=[
        {"privy_did":"did:privy:alice12345","asset":"LINK",
         "execution_id":"sig_one123456"},
        {"privy_did":"did:privy:bob123456","asset":"UNI",
         "execution_id":"sig_two123456"},
    ]
    monkeypatch.setattr(worker,"_pending_owner_auto_fee_obligations",
                        lambda:original)
    sent=[]
    async def send(**kw):
        sent.append((kw["did"],kw["asset"],kw["execution_id"]))
        return {"state":"SUBMITTED"}
    monkeypatch.setattr(worker,"submit_original_owner_auto_fee_once",send)
    async def run():
        async with httpx.AsyncClient() as client:
            return await worker.run_owner_approved_auto_fee_batch(client=client)
    r=asyncio.run(run())
    assert r["new_fee_sends"]==2
    assert len(sent)==2
    assert r["need_owner_or_network_reconciliation"]==0


def test_failed_owner_privy_send_does_not_retry_original(monkeypatch):
    monkeypatch.setattr(worker,"automatic_fee_transfer_worker_enabled",
                        lambda:True)
    original=[
        {"privy_did":"did:privy:alice12345","asset":"LINK",
         "execution_id":"sig_one123456"},
    ]
    monkeypatch.setattr(worker,"_pending_owner_auto_fee_obligations",
                        lambda:original)
    calls=[]
    async def fail(**kw):
        calls.append(kw)
        raise ValueError("Privy send uncertain")
    monkeypatch.setattr(worker,"submit_original_owner_auto_fee_once",fail)
    async def run():
        async with httpx.AsyncClient() as client:
            return await worker.run_owner_approved_auto_fee_batch(client=client)
    result=asyncio.run(run())
    assert result["new_fee_sends"]==0
    assert result["need_owner_or_network_reconciliation"]==1
    assert len(calls)==1


def test_live_worker_cost_hwm_fee_execution_order_is_explicit():
    import inspect
    from backend.shared_evm_live_worker import SharedEvmLiveWorker
    source=inspect.getsource(SharedEvmLiveWorker._run)
    assert source.index("reconcile_verified_erc20_fees_once(client=client)") < source.index(
        "reconcile_auto_fee_receipts_once()")
    assert source.index("reconcile_auto_fee_receipts_once()") < source.index(
        "run_owner_approved_auto_fee_batch(client=client)")
