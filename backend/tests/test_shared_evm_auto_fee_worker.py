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
        {"privy_did":"did:privy:arb_owner12345","asset":"ARB",
         "execution_id":"sig_three123456"},
    ]
    monkeypatch.setattr(worker,"_pending_owner_auto_fee_obligations",
                        lambda:original)
    monkeypatch.setattr(worker,"_pending_combined_grant_fee_obligations",
                        lambda:[])
    sent=[]
    async def send(**kw):
        sent.append((kw["did"],kw["asset"],kw["execution_id"]))
        return {"state":"SUBMITTED"}
    monkeypatch.setattr(worker,"submit_original_owner_auto_fee_once",send)
    async def run():
        async with httpx.AsyncClient() as client:
            return await worker.run_owner_approved_auto_fee_batch(client=client)
    r=asyncio.run(run())
    assert r["new_fee_sends"]==3
    assert len(sent)==3
    assert {item[1] for item in sent}=={"LINK","UNI","ARB"}
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
    monkeypatch.setattr(worker,"_pending_combined_grant_fee_obligations",
                        lambda:[])
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
    assert source.index("reconcile_verified_erc20_fees_once(client=client,capture_only=True)") < source.index(
        "reconcile_auto_fee_receipts_once()")
    assert source.index("reconcile_auto_fee_receipts_once()") < source.index(
        "run_owner_approved_auto_fee_batch(client=client)")


def test_fee_work_pages_reach_new_owners_even_if_old_grants_fail(monkeypatch):
    """Old unavailable remote grants cannot monopolize every background scan."""
    monkeypatch.setattr(worker,"automatic_fee_transfer_worker_enabled",lambda:True)
    monkeypatch.setattr(worker,"_PENDING_FEE_SCAN_OFFSET",0)
    monkeypatch.setattr(worker,"_db_url",lambda:"postgresql://test.invalid")
    monkeypatch.setattr(worker,"_ensure_schema",lambda _:None)
    records=[{"privy_did":f"did:privy:owner_{i}",
              "asset":"ARB","execution_id":f"sig_abcdefgh{i}"} for i in range(5)]
    queries=[]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,params):
            assert "o.fee_due_usd=e.fee_assessed_usd" in sql
            assert "g.status='GRANT_VERIFIED'" in sql
            assert "p.execution_id IS NULL AND a.execution_id IS NULL" in sql
            assert "LIMIT %s OFFSET %s" in sql
            self.limit,self.offset=params
            queries.append(params)
        def fetchall(self):return records[self.offset:self.offset+self.limit]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return Cursor()
    monkeypatch.setattr(worker.psycopg,"connect",lambda *args,**kwargs:DB())
    pages=[worker._pending_owner_auto_fee_obligations(limit=2)
           for _ in range(4)]
    assert pages==[records[:2],records[2:4],records[4:],records[:2]]
    assert queries==[(2,0),(2,2),(2,4),(2,5),(2,0)]


def test_disabled_fee_worker_does_not_scan_privy_debt(monkeypatch):
    monkeypatch.setattr(worker,"automatic_fee_transfer_worker_enabled",lambda:False)
    monkeypatch.setattr(worker,"_db_url",lambda:pytest.fail("No database on disabled fee worker"))
    assert worker._pending_owner_auto_fee_obligations()==[]



def test_original_single_grant_hwm_debt_is_scanned_without_second_fee_signup(
    monkeypatch,
):
    monkeypatch.setattr(worker,"automatic_fee_transfer_worker_enabled",lambda:True)
    monkeypatch.setattr(worker,"_COMBINED_FEE_PAGE_OFFSET",0)
    monkeypatch.setattr(worker,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(worker,"_ensure_schema",lambda _:None)
    inspected=[]
    class Cur:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,args):
            inspected.append(sql)
            assert "g.authorization_method='PRIVY_SCOPED_GRANT'" in sql
            assert "separate.privy_did IS NULL" in sql
            assert "o.fee_due_usd=e.fee_assessed_usd" in sql
        def fetchall(self):return []
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return Cur()
    monkeypatch.setattr(worker.psycopg,"connect",lambda *a,**kw:DB())
    assert worker._pending_combined_grant_fee_obligations()==[]
    assert len(inspected)==1
