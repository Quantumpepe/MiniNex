"""Native EVM SELL never repeats a deposit, invents a swap, or books paper."""
import asyncio
from decimal import Decimal
import inspect
import httpx
import pytest
from fastapi import HTTPException

from backend import shared_evm_native_sell_reconcile as rec
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:owner"
KEY="sig_abcdef01234567890123456789"
WALLET="0x"+"ab"*20
HASH="0x"+"cd"*32
ASSETS={"BNB":"USDT","POL":"USDC","AVAX":"USDC"}


def proof(asset,success=True):
    pair=_pair(asset,ASSETS[asset],"SELL")
    return {"mode":"SHARED_FINALIZED_NATIVE_EVM_CONVERSION",
            "asset":asset,"network":pair["network"],
            "chain_id":pair["chain_id"],
            "wallet_address":WALLET,
            "tx_hash":HASH,"operation":"DEPOSIT",
            "transaction_finalized":True,
            "confirmed_native_conversion":success,
            "state":"FINALIZED_NATIVE_CONVERSION"
                    if success else "FINALIZED_REVERTED",
            "amount_native":"1.125" if success else None}


def original(asset):
    pair=_pair(asset,ASSETS[asset],"SELL")
    return {"status":"SUBMITTED",
            "chain_id":pair["chain_id"],"stable":ASSETS[asset],
            "wallet_address":WALLET,
            "tx_hash":HASH,"amount_wei":Decimal("1125000000000000000")}


class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows);self.calls=[]
    def execute(self,query,args):
        self.calls.append((query,args))
    def fetchone(self):
        return next(self.rows)


@pytest.mark.parametrize("asset",list(ASSETS))
@pytest.mark.parametrize("success",[True,False])
def test_native_sell_only_finalizes_matching_actual_receipt(asset,success):
    state="FINALIZED_SUCCESS" if success else "FINALIZED_REVERTED"
    cur=Cursor([original(asset),{"status":state}])
    result=rec._finalize_native_sell_deposit_locked(
        cur,did=DID,asset=asset,key=KEY,proof=proof(asset,success))
    assert result==state
    assert len(cur.calls)==2
    assert "FOR UPDATE" in cur.calls[0][0]
    assert "AND status='SUBMITTED'" in cur.calls[1][0]


@pytest.mark.parametrize("mutate",[
    lambda p:p.update(wallet_address="0x"+"ef"*20),
    lambda p:p.update(tx_hash="0x"+"ab"*32),
    lambda p:p.update(chain_id=1),
    lambda p:p.update(operation="WITHDRAW"),
    lambda p:p.update(transaction_finalized=False),
    lambda p:p.update(amount_native="1.126"),
    lambda p:p.update(confirmed_native_conversion=False),
])
def test_native_sell_wrong_wallet_hash_chain_or_wei_never_finalizes(mutate):
    p=proof("BNB")
    mutate(p)
    cur=Cursor([original("BNB")])
    with pytest.raises((ValueError,HTTPException)):
        rec._finalize_native_sell_deposit_locked(
            cur,did=DID,asset="BNB",key=KEY,proof=p)
    assert not any("UPDATE mininex_live_evm_native_sell_intents" in q
                   for q,args in cur.calls)


@pytest.mark.parametrize("asset",list(ASSETS))
def test_deposit_to_sell_uses_original_exact_wei(asset):
    pair=_pair(asset,ASSETS[asset],"SELL")
    row={"asset":asset,"stable":ASSETS[asset],
         "chain_id":pair["chain_id"],
         "amount_wei":Decimal("1125000000000000000")}
    assert rec._exact_wrapped_sell_amount(row)=="1.125"


@pytest.mark.parametrize("tamper",[
    lambda r:r.update(amount_wei="NaN"),
    lambda r:r.update(amount_wei=Decimal("1.5")),
    lambda r:r.update(amount_wei=0),
    lambda r:r.update(chain_id=1),
    lambda r:r.update(stable="INVALID"),
])
def test_native_sell_wont_swap_unverified_quantity(tamper):
    pair=_pair("POL","USDC","SELL")
    row={"asset":"POL","stable":"USDC",
         "chain_id":pair["chain_id"],"amount_wei":10**18}
    tamper(row)
    with pytest.raises(ValueError):
        rec._exact_wrapped_sell_amount(row)


