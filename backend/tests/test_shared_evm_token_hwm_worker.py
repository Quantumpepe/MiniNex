"""Accounting worker captures real onchain prices without sending money."""
import asyncio

import httpx
import pytest

from backend import shared_evm_token_hwm_worker as worker


def test_passive_price_capture_then_owner_verified_hwm(monkeypatch):
    observed=[]
    monkeypatch.setattr(worker,"_read_pending_original_usd_fills",lambda:[
        {"did":"did:privy:owner1","fill":{"asset":"LINK","tx_hash":"0x"+"ab"*32}},
    ])
    monkeypatch.setattr(worker,"_read_unaccounted_token_sell_owners",lambda:[
        {"privy_did":"did:privy:owner1","asset":"LINK"}])
    async def prices(**kw):
        observed.append(("quote",kw["did"],kw["row"]["asset"]))
        assert kw["durable_rates"] is True
        return {"historic_usd_priced":True}
    async def hwm(**kw):
        observed.append(("hwm",kw["did"],kw["asset"]))
        return {"mode":"HWM_ACCOUNTED_VERIFIED_ERC20_SELLS"}
    monkeypatch.setattr(worker,"independently_price_stored_evm_fill",prices)
    monkeypatch.setattr(worker,"reconcile_owner_erc20_hwm_after_verified_sell",hwm)
    async def run():
        async with httpx.AsyncClient() as client:
            return await worker.reconcile_verified_erc20_fees_once(client=client)
    result=asyncio.run(run())
    assert result=={
        "prices_recorded":1,"token_accounts_reconciled":1,
        "accounts_needing_manual_evidence":0,"wallet_transactions_sent":0}
    assert [x[0] for x in observed]==["quote","hwm"]


def test_missing_onchain_price_does_not_create_fee_or_block_other_coin(monkeypatch):
    monkeypatch.setattr(worker,"_read_pending_original_usd_fills",lambda:[
        {"did":"did:privy:owner1","fill":{"asset":"LINK"}},
        {"did":"did:privy:owner2","fill":{"asset":"UNI"}},
    ])
    monkeypatch.setattr(worker,"_read_unaccounted_token_sell_owners",lambda:[])
    async def prices(**kw):
        if kw["row"]["asset"]=="LINK":
            raise ValueError("Older unsupported history")
        return {"historic_usd_priced":True}
    monkeypatch.setattr(worker,"independently_price_stored_evm_fill",prices)
    async def run():
        async with httpx.AsyncClient() as client:
            return await worker.reconcile_verified_erc20_fees_once(client=client)
    r=asyncio.run(run())
    assert r["prices_recorded"]==1
    assert r["accounts_needing_manual_evidence"]==1
    assert r["wallet_transactions_sent"]==0


def test_worker_is_wired_into_existing_evm_cycle_without_privy_fee_signer():
    import inspect
    from backend import shared_evm_live_worker
    source=inspect.getsource(shared_evm_live_worker.SharedEvmLiveWorker)
    assert "reconcile_verified_erc20_fees_once(client=client)" in source
    assert "submit_owner_treasury_payment" not in source


def test_arb_uses_the_same_verified_hwm_worker_without_fee_send(monkeypatch):
    """ARB is an ERC20 settlement, not a native wrap/unwrap fee shortcut."""
    events=[]
    monkeypatch.setattr(worker,"_read_pending_original_usd_fills",lambda:[
        {"did":"did:privy:arb_owner","fill":{"asset":"ARB","tx_hash":"0x"+"cd"*32}},
    ])
    monkeypatch.setattr(worker,"_read_unaccounted_token_sell_owners",lambda:[
        {"privy_did":"did:privy:arb_owner","asset":"ARB"}])
    async def priced(**kwargs):
        events.append(("rate",kwargs["row"]["asset"]))
        assert kwargs["durable_rates"] is True
        return {"historic_usd_priced":True}
    async def account(**kwargs):
        events.append(("fifo",kwargs["asset"]))
        return {"mode":"HWM_ACCOUNTED_VERIFIED_ERC20_SELLS"}
    monkeypatch.setattr(worker,"independently_price_stored_evm_fill",priced)
    monkeypatch.setattr(worker,"reconcile_owner_erc20_hwm_after_verified_sell",account)
    async def run():
        async with httpx.AsyncClient() as client:
            return await worker.reconcile_verified_erc20_fees_once(client=client)
    result=asyncio.run(run())
    assert events==[("rate","ARB"),("fifo","ARB")]
    assert result["token_accounts_reconciled"]==1
    assert result["wallet_transactions_sent"]==0


def test_original_usd_capture_prioritizes_newest_finalized_arb_token_fill(monkeypatch):
    """A backlog must not let aging failed prices starve fresh on-chain fills."""
    queries=[]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,params):queries.append((sql,params))
        def fetchall(self):return []
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return Cursor()
    monkeypatch.setattr(worker,"_db_url",lambda:"postgresql://test")
    monkeypatch.setattr(worker,"_ensure_schema",lambda db:None)
    monkeypatch.setattr(worker.psycopg,"connect",lambda *args,**kwargs:Connection())
    assert worker._read_pending_original_usd_fills(limit=40)==[]
    sql,params=queries[0]
    assert "f.asset IN ('LINK','UNI','ARB')" in sql
    assert "b.status='FINALIZED_SUCCESS'" in sql
    assert "ORDER BY f.created_at DESC" in sql
    assert "n.quoted_asset='ETH'" in sql  # ARB pays native gas in ETH
    assert params==(40,)
