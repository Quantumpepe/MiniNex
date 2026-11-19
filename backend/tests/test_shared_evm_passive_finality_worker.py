"""Passive original EVM order finality does not send or duplicate funds."""
import asyncio
import pytest
from backend import shared_evm_passive_finality_worker as worker
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
TX="0x"+"cd"*32
KEY="sig_1234567890"


def order(asset,side="BUY"):
    stable="USDT" if asset=="BNB" else "USDC"
    return dict(privy_did="did:privy:test_12345",
                asset=asset,stable=stable,side=side,
                chain_id=_pair(asset,stable,side)["chain_id"],
                intent_key=KEY,tx_hash=TX,wallet_address=WALLET)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_real_owner_submitted_order_per_chain_is_eligible_for_passive_finality(asset):
    r=worker.validate_original_submitted_swap_record(order(asset))
    assert r["tx_hash"]==TX
    assert r["asset"]==asset


@pytest.mark.parametrize("mutation",[
    lambda r:r.update(chain_id=1),
    lambda r:r.update(tx_hash=None),
    lambda r:r.update(wallet_address="0x"+"no"*20),
    lambda r:r.update(intent_key="bad"),
    lambda r:r.update(privy_did="paper:unknown"),
])
def test_only_original_immutable_db_send_record_is_reconciled(mutation):
    row=order("BNB")
    mutation(row)
    with pytest.raises(ValueError):
        worker.validate_original_submitted_swap_record(row)


def test_worker_joins_submitted_and_finalizes_without_resend(monkeypatch):
    pending=[order("BNB"),order("LINK"),order("UNI"),order("ARB")]
    monkeypatch.setattr(worker,"original_submitted_evm_orders",lambda:pending)
    called=[]
    async def settle(**kw):
        called.append(kw)
        return {"state":"FINALIZED_SUCCESS" if kw["asset"]!="UNI"
                else "AWAITING_FINALITY"}
    monkeypatch.setattr(worker,"reconcile_evm_internal_order",settle)
    result=asyncio.run(worker.reconcile_submitted_evm_swaps_once())
    assert result=={
        "submitted_checked":4,"finalized_swaps":3,
        "awaiting_finality":1,"unresolved":0,
        "wallet_transactions_sent":0}
    assert {x["asset"] for x in called}=={"BNB","LINK","UNI","ARB"}


def test_live_worker_executes_receipt_finality_before_native_completion():
    import inspect
    from backend.shared_evm_live_worker import SharedEvmLiveWorker
    source=inspect.getsource(SharedEvmLiveWorker._run)
    assert source.index("reconcile_submitted_evm_swaps_once()")<source.index(
        "reconcile_native_buy_cycle_once(client=client)")
    assert source.index("reconcile_native_sell_once(client=client)")<source.index(
        "reconcile_verified_erc20_fees_once(client=client)")


def test_original_db_receipt_scan_includes_arbitrum_submissions(monkeypatch):
    sql_queries=[]
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,sql,params):sql_queries.append((sql,params))
        def fetchall(self):return [order("ARB",side="SELL")]
    monkeypatch.setattr(worker,"_db_url",lambda:"postgresql://test")
    monkeypatch.setattr(worker,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(worker.psycopg,"connect",lambda *a,**kw:Session())
    found=worker.original_submitted_evm_orders(limit=10)
    assert len(found)==1 and found[0]["asset"]=="ARB"
    assert "'ARB'" in sql_queries[0][0]
    assert "status='SUBMITTED'" in sql_queries[0][0]
    assert "tx_hash IS NOT NULL" in sql_queries[0][0]
    assert sql_queries[0][1]==(10,0)


def test_old_unmined_submissions_do_not_starve_newer_arb_receipts(monkeypatch):
    rows=[order(coin) for coin in ("BNB","POL","AVAX","LINK","UNI","ARB")]
    params_seen=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kw):return self
        def execute(self,sql,params):
            self.limit,self.offset=params
            params_seen.append(params)
            assert "tx_hash IS NOT NULL" in sql
            assert "ORDER BY created_at ASC" in sql
        def fetchall(self):return rows[self.offset:self.offset+self.limit]
    monkeypatch.setattr(worker,"_FINALITY_PAGE_OFFSET",0)
    monkeypatch.setattr(worker,"_db_url",lambda:"postgresql://test")
    monkeypatch.setattr(worker,"_ensure_schema",lambda db:None)
    monkeypatch.setattr(worker.psycopg,"connect",lambda *args,**kwargs:DB())
    pages=[worker.original_submitted_evm_orders(limit=2) for _ in range(4)]
    assert pages==[rows[:2],rows[2:4],rows[4:6],rows[:2]]
    assert params_seen==[(2,0),(2,2),(2,4),(2,6),(2,0)]