def test_worker_uses_only_finalized_deposit_with_no_existing_swap():
    source=inspect.getsource(rec.original_native_sells_ready_for_swap)
    assert "n.status='FINALIZED_SUCCESS'" in source
    assert "s.status='ACTIVE'" in source
    assert "s.budget_revision=n.budget_revision" in source
    assert "b.intent_key IS NULL" in source
    assert "mininex_live_evm_router_approval_sends" in source
    assert "'RECONCILIATION_REQUIRED'" in source
    assert "LEFT JOIN mininex_live_evm_auto_broadcasts" in source
    source=inspect.getsource(rec.pending_native_sell_deposits)
    assert "status='SUBMITTED' AND tx_hash IS NOT NULL" in source
    assert "RECONCILIATION_REQUIRED" not in source


def test_cycle_reconciles_only_verified_native_and_swap_same_signal():
    s=inspect.getsource(rec.original_native_sells_needing_cycle)
    assert "b.status='FINALIZED_SUCCESS' AND b.side='SELL'" in s
    assert "n.status='FINALIZED_SUCCESS' AND f.side='SELL'" in s
    assert "r.state='RECONCILIATION_REQUIRED'" in s
    assert "c.intent_key IS NULL" in s


def test_once_only_original_event_passed_to_final_router02_sell(monkeypatch):
    phases=[]
    base={"asset":"BNB","stable":"USDT","chain_id":56,
          "privy_did":DID,"intent_key":KEY,"strategy_run_id":"owner_20261117T160000",
          "signal_event_id":"market_12345",
          "budget_revision":7,"amount_wei":10**18}
    monkeypatch.setattr(rec,"pending_native_sell_deposits",
                        lambda limit:phases.append("read_deposits") or [])
    monkeypatch.setattr(rec,"original_native_sells_needing_cycle",
                        lambda limit:phases.append("read_cycles") or [])
    monkeypatch.setattr(rec,"original_native_sells_ready_for_swap",
                        lambda limit:phases.append("read_ready") or [base])
    async def only_send(**kw):
        phases.append("single_sell")
        assert kw["side"]=="SELL"
        assert kw["strategy_run_id"]==base["strategy_run_id"]
        assert kw["signal_event_id"]==base["signal_event_id"]
        assert kw["amount"]=="1"
        assert kw["budget_revision"]==7
        return {"state":"SUBMITTED"}
    monkeypatch.setattr(rec,"execute_internal_evm_strategy_signal",only_send)
    async def approved(did,**kw):
        phases.append("allowance")
        assert kw["side"]=="SELL" and kw["amount"]=="1"
        pair=_pair(base["asset"],base["stable"],"SELL")
        return {
            "wallet_identity_verified":True,"asset":base["asset"],
            "side":"SELL","stable":base["stable"],"chain_id":base["chain_id"],
            "source_token_address":pair["source_token"],
            "native_gas_available":True,"source_funds_sufficient":True,
            "router_allowance_sufficient":True}
    monkeypatch.setattr(rec,"check_evm_source_allowance",approved)
    async def go():
        async with httpx.AsyncClient() as client:
            return await rec.reconcile_native_sell_once(client=client)
    outcome=asyncio.run(go())
    assert outcome["original_swaps_submitted"]==1
    assert phases==["read_deposits","read_cycles","read_ready",
                    "allowance","single_sell"]


@pytest.mark.parametrize("asset",list(ASSETS))
def test_receipt_pending_never_creates_second_native_deposit(monkeypatch,asset):
    p=_pair(asset,ASSETS[asset],"SELL")
    async def check(did,tx_hash,**kw):
        assert kw["operation"]=="DEPOSIT"
        return {"transaction_finalized":False}
    monkeypatch.setattr(rec,"check_user_native_evm_conversion",check)
    async def go():
        return await rec.reconcile_original_sell_deposit({
            "privy_did":DID,"asset":asset,
            "intent_key":KEY,"chain_id":p["chain_id"],"tx_hash":HASH})
    assert asyncio.run(go())=="AWAITING_FINALITY"


