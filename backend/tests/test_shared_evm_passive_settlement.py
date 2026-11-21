"""Multi-EVM passive worker never sends, signs or repeats ambiguous orders."""
import asyncio
import inspect

import pytest

from backend import shared_evm_passive_settlement as m

DID="did:privy:alice"
TX="0x"+"ab"*32
KEY="sig_abcdef01234567890123456789"


def pending(asset="BNB",side="BUY",stable=None):
    return {
        "privy_did":DID,"asset":asset,"intent_key":KEY,
        "tx_hash":TX,"side":side,
        "stable":stable or ("USDT" if asset=="BNB" else "USDC"),
        "chain_id":{"BNB":56,"POL":137,"AVAX":43114,
                    "LINK":1,"UNI":1,"ARB":42161}[asset],
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_valid_network_specific_journal_row(asset):
    assert m._valid_pending_row(pending(asset)) is True


@pytest.mark.parametrize("change",[
    lambda r:r.update(chain_id=1),
    lambda r:r.update(tx_hash="0x1234"),
    lambda r:r.update(side="SHORT"),
    lambda r:r.update(privy_did="other"),
    lambda r:r.update(intent_key="bad"),
    lambda r:r.update(stable="DAI"),
])
def test_invalid_pending_claim_rejected_before_network(change):
    row=pending("AVAX")
    change(row)
    assert m._valid_pending_row(row) is False


def test_passive_receipt_worker_needs_only_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL",raising=False)
    assert m._settlement_enabled() is False
    assert asyncio.run(m.reconcile_submitted_evm_once())=={
        "worker_enabled":False,"scanned":0,
        "finalized":0,"pending":0,"errors":0,
        "new_transactions_sent":0,
    }


def test_receipts_do_not_depend_on_any_operator_trading_flag(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    monkeypatch.delenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_EVM_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED",raising=False)
    assert m._settlement_enabled() is True
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","false")
    assert m._settlement_enabled() is True
    monkeypatch.delenv("DATABASE_URL")
    assert m._settlement_enabled() is False


def test_live_worker_owns_finality_poll_without_duplicate_passive_task(monkeypatch):
    monkeypatch.setenv("MININEX_EVM_AUTO_WORKER_ENABLED","explicit-live")
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    monkeypatch.setenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED","explicit-readonly")
    monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    async def check():
        worker=m.PassiveEvmSettlementWorker()
        await worker.start()
        assert worker._task is None
        await worker.stop()
    asyncio.run(check())


def test_completed_pending_and_bad_rows_are_separately_counted(monkeypatch):
    monkeypatch.setenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED","explicit-readonly")
    monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    rows=[pending("BNB"),pending("AVAX"),pending("LINK"),
          {**pending("POL"),"chain_id":56},pending("UNI"),pending("ARB")]
    monkeypatch.setattr(m,"_pending_submitted",lambda limit:rows)
    calls=[]
    async def observed(**kwargs):
        calls.append(kwargs)
        if kwargs["asset"]=="BNB":
            return {"state":"FINALIZED_SUCCESS"}
        if kwargs["asset"]=="AVAX":
            return {"state":"FINALIZED_REVERTED"}
        if kwargs["asset"]=="LINK":
            return {"state":"AWAITING_FINALITY"}
        raise RuntimeError("Temporary RPC outage")
    monkeypatch.setattr(m,"reconcile_evm_internal_order",observed)
    result=asyncio.run(m.reconcile_submitted_evm_once(limit=10))
    assert result=={
        "worker_enabled":True,"scanned":6,
        "finalized":2,"pending":1,"errors":3,
        "new_transactions_sent":0,
    }
    assert [c["asset"] for c in calls]==["BNB","AVAX","LINK","UNI","ARB"]


def test_no_background_ability_to_resend_unknown_or_uncertain_claim():
    source=inspect.getsource(m)
    assert "post_claimed_privy_evm_rpc" not in source
    assert "sign_privy_evm_swap_request" not in source
    assert "claim_internal_evm_send" not in source
    assert "record_internal_evm_send_result" not in source
    assert "WHERE status='SUBMITTED' AND tx_hash IS NOT NULL" in source
    from backend.app import app
    assert all("evm-passive-settlement" not in r.path for r in app.routes)


def test_readonly_original_receipt_scan_rotates_all_wallets(monkeypatch):
    monkeypatch.setenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED","explicit-readonly")
    monkeypatch.setenv("DATABASE_URL","postgresql://test.invalid")
    monkeypatch.setattr(m,"_PASSIVE_PAGE_OFFSET",0)
    monkeypatch.setattr(m,"_db_url",lambda:"postgresql://test.invalid")
    monkeypatch.setattr(m,"_ensure_schema",lambda _:None)
    records=[pending("LINK")|{"intent_key":f"sig_abcdef0{i}"} for i in range(5)]
    sql_calls=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,sql,args):
            self.limit,self.offset=args
            sql_calls.append((sql,args))
        def fetchall(self):return records[self.offset:self.offset+self.limit]
    monkeypatch.setattr(m.psycopg,"connect",lambda *a,**kwargs:DB())
    pages=[m._pending_submitted(limit=2) for _ in range(4)]
    assert pages==[records[:2],records[2:4],records[4:],records[:2]]
    assert [args for _,args in sql_calls]==[
        (2,0),(2,2),(2,4),(2,5),(2,0)]
    assert all("LIMIT %s OFFSET %s" in sql for sql,_ in sql_calls)