def test_original_sell_deposit_receipts_rotate_across_all_users(monkeypatch):
    """An older pending native conversion must not starve another coin."""
    records=[{"privy_did":f"did:privy:owner_{i}","asset":asset,
              "intent_key":f"sig_abcdef123456{i}"} for i,asset in
             enumerate(["BNB","POL","AVAX","BNB","POL"])]
    requests=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,query,args):
            assert "mininex_live_evm_native_sell_intents" in query
            assert "status='SUBMITTED' AND tx_hash IS NOT NULL" in query
            assert "LIMIT %s OFFSET %s" in query
            self.size,self.offset=args
            requests.append(args)
        def fetchall(self):return records[self.offset:self.offset+self.size]
    monkeypatch.setattr(rec,"_NATIVE_SELL_RECEIPT_PAGE_OFFSET",0)
    monkeypatch.setattr(rec,"_db_url",lambda:"postgresql://test")
    monkeypatch.setattr(rec,"_ensure_schema",lambda db:None)
    monkeypatch.setattr(rec.psycopg,"connect",lambda *args,**kwargs:DB())
    batches=[rec.pending_native_sell_deposits(limit=2) for _ in range(4)]
    assert batches==[records[:2],records[2:4],records[4:],records[:2]]
    assert requests==[(2,0),(2,2),(2,4),(2,5),(2,0)]


@pytest.mark.parametrize("function,offset_var,required_sql",[["original_native_sells_ready_for_swap","_NATIVE_SELL_SWAP_PAGE_OFFSET","b.intent_key IS NULL"],["original_native_sells_needing_cycle","_NATIVE_SELL_CYCLE_PAGE_OFFSET","c.intent_key IS NULL"]])
def test_downstream_native_reconciliation_pages_are_fair(monkeypatch,function,offset_var,required_sql):
    """Owner-active native SELL/BUY conversion stages cannot monopolize a batch."""
    history=[{"privy_did":f"did:privy:owner_{i}","asset":asset,
              "intent_key":f"sig_abcdef012345{i}"} for i,asset in
             enumerate(["BNB","POL","AVAX","BNB","POL"])]
    calls=[]
    class Database:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,sql,params):
            assert required_sql in sql
            assert "LIMIT %s OFFSET %s" in sql
            self.limit,self.offset=params
            calls.append(params)
        def fetchall(self):return history[self.offset:self.offset+self.limit]
    monkeypatch.setattr(rec,offset_var,0)
    monkeypatch.setattr(rec,"_db_url",lambda:"postgresql://test")
    monkeypatch.setattr(rec,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(rec.psycopg,"connect",lambda *args,**kwargs:Database())
    scan=getattr(rec,function)
    pages=[scan(limit=2) for _ in range(4)]
    assert pages==[history[:2],history[2:4],history[4:],history[:2]]
    assert calls==[(2,0),(2,2),(2,4),(2,5),(2,0)]


@pytest.mark.parametrize("asset",list(ASSETS))
def test_finalized_native_sell_can_submit_original_scoped_wrapped_approval(
    monkeypatch,asset,
):
    pair=_pair(asset,ASSETS[asset],"SELL")
    base={"asset":asset,"stable":ASSETS[asset],
          "chain_id":pair["chain_id"],"privy_did":DID,
          "intent_key":KEY,"strategy_run_id":"owner_20261117T160000",
          "signal_event_id":"market_12345","budget_revision":7,
          "amount_wei":10**18}
    monkeypatch.setattr(rec,"pending_native_sell_deposits",lambda limit:[])
    monkeypatch.setattr(rec,"original_native_sells_needing_cycle",lambda limit:[])
    monkeypatch.setattr(rec,"original_native_sells_ready_for_swap",lambda limit:[base])
    async def evidence(did,**kw):
        assert did==DID and kw["amount"]=="1" and kw["side"]=="SELL"
        return {
            "wallet_identity_verified":True,"asset":asset,"side":"SELL",
            "stable":ASSETS[asset],"chain_id":pair["chain_id"],
            "source_token_address":pair["source_token"],
            "native_gas_available":True,"source_funds_sufficient":True,
            "router_allowance_sufficient":False}
    monkeypatch.setattr(rec,"check_evm_source_allowance",evidence)
    calls=[]
    async def one_owner_approval(**kw):
        calls.append(kw)
        return {"state":"SUBMITTED","tx_hash":"0x"+"a"*64}
    async def never_swap(**kw):
        pytest.fail("No original swap before allowance is finalized")
    monkeypatch.setattr(rec,"submit_scoped_router_approval_once",one_owner_approval)
    monkeypatch.setattr(rec,"execute_internal_evm_strategy_signal",never_swap)
    async def go():
        async with httpx.AsyncClient() as client:
            return await rec.reconcile_native_sell_once(client=client)
    completed=asyncio.run(go())
    assert completed["original_swaps_submitted"]==0
    assert completed["pending"]==1
    assert len(calls)==1
    assert calls[0]["signal_event_id"]==base["signal_event_id"]
    assert calls[0]["amount_atomic"]==10**18
    assert calls[0]["budget_revision"]==7
